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
    """Return composite link ids verbatim, in input order, from a bulk
    create-link response. These are the path id for later PATCH / DELETE.
    Empty on malformed shapes (callers treat empty as failure).
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
    """Build the JSON:API body for bulk create-link POST.

    Each spec produces one ``{"type": "linkedworkitems", "attributes": ...,
    "relationships": ...}`` resource; all are sent in a single ``data``
    array. ``revision`` is skipped when unset so Polarion does not
    interpret an empty string as a clear-default. ``target_project_id``
    defaults to the source's project per spec when None.
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
    """Build the composite ids and JSON:API body for bulk delete-link DELETE.

    Polarion identifies each link by its five-segment composite id
    ``<srcProj>/<srcWI>/<role>/<tgtProj>/<tgtWI>``. We construct each id
    from the validated structured ref so the LLM never sees the raw
    composite form. Returns the id list (echoed in the result) and the
    JSON:API body to send.
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
    """Build the composite id, request path, and JSON:API body for one PATCH.

    Unlike POST/DELETE (server-side bulk on ``.../linkedworkitems``), PATCH
    is per-link on ``.../linkedworkitems/{role}/{tgtProj}/{tgtWI}`` with a
    single-resource ``{"data": {...}}`` body. ``suspect`` / ``revision``
    are only attached when explicitly set so JSON:API omit-preserve takes
    effect on the server side.

    Returning the request path here (instead of re-deriving it in the
    caller) keeps the composite id and the path string locked to the same
    ``tgt_proj`` resolution so the two cannot drift.
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
    """Parse linked work items from a JSON:API response.

    Uses ``attributes.role`` for the link role, ``attributes.suspect``
    for the suspect flag, and resolves the target work item title from
    the ``included`` array (populated via ``include=workItem``).

    The raw ID has the format::

        {projectId}/{sourceWiId}/{role}/{targetProjectId}/{targetWiId}

    e.g. ``MCP_Test_Project/MCPT-9/parent/MCP_Test_Project/MCPT-1``

    The target work item ID is extracted from
    ``relationships.workItem.data.id``.

    Args:
        response: Decoded JSON:API response from the linked items
            endpoint.
        direction: Link direction ('forward' or 'back').

    Returns:
        List of parsed ``WorkItemLink`` instances.
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
    work_item_id: str = Field(description="Work Item ID (e.g. 'MCPT-001')."),
    direction: Literal["forward", "back"] = "forward",
    page_size: int = Field(default=DEFAULT_PAGE_SIZE, ge=1, le=100),
    page_number: int = Field(default=1, ge=1),
) -> PaginatedResult[WorkItemLink]:
    """List a work item's outgoing or incoming links.

    One call returns one direction; call twice (``forward`` then ``back``) for
    both sides of the traceability graph. Forward links use ``/linkedworkitems``
    and expose the originating ``role`` (e.g. ``parent``, ``relates_to``,
    ``verifies``). Back links fall back to a ``linkedWorkItems:`` Lucene query
    that does not surface the role on this server, so ``role`` is ``None`` for
    every back item — recover it by calling forward on the source. The
    ``suspect`` flag (forward only) marks links whose target changed since last
    review.

    Args:
        ctx: MCP tool context (injected automatically).
        project_id: Polarion project ID.
        work_item_id: Work Item ID (e.g. 'MCPT-001').
        direction: 'forward' (outgoing) or 'back' (incoming). Default 'forward'.
        page_size: Items per page (1-100, default 100).
        page_number: 1-based page number (default 1).

    Returns:
        PaginatedResult of ``WorkItemLink`` items with ``id``, ``title``,
        ``role``, ``direction``, ``suspect``, ``type``, ``status``,
        ``space_id``, and ``document_name``.

    Raises:
        ValueError: Work item or project not found.
        PermissionError: Token lacks permission.
        RuntimeError: Other Polarion API errors.
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
        description="Source work item ID (the links' outgoing endpoint).",
    ),
    links: list[WorkItemLinkSpec] = Field(  # noqa: B008
        min_length=1,
        max_length=MAX_BULK_ITEMS,
        description="One or more links to create under the source work item (1-50).",
    ),
    dry_run: bool = Field(
        default=False,
        description="When True, return payload preview without calling Polarion.",
    ),
) -> WorkItemLinksCreateResult:
    """Create one or more outgoing links (1-50) from a single source work item.

    All links share the source (``project_id`` / ``work_item_id``) and post as
    one atomic bulk JSON:API request. Per spec, ``target_project_id`` defaults
    to ``project_id`` (set it only for cross-project links); ``revision`` pins
    the link to a revision (else HEAD); ``suspect`` marks it for re-review
    (usually False). Orientation matches
    ``list_work_item_links(direction="forward")`` on the source.

    Polarion validates neither role nor target, so the tool guards both before
    the POST (on ``dry_run=True`` too): each ``role`` is checked against the
    project's ``workitem-link-role`` enumeration (``ValueError`` listing the
    valid ids on a miss), and each target's existence is verified (a missing
    target would otherwise store as a silent dangling link with empty
    title/type/status -- ``ValueError`` on a miss).

    Returned ``link_ids`` are five-segment composites
    ``<srcProj>/<srcWI>/<role>/<tgtProj>/<tgtWI>`` in input order -- the path
    ids for later ``delete_work_item_links``. The POST is atomic: any Polarion
    rejection (e.g. a duplicate (role, target) pair -> HTTP 409) rolls back the
    whole batch, so a 4xx means nothing committed -- re-query
    ``list_work_item_links(direction="forward")`` before retrying. The tool also
    raises if a 2xx returns an id count differing from the number submitted.

    Phantom-success footgun: when the source is in a document,
    ``move_work_item_to_document`` already auto-created one outgoing link (role
    is project-config-dependent). Posting a NEW link with the SAME role returns
    201 and echoes the ``link_id`` but is NOT persisted -- the auto-link stays
    the only forward link, and there is no client-side error. After creating on
    an in-document source, verify with
    ``list_work_item_links(direction="forward")`` on the source and
    ``(direction="back")`` on the target; if missing, detach via
    ``move_work_item_from_document``, create the link, then re-attach with
    ``move_work_item_to_document``.

    Args:
        ctx: MCP tool context (injected automatically).
        project_id: Source work item's project ID.
        work_item_id: Source work item ID.
        links: One or more ``WorkItemLinkSpec`` (role + target + optional
            target_project_id / suspect / revision).
        dry_run: When True, return payload preview only.

    Returns:
        WorkItemLinksCreateResult with ``created``, ``dry_run``,
        ``link_ids`` (composite five-segment ids in input order; empty
        on dry-run), and ``payload_preview`` (populated on dry-run;
        None on real create).

    Raises:
        ValueError: Source project or work item not found, a target work
            item / target project that does not exist, or a ``role`` not in
            the project's ``workitem-link-role`` enumeration.
        PermissionError: Token lacks permission.
        RuntimeError: Other Polarion API errors (including duplicate link),
            an unreachable target- or role-validation request, or a
            returned-id count that does not match the number of links
            submitted.
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
        description="Source work item ID (the links' outgoing endpoint).",
    ),
    links: list[WorkItemLinkRef] = Field(  # noqa: B008
        min_length=1,
        max_length=MAX_BULK_ITEMS,
        description="One or more existing outgoing links to delete (1-50).",
    ),
    dry_run: bool = Field(
        default=False,
        description="When True, return payload preview without calling Polarion.",
    ),
) -> WorkItemLinksDeleteResult:
    """Delete one or more outgoing links (1-50) from a single source work item.

    Mirrors ``create_work_item_links``: same source coordinates, structured refs
    per target. Only **outgoing** ("forward") links are removed — delete a back
    link by calling this tool on the other work item. External hyperlinks live
    on ``hyperlinks`` (managed via ``update_work_item``).

    Identify links from a prior ``create_work_item_links`` (reuse the specs,
    dropping ``suspect`` / ``revision`` — delete needs only role + target) or
    from ``list_work_item_links(direction="forward")`` (each item's ``role`` +
    ``id`` form a ref; ``target_project_id`` defaults to ``project_id``).

    Polarion's delete is idempotent and silent — it removes matching refs,
    ignores the rest, and returns 204 either way, so a stale ref looks like a
    real delete. To make that visible the tool first reads the source's existing
    outgoing links (one paginated GET) and splits the request into
    ``deleted_link_ids`` (matched) and ``not_found_link_ids`` (no-ops); a no-op
    is reported, never raised, so re-deleting stays idempotent. The pre-read is
    fail-closed — an unreachable backend raises ``RuntimeError`` before any
    delete, so the split is always trustworthy when the call returns.

    Args:
        ctx: MCP tool context (injected automatically).
        project_id: Source work item's project ID.
        work_item_id: Source work item ID.
        links: One or more ``WorkItemLinkRef`` (role + target + optional
            target_project_id).
        dry_run: When True, return payload preview only. The pre-read still
            runs, so the preview's matched/no-op split is accurate.

    Returns:
        WorkItemLinksDeleteResult with ``deleted``, ``dry_run``,
        ``link_ids`` (every requested composite id, in input order),
        ``deleted_link_ids`` (refs that matched an existing link),
        ``not_found_link_ids`` (refs that matched nothing), and
        ``payload_preview`` (populated on dry-run; None on real delete).

    Raises:
        ValueError: Source work item itself not found.
        PermissionError: Token lacks permission.
        RuntimeError: An unreachable pre-read or other Polarion API error
            (e.g. 400 on a malformed composite id -- but this tool
            constructs valid ids from structured refs, so 400 should be
            unreachable).
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
            f"'{project_id}'. (Body-level 'link not found' is silently "
            "ignored by Polarion; this error means the source WI itself "
            "is missing. Use `list_work_items` to discover valid IDs.)"
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
        description="Source work item ID (the link's outgoing endpoint).",
    ),
    role: str = Field(min_length=1, description="Link role id of the existing link."),
    target_work_item_id: str = Field(
        min_length=1,
        description="Target work item ID (the link's incoming endpoint).",
    ),
    target_project_id: str | None = Field(
        default=None,
        description="Target's project; defaults to the source's project.",
    ),
    suspect: bool | None = Field(
        default=None,
        description="New suspect flag value; None leaves it unchanged.",
    ),
    revision: str | None = Field(
        default=None,
        description="New revision pin; None leaves the existing pin unchanged.",
    ),
    dry_run: bool = Field(
        default=False,
        description="When True, return payload preview without calling Polarion.",
    ),
) -> WorkItemLinkUpdateResult:
    """Update ``suspect`` / ``revision`` on one existing outgoing work item link.

    Use to clear a ``suspect`` flag after sign-off or pin a link to a revision.
    Identify the link first with ``list_work_item_links(direction="forward")``
    (its ``role`` + target id address one link). ``suspect`` / ``revision`` are
    tri-state: an explicit value updates that attribute, ``None`` (default)
    leaves it unchanged — at least one must be set (an all-``None`` PATCH 400s).
    One link per call: the PATCH endpoint has no bulk equivalent. A typo in
    ``role`` returns 404 (no link exists under that role).

    Args:
        ctx: MCP tool context (injected automatically).
        project_id: Source work item's project ID.
        work_item_id: Source work item ID.
        role: Link role id of the existing link (e.g. ``'relates_to'``).
        target_work_item_id: Target work item ID.
        target_project_id: Target's project; defaults to ``project_id``.
        suspect: New suspect flag; None leaves it unchanged.
        revision: New revision pin; None leaves it unchanged.
        dry_run: When True, return payload preview only.

    Returns:
        WorkItemLinkUpdateResult with ``updated`` (True on success),
        ``dry_run``, ``link_id`` (composite 5-segment id computed from
        inputs), and ``payload_preview`` (PATCH body on dry-run; None
        after a real call).

    Raises:
        ValueError: Both ``suspect`` and ``revision`` are None, or the
            link was not found (HTTP 404).
        PermissionError: Token lacks permission.
        RuntimeError: Polarion returned an unexpected HTTP error.
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
