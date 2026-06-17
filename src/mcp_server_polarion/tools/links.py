"""Work item link tools — list, create, delete, and update links."""

from __future__ import annotations

import logging
from typing import Literal, cast

from fastmcp import Context
from pydantic import Field

from mcp_server_polarion.core.client import PolarionClient
from mcp_server_polarion.core.exceptions import (
    PolarionAuthError,
    PolarionError,
    PolarionNotFoundError,
)
from mcp_server_polarion.models import (
    JsonValue,
    PaginatedResult,
    WorkItemLink,
    WorkItemLinkRef,
    WorkItemLinksCreateResult,
    WorkItemLinksDeleteResult,
    WorkItemLinkSpec,
    WorkItemLinkUpdateResult,
    WorkItemLinkUpdateSpec,
)
from mcp_server_polarion.server import mcp
from mcp_server_polarion.tools._shared.guard import (
    guard_work_item_link_roles,
    guard_work_item_link_targets,
    partition_delete_links,
)
from mcp_server_polarion.tools._shared.helpers import (
    DEFAULT_PAGE_SIZE,
    MAX_BULK_ITEMS,
    WORK_ITEM_LIST_FIELDS,
    build_included_work_item_map,
    compute_has_more,
    encode_path_segment,
    extract_relationship_id,
    extract_short_id,
    extract_total_count,
    get_client,
    parse_work_item_summaries,
    safe_str,
    split_module_id,
    summary_to_back_link,
    validate_work_item_id_for_lucene,
)

logger = logging.getLogger("mcp_server_polarion.tools.links")


def _extract_created_link_ids(response: dict[str, object]) -> list[str]:
    """Composite link ids verbatim, input order, from a bulk create response —
    the path ids for later PATCH / DELETE. Empty on malformed shapes.
    """
    data = response.get("data")
    if not isinstance(data, list):
        return []
    ids: list[str] = []
    for item in data:
        if isinstance(item, dict):
            item_id = item.get("id")
            if isinstance(item_id, str):
                ids.append(item_id)
    return ids


def _build_create_links_payload(
    *,
    source_project_id: str,
    links: list[WorkItemLinkSpec],
) -> dict[str, JsonValue]:
    """JSON:API body for bulk create-link POST; ``revision`` skipped when
    unset, ``target_project_id`` defaults to source.
    """
    data: list[JsonValue] = []
    for spec in links:
        tgt_proj = (
            spec.target_project_id
            if spec.target_project_id is not None
            else source_project_id
        )
        attributes: dict[str, JsonValue] = {"role": spec.role, "suspect": spec.suspect}
        if spec.revision:
            attributes["revision"] = spec.revision
        data.append(
            {
                "type": "linkedworkitems",
                "attributes": attributes,
                "relationships": {
                    "workItem": {
                        "data": {
                            "type": "workitems",
                            "id": f"{tgt_proj}/{spec.target_work_item_id}",
                        }
                    }
                },
            }
        )
    return {"data": data}


def _build_delete_links_payload(
    *,
    source_project_id: str,
    source_work_item_id: str,
    links: list[WorkItemLinkRef],
) -> tuple[list[str], dict[str, JsonValue]]:
    """(composite id list, JSON:API body) for bulk delete-link DELETE; each
    5-segment id ``<srcProj>/<srcWI>/<role>/<tgtProj>/<tgtWI>`` built from the
    structured ref.
    """
    link_ids: list[str] = []
    data: list[JsonValue] = []
    for ref in links:
        tgt_proj = (
            ref.target_project_id
            if ref.target_project_id is not None
            else source_project_id
        )
        link_id = (
            f"{source_project_id}/{source_work_item_id}/{ref.role}/"
            f"{tgt_proj}/{ref.target_work_item_id}"
        )
        link_ids.append(link_id)
        data.append({"type": "linkedworkitems", "id": link_id})
    return link_ids, {"data": data}


def _build_update_link_payload(
    *,
    source_project_id: str,
    source_work_item_id: str,
    spec: WorkItemLinkUpdateSpec,
) -> tuple[str, str, dict[str, JsonValue]]:
    """(composite id, path, body) for one link PATCH; ``suspect``/``revision``
    attached only when set (omit-preserve). Path built here so id and path
    share one ``tgt_proj``.
    """
    tgt_proj = (
        spec.target_project_id
        if spec.target_project_id is not None
        else source_project_id
    )
    link_id = (
        f"{source_project_id}/{source_work_item_id}/{spec.role}/"
        f"{tgt_proj}/{spec.target_work_item_id}"
    )
    path = (
        f"/projects/{encode_path_segment(source_project_id)}"
        f"/workitems/{encode_path_segment(source_work_item_id)}"
        f"/linkedworkitems/{encode_path_segment(spec.role)}"
        f"/{encode_path_segment(tgt_proj)}"
        f"/{encode_path_segment(spec.target_work_item_id)}"
    )
    attributes: dict[str, JsonValue] = {}
    if spec.revision is not None:
        attributes["revision"] = spec.revision
    if spec.suspect is not None:
        attributes["suspect"] = spec.suspect
    payload: dict[str, JsonValue] = {
        "data": {
            "type": "linkedworkitems",
            "id": link_id,
            "attributes": attributes,
        }
    }
    return link_id, path, payload


def _parse_work_item_links(
    response: dict[str, object],
    *,
    direction: Literal["forward", "back"],
) -> list[WorkItemLink]:
    """Parse linked work items into ``WorkItemLink``s; target id from
    ``relationships.workItem.data.id``, never by parsing the composite id.
    """
    work_item_map = build_included_work_item_map(response)

    items: list[WorkItemLink] = []
    data = response.get("data", [])
    if not isinstance(data, list):
        return items

    for item in data:
        if not isinstance(item, dict):
            continue
        attributes = item.get("attributes", {})
        if not isinstance(attributes, dict):
            attributes = {}

        role = safe_str(attributes.get("role", ""))
        suspect = bool(attributes.get("suspect", False))

        # Derive the target via relationships, never by parsing the 5-segment id.
        relationships = item.get("relationships", {})
        if not isinstance(relationships, dict):
            relationships = {}
        work_item_full_id = extract_relationship_id(relationships, "workItem")
        work_item_id = extract_short_id(work_item_full_id)
        if not work_item_id:
            continue

        title = ""
        work_item_type = ""
        work_item_status = ""
        space_id = ""
        document_name = ""
        work_item = work_item_map.get(work_item_full_id, {})
        work_item_attrs = work_item.get("attributes", {})
        if isinstance(work_item_attrs, dict):
            title = safe_str(work_item_attrs.get("title", ""))
            work_item_type = safe_str(work_item_attrs.get("type", ""))
            work_item_status = safe_str(work_item_attrs.get("status", ""))
        work_item_rels = work_item.get("relationships", {})
        if isinstance(work_item_rels, dict):
            space_id, document_name = split_module_id(
                extract_relationship_id(work_item_rels, "module")
            )

        items.append(
            WorkItemLink(
                id=work_item_id,
                title=title,
                role=role,
                direction=direction,
                suspect=suspect,
                type=work_item_type,
                status=work_item_status,
                space_id=space_id,
                document_name=document_name,
            )
        )
    return items


async def _get_forward_link_page(
    client: PolarionClient,
    *,
    project_id: str,
    work_item_id: str,
    page_size: int,
    page_number: int,
) -> PaginatedResult[WorkItemLink]:
    """Fetch a single page of forward (outgoing) links."""
    path = (
        f"/projects/{encode_path_segment(project_id)}"
        f"/workitems/{encode_path_segment(work_item_id)}/linkedworkitems"
    )
    try:
        response = await client.get(
            path,
            params={
                "fields[linkedworkitems]": "@all",
                "fields[workitems]": WORK_ITEM_LIST_FIELDS,
                "include": "workItem",
                "page[size]": page_size,
                "page[number]": page_number,
            },
        )
    except PolarionNotFoundError as exc:
        raise ValueError(
            f"Work item '{work_item_id}' not found in project "
            f"'{project_id}'. "
            "Use `list_work_items` to discover valid IDs."
        ) from exc
    except PolarionAuthError as exc:
        raise PermissionError(
            "Cannot access linked work items -- check your POLARION_TOKEN permissions."
        ) from exc
    except PolarionError as exc:
        raise RuntimeError(
            f"Failed to get linked work items for '{work_item_id}': {exc.message}"
        ) from exc

    items = _parse_work_item_links(response, direction="forward")

    raw_total = extract_total_count(response)
    total = raw_total
    if total <= 0 and items:
        total = (page_number - 1) * page_size + len(items)

    return PaginatedResult[WorkItemLink](
        items=items,
        total_count=total,
        page=page_number,
        page_size=page_size,
        has_more=compute_has_more(
            response, raw_total, page_number, page_size, len(items)
        ),
    )


async def _get_back_link_page(
    client: PolarionClient,
    *,
    project_id: str,
    work_item_id: str,
    page_size: int,
    page_number: int,
) -> PaginatedResult[WorkItemLink]:
    """Fetch a single page of back (incoming) links via Lucene query."""
    validate_work_item_id_for_lucene(work_item_id)
    try:
        response = await client.get(
            f"/projects/{encode_path_segment(project_id)}/workitems",
            params={
                "query": f"linkedWorkItems:{work_item_id}",
                "fields[workitems]": WORK_ITEM_LIST_FIELDS,
                "page[size]": page_size,
                "page[number]": page_number,
            },
        )
    except PolarionNotFoundError as exc:
        raise ValueError(
            f"Work item '{work_item_id}' not found in project "
            f"'{project_id}'. "
            "Use `list_work_items` to discover valid IDs."
        ) from exc
    except PolarionAuthError as exc:
        raise PermissionError(
            "Cannot access linked work items -- check your POLARION_TOKEN permissions."
        ) from exc
    except PolarionError as exc:
        raise RuntimeError(
            f"Backlink query failed for work item '{work_item_id}': {exc.message}"
        ) from exc

    summaries = parse_work_item_summaries(response.get("data", []))
    items = [summary_to_back_link(s) for s in summaries]

    raw_total = extract_total_count(response)
    total = raw_total
    if total <= 0 and items:
        total = (page_number - 1) * page_size + len(items)

    return PaginatedResult[WorkItemLink](
        items=items,
        total_count=total,
        page=page_number,
        page_size=page_size,
        has_more=compute_has_more(
            response, raw_total, page_number, page_size, len(items)
        ),
    )


@mcp.tool(
    tags={"read"},
    timeout=60.0,
    annotations={"readOnlyHint": True},
)
async def list_work_item_links(  # noqa: PLR0913
    ctx: Context,
    project_id: str = Field(description="Polarion project ID."),
    work_item_id: str = Field(description="Work item ID (e.g. 'MCPT-001')."),
    direction: Literal["forward", "back"] = "forward",
    page_size: int = Field(default=DEFAULT_PAGE_SIZE, ge=1, le=100),
    page_number: int = Field(default=1, ge=1),
) -> PaginatedResult[WorkItemLink]:
    """List a work item's links, one direction per call.

    Forward carries role (parent, verifies, …) and suspect; back is a Lucene
    fallback that drops role (always None) — recover it via forward on the
    source.
    """
    client = get_client(ctx)

    if direction == "forward":
        return await _get_forward_link_page(
            client,
            project_id=project_id,
            work_item_id=work_item_id,
            page_size=page_size,
            page_number=page_number,
        )
    return await _get_back_link_page(
        client,
        project_id=project_id,
        work_item_id=work_item_id,
        page_size=page_size,
        page_number=page_number,
    )


@mcp.tool(
    tags={"write"},
    timeout=60.0,
    annotations={
        # Additive: non-destructive, non-idempotent (a duplicate role+target 409s).
        "readOnlyHint": False,
        "destructiveHint": False,
        "idempotentHint": False,
        "openWorldHint": True,
    },
)
async def create_work_item_links(
    ctx: Context,
    project_id: str = Field(min_length=1, description="Source work item's project ID."),
    work_item_id: str = Field(
        min_length=1,
        description="Source work item ID.",
    ),
    links: list[WorkItemLinkSpec] = Field(  # noqa: B008
        min_length=1,
        max_length=MAX_BULK_ITEMS,
        description="Links to create under the source work item (1-50).",
    ),
    dry_run: bool = Field(
        default=False,
        description="Preview payload without writing; guards still query Polarion.",
    ),
) -> WorkItemLinksCreateResult:
    """Create 1-50 outgoing links from one source work item, atomically.

    Role and target existence validated before POST — invalid ones raise
    ValueError. Per spec: target_project_id defaults to source, revision pins
    (else HEAD), suspect flags re-review. A 4xx (e.g. duplicate role+target →
    409) rolls back the whole batch — re-query list_work_item_links before
    retrying. link_ids are the delete-path ids, input order.

    Phantom success: when the source sits in a document,
    move_work_item_to_document already auto-created one heading link; a NEW
    same-role link 201s but is NOT persisted — verify with list_work_item_links.
    """
    payload = _build_create_links_payload(
        source_project_id=project_id,
        links=links,
    )

    client = get_client(ctx)
    await guard_work_item_link_targets(client, project_id, links)
    await guard_work_item_link_roles(client, project_id, [spec.role for spec in links])

    if dry_run:
        return WorkItemLinksCreateResult(
            created=False,
            dry_run=True,
            link_ids=[],
            payload_preview=payload,
        )

    path = (
        f"/projects/{encode_path_segment(project_id)}"
        f"/workitems/{encode_path_segment(work_item_id)}/linkedworkitems"
    )
    try:
        response = await client.post(path, json=cast(dict[str, object], payload))
    except PolarionAuthError as exc:
        raise PermissionError(
            "Cannot create work item links -- check your POLARION_TOKEN permissions."
        ) from exc
    except PolarionNotFoundError as exc:
        raise ValueError(
            f"Work item '{work_item_id}' not found in project '{project_id}'. "
            "Use `list_work_items` to discover valid IDs."
        ) from exc
    except PolarionError as exc:
        raise RuntimeError(f"Failed to create work item links: {exc.message}") from exc

    link_ids = _extract_created_link_ids(response)
    if len(link_ids) != len(links):
        raise RuntimeError(
            f"Polarion accepted the bulk create-link request but returned "
            f"{len(link_ids)} ids for {len(links)} requested links. The batch "
            "may be partially created; verify with list_work_item_links before "
            "retrying."
        )

    return WorkItemLinksCreateResult(
        created=True,
        dry_run=False,
        link_ids=link_ids,
        payload_preview=None,
    )


@mcp.tool(
    tags={"write"},
    timeout=60.0,
    annotations={
        # Destructive but idempotent: unmatched ids are ignored, 204 regardless.
        "readOnlyHint": False,
        "destructiveHint": True,
        "idempotentHint": True,
        "openWorldHint": True,
    },
)
async def delete_work_item_links(
    ctx: Context,
    project_id: str = Field(min_length=1, description="Source work item's project ID."),
    work_item_id: str = Field(
        min_length=1,
        description="Source work item ID.",
    ),
    links: list[WorkItemLinkRef] = Field(  # noqa: B008
        min_length=1,
        max_length=MAX_BULK_ITEMS,
        description="Existing outgoing links to delete (1-50).",
    ),
    dry_run: bool = Field(
        default=False,
        description=(
            "Preview payload without deleting; the pre-read still queries Polarion."
        ),
    ),
) -> WorkItemLinksDeleteResult:
    """Delete 1-50 outgoing links from one source work item.

    Outgoing only — delete a back link from its source item. Refs from
    list_work_item_links(direction="forward") or a prior create. Stale refs
    never raise: a pre-read splits results into deleted_link_ids /
    not_found_link_ids.
    """
    link_ids, payload = _build_delete_links_payload(
        source_project_id=project_id,
        source_work_item_id=work_item_id,
        links=links,
    )

    client = get_client(ctx)
    deleted_link_ids, not_found_link_ids = await partition_delete_links(
        client, project_id, work_item_id, link_ids
    )

    if dry_run:
        return WorkItemLinksDeleteResult(
            deleted=False,
            dry_run=True,
            link_ids=link_ids,
            deleted_link_ids=deleted_link_ids,
            not_found_link_ids=not_found_link_ids,
            payload_preview=payload,
        )

    path = (
        f"/projects/{encode_path_segment(project_id)}"
        f"/workitems/{encode_path_segment(work_item_id)}/linkedworkitems"
    )
    try:
        await client.delete(path, json=cast(dict[str, object], payload))
    except PolarionAuthError as exc:
        raise PermissionError(
            "Cannot delete work item links -- check your POLARION_TOKEN permissions."
        ) from exc
    except PolarionNotFoundError as exc:
        raise ValueError(
            f"Source work item '{work_item_id}' not found in project "
            f"'{project_id}'. A nonexistent link target is ignored silently, so "
            f"this means the source work item itself is absent -- use "
            f"`list_work_items` to find valid ids."
        ) from exc
    except PolarionError as exc:
        raise RuntimeError(f"Failed to delete work item links: {exc.message}") from exc

    return WorkItemLinksDeleteResult(
        deleted=True,
        dry_run=False,
        link_ids=link_ids,
        deleted_link_ids=deleted_link_ids,
        not_found_link_ids=not_found_link_ids,
        payload_preview=None,
    )


@mcp.tool(
    tags={"write"},
    timeout=60.0,
    annotations={
        # Non-destructive, non-idempotent (a re-PATCH still bumps the revision).
        "readOnlyHint": False,
        "destructiveHint": False,
        "idempotentHint": False,
        "openWorldHint": True,
    },
)
async def update_work_item_link(  # noqa: PLR0913
    ctx: Context,
    project_id: str = Field(min_length=1, description="Source work item's project ID."),
    work_item_id: str = Field(
        min_length=1,
        description="Source work item ID.",
    ),
    role: str = Field(min_length=1, description="Role id of the existing link."),
    target_work_item_id: str = Field(
        min_length=1,
        description="Target work item ID.",
    ),
    target_project_id: str | None = Field(
        default=None,
        description="Defaults to the source's project.",
    ),
    suspect: bool | None = Field(
        default=None,
        description="New suspect flag; None = unchanged.",
    ),
    revision: str | None = Field(
        default=None,
        description="New revision pin; None = unchanged.",
    ),
    dry_run: bool = Field(
        default=False,
        description="Preview payload without calling Polarion.",
    ),
) -> WorkItemLinkUpdateResult:
    """Set suspect and/or revision on one existing outgoing link.

    Identify the link via list_work_item_links(direction="forward") (role +
    target address one link). None = unchanged; at least one of suspect /
    revision required. One link per call. A role typo 404s.
    """
    if suspect is None and revision is None:
        raise ValueError(
            "at least one of `suspect` / `revision` must be set;"
            " an all-None spec would produce an empty PATCH body."
        )

    spec = WorkItemLinkUpdateSpec(
        role=role,
        target_work_item_id=target_work_item_id,
        target_project_id=target_project_id,
        suspect=suspect,
        revision=revision,
    )
    link_id, path, payload = _build_update_link_payload(
        source_project_id=project_id,
        source_work_item_id=work_item_id,
        spec=spec,
    )

    if dry_run:
        return WorkItemLinkUpdateResult(
            updated=False,
            dry_run=True,
            link_id=link_id,
            payload_preview=payload,
        )

    client = get_client(ctx)
    try:
        await client.patch(path, json=cast(dict[str, object], payload))
    except PolarionAuthError as exc:
        raise PermissionError(
            "Cannot update work item link -- check your POLARION_TOKEN permissions."
        ) from exc
    except PolarionNotFoundError as exc:
        raise ValueError(f"Link not found (HTTP 404): {exc.message}") from exc
    except PolarionError as exc:
        raise RuntimeError(
            f"Patch failed (HTTP {exc.status_code}): {exc.message}"
        ) from exc

    return WorkItemLinkUpdateResult(
        updated=True,
        dry_run=False,
        link_id=link_id,
        payload_preview=None,
    )
