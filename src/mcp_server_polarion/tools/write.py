"""Write MCP tools for Polarion ALM.

All write tools follow the same patterns: greenfield Markdown bodies are
converted to sanitized HTML before send, round-trip HTML bodies pass
through verbatim, request payloads skip unset fields rather than sending
empty values, and domain exceptions are mapped to user-facing ones at
the tool layer.
"""

from __future__ import annotations

import copy
import logging
from typing import Final, cast
from urllib.parse import urlencode

from fastmcp import Context
from pydantic import Field

from mcp_server_polarion.core.exceptions import (
    PolarionAuthError,
    PolarionError,
    PolarionNotFoundError,
)
from mcp_server_polarion.models import (
    MAX_BODY_HTML_LEN,
    DocumentCommentsCreateResult,
    DocumentCommentSpec,
    DocumentCommentUpdateResult,
    DocumentCreateResult,
    DocumentUpdateResult,
    Hyperlink,
    JsonValue,
    WorkItemCreateSpec,
    WorkItemLinkRef,
    WorkItemLinksCreateResult,
    WorkItemLinksDeleteResult,
    WorkItemLinkSpec,
    WorkItemLinkUpdateResult,
    WorkItemLinkUpdateSpec,
    WorkItemMoveResult,
    WorkItemsCreateResult,
    WorkItemUpdateResult,
)
from mcp_server_polarion.server import mcp
from mcp_server_polarion.tools._cache import (
    invalidate_documents_cache,
    record_work_item_custom_field_keys,
)
from mcp_server_polarion.tools._guard import (
    guard_document_custom_field_keys,
    guard_document_enums,
    guard_hyperlink_roles,
    guard_work_item_custom_field_keys,
    guard_work_item_enums,
    guard_work_item_link_roles,
    guard_work_item_link_targets,
    partition_delete_links,
)
from mcp_server_polarion.tools._helpers import (
    STANDARD_DOCUMENT_ATTRIBUTES,
    STANDARD_WORK_ITEM_ATTRIBUTES,
    WORK_ITEM_DETAIL_FIELDS,
    encode_path_segment,
    extract_short_id,
    get_client,
    merge_custom_fields,
    parse_work_item_detail,
    safe_str,
    split_module_id,
)
from mcp_server_polarion.utils import (
    first_anchorless_block,
    markdown_to_html,
    sanitize_html,
    stamp_block_ids,
)

logger = logging.getLogger("mcp_server_polarion.tools.write")

# Caps how many items a single bulk write may carry. Polarion allows no
# concurrent requests and throttles at ~3 req/s, so an unbounded batch is a
# rate-limit and payload-size hazard; 50 bounds one request without forcing
# callers to paginate typical work. Shared by every bulk write tool.
MAX_BULK_ITEMS: Final[int] = 50


def _build_work_item_resource(
    *,
    spec: WorkItemCreateSpec,
    description_html: str,
) -> dict[str, JsonValue]:
    """Build one ``workitems`` resource object for a bulk create POST.

    Skips unset values so creation never overwrites Polarion defaults with
    empties. ``custom_fields`` inline into ``attributes`` via
    ``merge_custom_fields``, which raises on a key colliding with a standard
    field. ``description_html`` arrives pre-converted (pure data-shaping).
    """
    attributes: dict[str, JsonValue] = {
        "title": spec.title,
        "type": spec.type,
    }
    if description_html:
        attributes["description"] = {
            "type": "text/html",
            "value": description_html,
        }
    if spec.status:
        attributes["status"] = spec.status
    if spec.priority:
        attributes["priority"] = spec.priority
    if spec.severity:
        attributes["severity"] = spec.severity
    if spec.due_date:
        attributes["dueDate"] = spec.due_date
    if spec.initial_estimate:
        attributes["initialEstimate"] = spec.initial_estimate
    if spec.hyperlinks:
        attributes["hyperlinks"] = [
            {"role": h.role, "title": h.title, "uri": h.uri} for h in spec.hyperlinks
        ]
    merge_custom_fields(attributes, spec.custom_fields, STANDARD_WORK_ITEM_ATTRIBUTES)

    relationships: dict[str, JsonValue] = {}
    if spec.assignee_ids:
        relationships["assignee"] = {
            "data": [{"type": "users", "id": uid} for uid in spec.assignee_ids]
        }

    resource: dict[str, JsonValue] = {
        "type": "workitems",
        "attributes": attributes,
    }
    if relationships:
        resource["relationships"] = relationships

    return resource


def _build_create_work_items_payload(
    *,
    specs: list[WorkItemCreateSpec],
    descriptions_html: list[str],
) -> dict[str, JsonValue]:
    """Build the JSON:API body for the bulk ``POST /projects/{p}/workitems``.

    Each spec, paired with its pre-converted ``description_html``, produces
    one resource object via ``_build_work_item_resource``; all are sent in a
    single ``data`` array so N work items create in one request.
    """
    data: list[JsonValue] = [
        _build_work_item_resource(spec=spec, description_html=html)
        for spec, html in zip(specs, descriptions_html, strict=True)
    ]
    return {"data": data}


def _extract_first_resource_id(response: dict[str, object]) -> str | None:
    """Pull the first resource ID out of a JSON:API ``{"data": [...]}`` body.

    Returns ``None`` when ``data`` is missing, not a non-empty list, or its
    first entry has no ``id`` string.
    """
    data = response.get("data")
    if not isinstance(data, list) or not data:
        return None
    first = data[0]
    if not isinstance(first, dict):
        return None
    full_id = safe_str(first.get("id", ""))
    return full_id or None


def _extract_created_work_item_ids(response: dict[str, object]) -> list[str]:
    """Return short work-item ids in submission order from a bulk 201 response.

    Relies on Polarion echoing ``data`` in submission order; the call-site
    count check catches a missing id, not a reordered one. Empty on malformed
    shapes.
    """
    data = response.get("data")
    if not isinstance(data, list):
        return []
    ids: list[str] = []
    for item in data:
        if isinstance(item, dict):
            full_id = safe_str(item.get("id", ""))
            if full_id:
                ids.append(extract_short_id(full_id))
    return ids


def _extract_created_module_name(response: dict[str, object]) -> str | None:
    """Extract the document name from a 201 document-create response.

    Polarion returns ``{"data": [{"type": "documents",
    "id": "projectId/spaceId/documentName", ...}]}`` where
    ``documentName`` itself may contain ``/``. Returns the document-name
    segment or ``None`` if the response shape is unexpected.
    """
    full_id = _extract_first_resource_id(response)
    if full_id is None:
        return None
    _, document_name = split_module_id(full_id)
    return document_name or None


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


def _build_update_work_item_payload(  # noqa: PLR0913
    *,
    project_id: str,
    work_item_id: str,
    title: str | None,
    description_html: str | None,
    status: str | None,
    priority: str | None,
    severity: str | None,
    due_date: str | None,
    initial_estimate: str | None,
    resolution: str | None,
    hyperlinks: list[Hyperlink] | None,
    assignee_ids: list[str] | None,
    custom_fields: dict[str, object] | None = None,
) -> dict[str, JsonValue]:
    """Build the JSON:API PATCH body for ``/projects/{p}/workitems/{work_item}``.

    The PATCH shape of ``_build_work_item_resource``: a single ``data``
    resource object with a required ``id`` ``"{project_id}/{work_item_id}"``.
    Skips unset values so an update never blanks an existing attribute.
    """
    attributes: dict[str, JsonValue] = {}
    if title:
        attributes["title"] = title
    if description_html:
        attributes["description"] = {
            "type": "text/html",
            "value": description_html,
        }
    if status:
        attributes["status"] = status
    if priority:
        attributes["priority"] = priority
    if severity:
        attributes["severity"] = severity
    if due_date:
        attributes["dueDate"] = due_date
    if initial_estimate:
        attributes["initialEstimate"] = initial_estimate
    if resolution:
        attributes["resolution"] = resolution
    if hyperlinks:
        attributes["hyperlinks"] = [
            {"role": h.role, "title": h.title, "uri": h.uri} for h in hyperlinks
        ]
    merge_custom_fields(attributes, custom_fields, STANDARD_WORK_ITEM_ATTRIBUTES)

    relationships: dict[str, JsonValue] = {}
    if assignee_ids:
        relationships["assignee"] = {
            "data": [{"type": "users", "id": uid} for uid in assignee_ids]
        }

    item: dict[str, JsonValue] = {
        "type": "workitems",
        "id": f"{project_id}/{work_item_id}",
    }
    if attributes:
        item["attributes"] = attributes
    if relationships:
        item["relationships"] = relationships

    return {"data": item}


def _build_move_to_document_payload(
    *,
    project_id: str,
    target_space_id: str,
    target_document_name: str,
    previous_part_id: str | None,
    next_part_id: str | None,
) -> dict[str, JsonValue]:
    """Build the request body for the ``moveToDocument`` action endpoint.

    NOT JSON:API — a flat object with ``targetDocument`` plus at most one of
    ``previousPart`` / ``nextPart`` (both omitted = append at end). The
    at-most-one invariant is re-checked here to fail closed for direct callers.
    """
    if previous_part_id is not None and next_part_id is not None:
        msg = (
            "_build_move_to_document_payload accepts at most one of "
            "previous_part_id or next_part_id; both being set is rejected "
            "by Polarion."
        )
        raise ValueError(msg)

    target_doc = f"{project_id}/{target_space_id}/{target_document_name}"
    payload: dict[str, JsonValue] = {"targetDocument": target_doc}
    if previous_part_id is not None:
        payload["previousPart"] = f"{target_doc}/{previous_part_id}"
    elif next_part_id is not None:
        payload["nextPart"] = f"{target_doc}/{next_part_id}"
    return payload


def _build_update_document_payload(  # noqa: PLR0913
    *,
    project_id: str,
    space_id: str,
    document_name: str,
    title: str | None,
    status: str | None,
    type: str | None,
    home_page_content_html: str | None = None,
    custom_fields: dict[str, object] | None = None,
) -> dict[str, JsonValue]:
    """Build the JSON:API request body for ``PATCH .../documents/{d}``.

    Same PATCH shape as ``_build_update_work_item_payload`` (single ``data``
    object, required ``id`` ``"{project_id}/{space_id}/{document_name}"``),
    skipping unset values. ``home_page_content_html`` is RAW Polarion HTML
    wrapped verbatim into ``{"type":"text/html","value":...}`` — the
    empty-string body-clearing guard lives in the tool layer, not here.
    """
    attributes: dict[str, JsonValue] = {}
    if title is not None:
        attributes["title"] = title
    if status is not None:
        attributes["status"] = status
    if type is not None:
        attributes["type"] = type
    if home_page_content_html is not None:
        attributes["homePageContent"] = {
            "type": "text/html",
            "value": home_page_content_html,
        }
    merge_custom_fields(attributes, custom_fields, STANDARD_DOCUMENT_ATTRIBUTES)

    item: dict[str, JsonValue] = {
        "type": "documents",
        "id": f"{project_id}/{space_id}/{document_name}",
    }
    if attributes:
        item["attributes"] = attributes

    return {"data": item}


def _build_create_document_payload(  # noqa: PLR0913
    *,
    module_name: str,
    title: str,
    type: str,
    home_page_content_html: str,
    status: str | None,
    custom_fields: dict[str, object] | None = None,
) -> dict[str, JsonValue]:
    """Build the JSON:API request body for ``POST .../spaces/{s}/documents``.

    POST shape (``data`` list, ``type=documents``, inline ``attributes``),
    skipping unset values so creation never overwrites Polarion defaults.
    """
    attributes: dict[str, JsonValue] = {
        "moduleName": module_name,
        "title": title,
        "type": type,
    }
    if status:
        attributes["status"] = status
    if home_page_content_html:
        attributes["homePageContent"] = {
            "type": "text/html",
            "value": home_page_content_html,
        }
    merge_custom_fields(attributes, custom_fields, STANDARD_DOCUMENT_ATTRIBUTES)

    item: dict[str, JsonValue] = {
        "type": "documents",
        "attributes": attributes,
    }
    return {"data": [item]}


def _build_document_comments_payload(
    *,
    specs: list[DocumentCommentSpec],
    project_id: str,
    space_id: str,
    document_name: str,
) -> dict[str, JsonValue]:
    """Build the JSON:API request body for POST .../documents/{d}/comments.

    Builds one resource object per spec. Only attaches ``resolved`` when
    explicitly set (None = omit). ``author`` and ``parentComment``
    relationships are omitted when None. ``parent_comment_id`` is
    composed into the full path form ``proj/space/doc/commentId``
    required by the Polarion API, so callers pass the short ID from
    ``list_document_comments``.
    """
    items: list[JsonValue] = []
    for spec in specs:
        attributes: dict[str, JsonValue] = {
            "text": {"type": spec.text_format, "value": spec.text},
        }
        if spec.resolved is not None:
            attributes["resolved"] = spec.resolved

        relationships: dict[str, JsonValue] = {}
        if spec.author_id is not None:
            relationships["author"] = {"data": {"id": spec.author_id, "type": "users"}}
        if spec.parent_comment_id is not None:
            full_parent = (
                f"{project_id}/{space_id}/{document_name}/{spec.parent_comment_id}"
            )
            relationships["parentComment"] = {
                "data": {"id": full_parent, "type": "document_comments"}
            }

        item: dict[str, JsonValue] = {
            "type": "document_comments",
            "attributes": attributes,
        }
        if relationships:
            item["relationships"] = relationships
        items.append(item)

    return {"data": items}


def _build_document_comment_update_payload(
    *,
    project_id: str,
    space_id: str,
    document_name: str,
    comment_id: str,
    resolved: bool,
) -> dict[str, JsonValue]:
    """Build the JSON:API PATCH body for one document comment.

    Produces a single-resource ``{"data": {...}}`` object (NOT a list — that
    is the POST/create shape). The ``id`` field is the full 4-segment path
    ``{project_id}/{space_id}/{document_name}/{comment_id}`` required by the
    Polarion PATCH endpoint. Only ``resolved`` is patchable per the API.
    """
    full_id = f"{project_id}/{space_id}/{document_name}/{comment_id}"
    return {
        "data": {
            "type": "document_comments",
            "id": full_id,
            "attributes": {
                "resolved": resolved,
            },
        }
    }


@mcp.tool(
    tags={"write"},
    timeout=60.0,
    annotations={
        # Additive: non-destructive, but non-idempotent (a retry duplicates).
        "readOnlyHint": False,
        "destructiveHint": False,
        "idempotentHint": False,
        "openWorldHint": True,
    },
)
async def create_work_items(
    ctx: Context,
    project_id: str = Field(min_length=1, description="Polarion project ID."),
    items: list[WorkItemCreateSpec] = Field(  # noqa: B008
        min_length=1,
        max_length=MAX_BULK_ITEMS,
        description=(
            "One or more work items to create in a single request "
            "(1-50). Pass a single-element list to create just one."
        ),
    ),
    dry_run: bool = Field(
        default=False,
        description=(
            "When True, return the payload preview without writing; the enum "
            "guard still calls Polarion's getAvailableOptions, so that "
            "endpoint must be reachable."
        ),
    ),
) -> WorkItemsCreateResult:
    """Create one or more Polarion work items (1-50) in a single request, all
    in the same project.

    For every enum-valued field on any item (``type``, ``status``,
    ``severity``, ``custom_fields`` enum entries) you MUST first confirm the
    value via ``list_work_item_enum_options(project_id, field_id,
    work_item_type)`` — unverified ids are accepted by Polarion but persist as
    ghosts that never match Lucene. ``priority`` partly coerces (non-numeric →
    project default; numeric out-of-range still persists verbatim).
    ``custom_fields`` keys are unvalidated and defined per project+type, so
    take them from an existing work item of the same ``type`` via
    ``get_work_item``. Each ``hyperlinks[].role`` is validated against the
    project's ``hyperlink-role`` enumeration (``ValueError`` on an unknown role
    before any write).

    Bulk semantics: enum guards, Markdown conversion, and payload build all run
    BEFORE any write, and Polarion's bulk POST is atomic — a bad enum, colliding
    custom-field key, or any server-rejected attribute on one item rejects the
    whole batch with nothing created. The tool raises if the returned id count
    differs from the number submitted; re-query ``list_work_items`` first.

    Each item is created free-floating — to place one in a document at an
    outline position, follow up with ``move_work_item_to_document``. Direct
    creation into a document (via ``module``) is intentionally not exposed: such
    items land in the recycle bin, invisible in the body. Always pair
    create + move.

    Format asymmetry: ``description`` is Markdown (converted to sanitized HTML
    on write); after creation the round-trip pair is
    ``get_work_item(include_description_html=True)`` ↔
    ``update_work_item(description_html=...)`` (raw HTML verbatim). The two
    formats never mix.

    Args:
        ctx: MCP tool context (injected automatically).
        project_id: Polarion project ID shared by every item.
        items: One or more ``WorkItemCreateSpec`` entries (1-50).
        dry_run: When True, return payload preview only.

    Returns:
        WorkItemsCreateResult with ``created``, ``dry_run``,
        ``work_item_ids`` (short ids in input order; empty on dry-run),
        and ``payload_preview`` (populated on dry-run; None on real create).

    Raises:
        ValueError: Project not found, or a custom-field key collides with
            a standard Polarion attribute.
        PermissionError: Token lacks permission.
        RuntimeError: Other Polarion API errors, or a returned-id count
            that does not match the number of items submitted.
    """
    client = get_client(ctx)
    for spec in items:
        await guard_work_item_enums(
            client,
            project_id,
            work_item_type=spec.type,
            type=spec.type,
            status=spec.status,
            severity=spec.severity,
            priority=spec.priority,
        )
    await guard_hyperlink_roles(
        client,
        project_id,
        [h.role for spec in items for h in (spec.hyperlinks or [])],
    )
    for index, spec in enumerate(items):
        if spec.custom_fields:
            # Create can't hard-guard keys (no project-config endpoint, no prior
            # item to observe) the way update_work_item does — warn instead.
            logger.warning(
                "create_work_items[%d].custom_fields cannot be schema-validated "
                "(no project-config endpoint for custom-field keys); "
                "ensure keys come from an existing work item of this type via "
                "get_work_item to avoid ghost attributes. "
                "project=%s type=%s keys=%s",
                index,
                project_id,
                spec.type,
                sorted(spec.custom_fields),
            )

    descriptions_html = [
        sanitize_html(markdown_to_html(spec.description)) if spec.description else ""
        for spec in items
    ]

    payload = _build_create_work_items_payload(
        specs=items,
        descriptions_html=descriptions_html,
    )

    if dry_run:
        return WorkItemsCreateResult(
            created=False,
            dry_run=True,
            work_item_ids=[],
            payload_preview=payload,
        )

    path = f"/projects/{encode_path_segment(project_id)}/workitems"
    try:
        response = await client.post(path, json=cast(dict[str, object], payload))
    except PolarionAuthError as exc:
        raise PermissionError(
            "Cannot create work items -- check your POLARION_TOKEN permissions."
        ) from exc
    except PolarionNotFoundError as exc:
        raise ValueError(
            f"Project '{project_id}' not found. "
            "Use `list_projects` to discover valid project IDs."
        ) from exc
    except PolarionError as exc:
        raise RuntimeError(f"Failed to create work items: {exc.message}") from exc

    new_ids = _extract_created_work_item_ids(response)
    if len(new_ids) != len(items):
        raise RuntimeError(
            f"Polarion accepted the bulk create but returned {len(new_ids)} "
            f"ids for {len(items)} requested items. The batch may be partially "
            "created; verify with list_work_items before retrying."
        )

    return WorkItemsCreateResult(
        created=True,
        dry_run=False,
        work_item_ids=new_ids,
        payload_preview=None,
    )


@mcp.tool(
    tags={"write"},
    timeout=60.0,
    annotations={
        "readOnlyHint": False,
        "destructiveHint": True,
        "idempotentHint": True,
        "openWorldHint": True,
    },
)
async def update_work_item(  # noqa: PLR0912, PLR0913, PLR0915
    ctx: Context,
    project_id: str = Field(min_length=1, description="Polarion project ID."),
    work_item_id: str = Field(
        min_length=1,
        description="Short ID of an EXISTING work item (e.g. 'MCPT-042').",
    ),
    title: str | None = None,
    description_html: str | None = Field(
        default=None,
        max_length=MAX_BODY_HTML_LEN,
        description="New raw Polarion HTML body (round-trip shape from get_work_item).",
    ),
    status: str | None = Field(
        default=None,
        description=(
            "New workflow status; prefer ``workflow_action`` for real transitions."
        ),
    ),
    priority: str | None = Field(
        default=None,
        description="New priority string (e.g. '50.0').",
    ),
    severity: str | None = None,
    due_date: str | None = Field(
        default=None, description="New due date 'YYYY-MM-DD'."
    ),
    initial_estimate: str | None = Field(
        default=None,
        description="New Polarion duration (e.g. '5 1/2d', '1w 2d').",
    ),
    resolution: str | None = Field(
        default=None,
        description=(
            "New resolution outcome; "
            "prefer ``workflow_action`` so workflow rules apply."
        ),
    ),
    hyperlinks: list[Hyperlink] | None = Field(  # noqa: B008
        default=None,
        description="REPLACES the hyperlink list — pass the full list, not a delta.",
    ),
    assignee_ids: list[str] | None = Field(  # noqa: B008
        default=None,
        description="REPLACES the assignee list — pass the full list, not a delta.",
    ),
    custom_fields: dict[str, object] | None = Field(  # noqa: B008
        default=None,
        description=(
            "Partial custom-field update; "
            "rich-text values must be ``{'type':'text/html','value':...}``."
        ),
    ),
    workflow_action: str | None = Field(
        default=None,
        description=(
            "Workflow action ID (e.g. 'close'); "
            "must be paired with at least one body field."
        ),
    ),
    change_type_to: str | None = Field(
        default=None,
        description=(
            "Change work-item type; RESETS status; "
            "must be paired with at least one body field."
        ),
    ),
    include_current_description_html: bool = Field(
        default=False,
        description=(
            "When True, return the post-PATCH raw HTML body in "
            "``current.description_html``."
        ),
    ),
    dry_run: bool = Field(
        default=False,
        description=(
            "When True, return the payload preview without writing; "
            "enum/custom-field guards still query Polarion, so the work item "
            "must be readable and the validation endpoint reachable."
        ),
    ),
) -> WorkItemUpdateResult:
    """Update an existing Polarion work item.

    PATCHes the supplied fields and follows up with a GET so the caller can
    confirm the change in ``current``. ``None`` / empty string / empty list all
    mean ``leave unchanged`` — there is no path to clear a field.

    ``description_html`` is RAW Polarion HTML, sent verbatim with no
    sanitization (XSS filtering is Polarion's job — NEVER pass untrusted input);
    pair with ``get_work_item(include_description_html=True)`` for the
    round-trip. For greenfield authoring use ``create_work_items`` with a
    Markdown ``description`` — the two paths never mix.

    ``hyperlinks`` and ``assignee_ids`` REPLACE the existing lists (pass the
    full list, not a delta). Each hyperlink ``role`` is validated against the
    project's ``hyperlink-role`` enumeration (``ValueError`` before the PATCH,
    on ``dry_run`` too, since an unknown role would persist as a ghost).
    ``custom_fields`` is partial (omitted keys preserved); unknown keys are
    rejected unless seen on a prior ``get_work_item`` for this type (one priming
    read on a miss) — otherwise they would persist as ghost attributes.

    The ``module`` relationship is NOT exposed (Polarion rejects PATCHes that
    touch it) — attach/detach/move via ``move_work_item_to_document`` /
    ``move_work_item_from_document``.

    Prefer ``workflow_action`` over a raw ``status`` edit so transition rules
    run. ``workflow_action`` and ``change_type_to`` MUST be paired with at least
    one body field (Polarion rejects empty PATCH bodies). Unknown ``status`` /
    ``severity`` / ``resolution`` / ``priority`` / ``change_type_to`` ids raise
    ``ValueError`` listing the valid options before the PATCH (on ``dry_run``
    too); with ``change_type_to`` set, ``status`` / ``severity`` /
    ``resolution`` are validated against the target type's options.

    Args:
        ctx: MCP tool context (injected automatically).
        project_id: Polarion project ID.
        work_item_id: Short ID of the work item to update.
        title: Optional new title.
        description_html: Optional new raw HTML body.
        status: Optional new workflow status.
        priority: Optional new priority string.
        severity: Optional new severity.
        due_date: Optional new ISO-8601 date.
        initial_estimate: Optional new duration.
        resolution: Optional new resolution outcome.
        hyperlinks: Optional REPLACEMENT hyperlink list.
        assignee_ids: Optional REPLACEMENT assignee list.
        custom_fields: Optional partial custom-field update.
        workflow_action: Optional ``workflowAction`` query parameter.
        change_type_to: Optional ``changeTypeTo`` query parameter.
        include_current_description_html: When True, return the
            post-update body in ``current.description_html``; default
            False keeps responses small.
        dry_run: When True, return payload preview only.

    Returns:
        WorkItemUpdateResult with ``updated``, ``dry_run``, ``current``
        (post-update detail; None on dry-run), ``changes`` (parameter
        deltas), and ``payload_preview`` (populated on dry-run).

    Raises:
        ValueError: No mutating fields supplied, action without body,
            custom-field key collides with a standard attribute, or work item
            not found.
        PermissionError: Token lacks permission.
        RuntimeError: Other Polarion API errors.
    """
    changes: dict[str, JsonValue] = {}
    if title:
        changes["title"] = title
    if description_html:
        changes["description_html"] = description_html
    if status:
        changes["status"] = status
    if priority:
        changes["priority"] = priority
    if severity:
        changes["severity"] = severity
    if due_date:
        changes["due_date"] = due_date
    if initial_estimate:
        changes["initial_estimate"] = initial_estimate
    if resolution:
        changes["resolution"] = resolution
    if hyperlinks:
        changes["hyperlinks"] = [
            {"role": h.role, "title": h.title, "uri": h.uri} for h in hyperlinks
        ]
    if assignee_ids:
        changes["assignee_ids"] = list(assignee_ids)
    if custom_fields:
        # deepcopy so the result's ``changes`` map is independent of the caller's
        # dict; a shallow copy would alias nested rich-text values.
        changes["custom_fields"] = cast(JsonValue, copy.deepcopy(custom_fields))
    if workflow_action:
        changes["workflow_action"] = workflow_action
    if change_type_to:
        changes["change_type_to"] = change_type_to

    if not changes:
        raise ValueError(
            "Nothing to update -- pass at least one of title, "
            "description_html, status, priority, severity, due_date, "
            "initial_estimate, resolution, hyperlinks, assignee_ids, "
            "custom_fields, workflow_action, or change_type_to."
        )

    payload = _build_update_work_item_payload(
        project_id=project_id,
        work_item_id=work_item_id,
        title=title,
        description_html=description_html,
        status=status,
        priority=priority,
        severity=severity,
        due_date=due_date,
        initial_estimate=initial_estimate,
        resolution=resolution,
        hyperlinks=hyperlinks,
        assignee_ids=assignee_ids,
        custom_fields=custom_fields,
    )

    # Polarion 400s on a PATCH body with neither attributes nor relationships,
    # even when only workflowAction / changeTypeTo is set — catch it here.
    payload_data = cast(dict[str, JsonValue], payload["data"])
    if "attributes" not in payload_data and "relationships" not in payload_data:
        raise ValueError(
            "Polarion's PATCH endpoint requires at least one body field "
            "(attribute or relationship) even when triggering "
            "workflow_action or change_type_to. Pair the action with one "
            "of: title, description, status, priority, severity, due_date, "
            "initial_estimate, resolution, hyperlinks, or assignee_ids."
        )

    client = get_client(ctx)
    base_path = (
        f"/projects/{encode_path_segment(project_id)}"
        f"/workitems/{encode_path_segment(work_item_id)}"
    )

    # Enum/custom-field args need the item's type (enum options are
    # type-scoped) and prime the custom-key guard cache, so fetch once —
    # on dry_run too, so preview raises the same ValueError as a real call.
    work_item_type = ""
    if status or severity or priority or resolution or change_type_to or custom_fields:
        try:
            prefetch = await client.get(
                base_path,
                params={"fields[workitems]": "@all"},
            )
        except PolarionNotFoundError as exc:
            raise ValueError(
                f"Work item '{work_item_id}' in project '{project_id}' not found. "
                "Use `list_work_items` to discover valid IDs."
            ) from exc
        except PolarionAuthError as exc:
            raise PermissionError(
                "Cannot read work item -- check your POLARION_TOKEN permissions."
            ) from exc
        except PolarionError as exc:
            raise RuntimeError(
                f"Failed to read work item for guard: {exc.message}"
            ) from exc
        prefetch_data = prefetch.get("data", {})
        if isinstance(prefetch_data, dict):
            current_detail = parse_work_item_detail(
                prefetch_data,
                project_id=project_id,
                fallback_id=work_item_id,
            )
            work_item_type = current_detail.type
            if work_item_type:
                record_work_item_custom_field_keys(
                    project_id,
                    work_item_type,
                    current_detail.custom_fields.keys(),
                )

        # Scope status/severity/resolution/priority by the target type
        # (``change_type_to`` if set, since the patch lands there). The guard
        # checks ``type`` first, so an invalid change_type_to raises before
        # it is reused as the scoping axis.
        effective_type = change_type_to or work_item_type or "~"
        await guard_work_item_enums(
            client,
            project_id,
            work_item_type=effective_type,
            type=change_type_to,
            status=status,
            severity=severity,
            priority=priority,
            resolution=resolution,
        )
        # Fail-closed even when the prefetch could not resolve a type: pass
        # whatever type we have (possibly "") so the guard's own priming GET
        # validates the keys rather than silently skipping the check.
        if custom_fields:
            await guard_work_item_custom_field_keys(
                client,
                project_id,
                work_item_id,
                work_item_type,
                custom_fields,
            )

    if hyperlinks:
        await guard_hyperlink_roles(client, project_id, [h.role for h in hyperlinks])

    if dry_run:
        return WorkItemUpdateResult(
            updated=False,
            dry_run=True,
            current=None,
            changes=changes,
            payload_preview=payload,
        )

    query_params: dict[str, str] = {}
    if workflow_action:
        query_params["workflowAction"] = workflow_action
    if change_type_to:
        query_params["changeTypeTo"] = change_type_to
    patch_path = f"{base_path}?{urlencode(query_params)}" if query_params else base_path

    try:
        await client.patch(patch_path, json=cast(dict[str, object], payload))
        response = await client.get(
            base_path,
            params={
                "fields[workitems]": WORK_ITEM_DETAIL_FIELDS,
                "include": "assignee",
            },
        )
    except PolarionAuthError as exc:
        raise PermissionError(
            "Cannot update work item -- check your POLARION_TOKEN permissions."
        ) from exc
    except PolarionNotFoundError as exc:
        raise ValueError(
            f"Work item '{work_item_id}' in project '{project_id}' not found. "
            "Use `list_work_items` to discover valid IDs."
        ) from exc
    except PolarionError as exc:
        raise RuntimeError(f"Failed to update work item: {exc.message}") from exc

    data = response.get("data", {})
    if not isinstance(data, dict):
        data = {}
    current = parse_work_item_detail(
        data,
        project_id=project_id,
        fallback_id=work_item_id,
    )
    if not include_current_description_html:
        # Blank the returned body (it still came over the wire) to keep the
        # common metadata-only update small — mirrors get_work_item.
        current = current.model_copy(update={"description_html": ""})

    return WorkItemUpdateResult(
        updated=True,
        dry_run=False,
        current=current,
        changes=changes,
        payload_preview=None,
    )


@mcp.tool(
    tags={"write"},
    timeout=60.0,
    annotations={
        # Non-idempotent: re-moving an already-moved item may 400, not no-op.
        "readOnlyHint": False,
        "destructiveHint": True,
        "idempotentHint": False,
        "openWorldHint": True,
    },
)
async def move_work_item_to_document(  # noqa: PLR0913
    ctx: Context,
    project_id: str = Field(description="Project containing the work item."),
    work_item_id: str = Field(
        min_length=1,
        description="Short ID of an EXISTING work item (e.g. 'MCPT-042').",
    ),
    target_space_id: str = Field(
        min_length=1,
        description="Target space ID (use '_default' for the default space).",
    ),
    target_document_name: str = Field(
        min_length=1,
        description="Target document name within ``target_space_id``.",
    ),
    previous_part_id: str | None = Field(
        default=None,
        description=(
            "Insert AFTER this part ID; mutually exclusive with ``next_part_id``. "
            "Omit both to append at the end of the target document."
        ),
    ),
    next_part_id: str | None = Field(
        default=None,
        description=(
            "Insert BEFORE this part ID; mutually exclusive with ``previous_part_id``. "
            "Omit both to append at the end of the target document."
        ),
    ),
    dry_run: bool = Field(
        default=False,
        description="When True, return payload preview without calling Polarion.",
    ),
) -> WorkItemMoveResult:
    """Move a work item into a Polarion document at a specific position.

    Calls the ``moveToDocument`` action endpoint, which atomically updates the
    work item's ``module`` relationship, inserts a document part at the
    position, and assigns an ``outline_number``. This is the ONLY supported way
    to attach a work item body to a document — editing ``homePageContent`` to
    inject a macro reference leaves ``module`` unset, an inconsistent state.

    Heading-type work items are rejected (HTTP 400) — headings must be created
    inside their target document. If the item is already in another document
    this detaches it from the source (a move, not a copy); to detach back to
    free-floating use ``move_work_item_from_document`` (``module`` cannot be
    cleared via PATCH).

    Provide AT MOST one of ``previous_part_id`` (insert AFTER) / ``next_part_id``
    (insert BEFORE); omit both to append at the end. Discover part IDs with
    ``read_document_parts``.

    Side effect: the server auto-creates an outgoing link from the moved item to
    its enclosing heading. The role is project-configurable (commonly
    ``parent`` / ``relates_to``) — inspect forward links on a known-attached
    item to find it. It appears in ``list_work_item_links(direction="forward")``
    after the move, is silently removed by ``move_work_item_from_document``, and
    collides with any same-role link from the same source (see the "phantom
    success" note on ``create_work_item_links``).

    Args:
        ctx: MCP tool context (injected automatically).
        project_id: Project containing the work item.
        work_item_id: Short ID of an existing work item.
        target_space_id: Target space ID.
        target_document_name: Target document name.
        previous_part_id: Insert AFTER this part.
        next_part_id: Insert BEFORE this part.
        dry_run: When True, return payload preview only.

    Returns:
        WorkItemMoveResult with ``moved``, ``dry_run``, and
        ``payload_preview`` (populated on dry-run). Polarion returns 204
        on success — call ``read_document_parts`` for the new part ID.

    Raises:
        ValueError: Both positions supplied, heading work item, or
            work item / document / part not found.
        PermissionError: Token lacks permission.
        RuntimeError: Other Polarion API errors.
    """
    if previous_part_id is not None and next_part_id is not None:
        raise ValueError(
            "Provide at most one of previous_part_id or next_part_id. "
            "Use previous_part_id to insert AFTER an existing part, or "
            "next_part_id to insert BEFORE; omit both to append at the "
            "end of the target document. Discover existing part IDs with "
            "`read_document_parts`."
        )

    payload = _build_move_to_document_payload(
        project_id=project_id,
        target_space_id=target_space_id,
        target_document_name=target_document_name,
        previous_part_id=previous_part_id,
        next_part_id=next_part_id,
    )

    if dry_run:
        return WorkItemMoveResult(
            moved=False,
            dry_run=True,
            payload_preview=payload,
        )

    client = get_client(ctx)
    path = (
        f"/projects/{encode_path_segment(project_id)}"
        f"/workitems/{encode_path_segment(work_item_id)}"
        "/actions/moveToDocument"
    )
    try:
        await client.post(path, json=cast(dict[str, object], payload))
    except PolarionAuthError as exc:
        raise PermissionError(
            "Cannot move work item -- check your POLARION_TOKEN permissions."
        ) from exc
    except PolarionNotFoundError as exc:
        raise ValueError(
            f"Work item '{work_item_id}' (project '{project_id}') or "
            f"target document '{target_document_name}' (space "
            f"'{target_space_id}') or referenced part not found. "
            "Verify with `get_work_item`, `list_documents`, and "
            "`read_document_parts`."
        ) from exc
    except PolarionError as exc:
        raise RuntimeError(f"Failed to move work item: {exc.message}") from exc

    return WorkItemMoveResult(
        moved=True,
        dry_run=False,
        payload_preview=None,
    )


@mcp.tool(
    tags={"write"},
    timeout=60.0,
    annotations={
        # Non-idempotent: a second moveFromDocument 400s (already free-floating).
        "readOnlyHint": False,
        "destructiveHint": True,
        "idempotentHint": False,
        "openWorldHint": True,
    },
)
async def move_work_item_from_document(
    ctx: Context,
    project_id: str = Field(description="Project containing the work item."),
    work_item_id: str = Field(
        min_length=1,
        description="Short ID of an EXISTING work item (e.g. 'MCPT-042').",
    ),
    dry_run: bool = Field(
        default=False,
        description="When True, return payload preview without calling Polarion.",
    ),
) -> WorkItemMoveResult:
    """Detach a work item from its document, returning it to free-floating state.

    Inverse of ``move_work_item_to_document``. Calls the ``moveFromDocument``
    action endpoint, which clears the work item's ``module`` relationship and
    removes its document part — the ONLY supported detach path (Polarion rejects
    PATCH on ``module``). The work item itself is preserved (history, links,
    attributes) and reappears as free-floating, visible to ``list_work_items``;
    re-attach via ``move_work_item_to_document``.

    NOT idempotent: calling it on an already free-floating item returns HTTP 400
    (``RuntimeError``). Heading-type items CAN be detached — the heading becomes
    a free-floating work item with ``space_id=""`` / ``outline_number=""``
    (orphan-like but intact).

    Args:
        ctx: MCP tool context (injected automatically).
        project_id: Project containing the work item.
        work_item_id: Short ID of an existing work item.
        dry_run: When True, return payload preview only.

    Returns:
        WorkItemMoveResult with ``moved``, ``dry_run``, and
        ``payload_preview`` (an empty dict on dry-run; None on real
        execution). Polarion returns 204 on success — confirm with
        ``get_work_item`` showing ``space_id=""``.

    Raises:
        ValueError: Project or work item not found.
        PermissionError: Token lacks permission.
        RuntimeError: Other Polarion API errors, including the 400
            returned when the work item is already free-floating.
    """
    if dry_run:
        return WorkItemMoveResult(
            moved=False,
            dry_run=True,
            payload_preview={},
        )

    client = get_client(ctx)
    path = (
        f"/projects/{encode_path_segment(project_id)}"
        f"/workitems/{encode_path_segment(work_item_id)}"
        "/actions/moveFromDocument"
    )
    try:
        # moveFromDocument takes no body per the Polarion REST API spec.
        await client.post(path)
    except PolarionAuthError as exc:
        raise PermissionError(
            "Cannot detach work item -- check your POLARION_TOKEN permissions."
        ) from exc
    except PolarionNotFoundError as exc:
        raise ValueError(
            f"Work item '{work_item_id}' in project '{project_id}' not found. "
            "Use `list_work_items` to discover valid IDs."
        ) from exc
    except PolarionError as exc:
        raise RuntimeError(f"Failed to detach work item: {exc.message}") from exc

    return WorkItemMoveResult(
        moved=True,
        dry_run=False,
        payload_preview=None,
    )


@mcp.tool(
    tags={"write"},
    timeout=60.0,
    annotations={
        "readOnlyHint": False,
        "destructiveHint": True,
        "idempotentHint": True,
        "openWorldHint": True,
    },
)
async def update_document(  # noqa: PLR0913
    ctx: Context,
    project_id: str = Field(description="Polarion project ID."),
    space_id: str = Field(
        min_length=1,
        description="Space ID (use '_default' for the default space).",
    ),
    document_name: str = Field(
        min_length=1,
        description="Document name within ``space_id``.",
    ),
    title: str | None = None,
    status: str | None = Field(
        default=None,
        description=(
            "New workflow status; prefer ``workflow_action`` for real transitions."
        ),
    ),
    type: str | None = Field(
        default=None, description="New document type (e.g. 'req_specification')."
    ),
    home_page_content_html: str | None = Field(
        default=None,
        max_length=MAX_BODY_HTML_LEN,
        description=(
            "New body as raw Polarion HTML (round-trip shape from get_document); "
            "'' is rejected."
        ),
    ),
    custom_fields: dict[str, object] | None = Field(  # noqa: B008
        default=None,
        description=(
            "Partial custom-field update; "
            "rich-text values must be ``{'type':'text/html','value':...}``."
        ),
    ),
    workflow_action: str | None = Field(
        default=None,
        description=(
            "Workflow action ID; must be paired with at least one attribute field."
        ),
    ),
    dry_run: bool = Field(
        default=False,
        description=(
            "When True, return the payload preview without writing; "
            "enum/custom-field guards still query Polarion, so the document "
            "must be readable and the validation endpoint reachable."
        ),
    ),
) -> DocumentUpdateResult:
    """Update a Polarion document's metadata or body.

    PATCHes only the attributes you set; omitted fields are preserved. Unlike
    ``update_work_item`` this does NOT follow up with a GET — call
    ``get_document`` for the refreshed state.

    ``home_page_content_html`` is the round-trip pair for
    ``get_document(include_homepage_content_html=True)``, sent verbatim with no
    sanitization (XSS filtering is Polarion's job — NEVER pass untrusted input).
    Empty string is rejected (would wipe the body and orphan every heading);
    pass ``'<p></p>'`` for a near-empty body.

    Body-write rules:

    - **Headings auto-create**: inline ``<h1>..<h4>`` become heading work items
      with ``module`` / ``outline_number`` set; a bare ``<hN>Title</hN>`` is safe
      and needs no id. Removing an ``<hN>`` drops the part but leaves the heading
      work item (still ``module``-linked, no ``outline_number``).
    - **Anchorless blocks break parts**: ``<h3>X</h3><p>Body</p>`` PATCHes 200 but
      the next ``read_document_parts`` returns HTTP 500 — Polarion's stored blocks
      all carry ``id="polarion_..."`` anchors. Every anchorless ``<p>`` / ``<ul>``
      / ``<ol>`` / ``<table>`` / ``<div>`` / ``<blockquote>`` / ``<pre>`` needs a
      unique non-empty ``id=`` (the tool raises ``ValueError`` before the PATCH
      otherwise, on ``dry_run`` too). This raw-HTML path has no Markdown
      auto-stamping; for body text prefer ``create_work_items`` +
      ``move_work_item_to_document``.
    - **No macro references**: injecting
      ``<div id="polarion_wiki macro name=module-workitem;params=id=NEW">`` makes
      a ``workitem_<NEW>`` part but leaves the work item's ``module`` unset
      (``space_id=""``, ``outline_number=""``) — a half-attached state. Always
      attach via ``move_work_item_to_document``.

    Workflow: prefer ``workflow_action`` over a raw ``status`` edit so project
    rules run; it MUST be paired with at least one attribute field (Polarion
    rejects empty PATCH bodies). Unknown ``status`` / ``type`` ids raise
    ``ValueError`` listing the valid options; unknown ``custom_fields`` keys are
    rejected unless seen on a prior ``get_document`` (one priming read on a miss).

    Args:
        ctx: MCP tool context (injected automatically).
        project_id: Polarion project ID.
        space_id: Space ID containing the document.
        document_name: Document name within ``space_id``.
        title: Optional new title.
        status: Optional new workflow status.
        type: Optional new document type.
        home_page_content_html: Optional new body as raw Polarion HTML.
        custom_fields: Optional partial custom-field update.
        workflow_action: Optional action ID; must be paired with a
            body field.
        dry_run: When True, return payload preview only.

    Returns:
        DocumentUpdateResult with ``updated``, ``dry_run``, and
        ``payload_preview`` (populated on dry-run; None on real update).

    Raises:
        ValueError: No fields supplied, action without body, empty
            ``home_page_content_html``, custom-field key collision, or
            document / space / project not found.
        PermissionError: Token lacks permission.
        RuntimeError: Other Polarion API errors.
    """
    if home_page_content_html is not None and not home_page_content_html.strip():
        raise ValueError(
            "home_page_content_html is empty or whitespace-only; sending "
            "this would wipe the document body and orphan every heading "
            "work item. Pass at minimum '<p></p>' or omit the parameter "
            "to leave the body unchanged."
        )
    if home_page_content_html is not None:
        anchorless = first_anchorless_block(home_page_content_html)
        if anchorless is not None:
            raise ValueError(
                f"home_page_content_html contains an anchorless <{anchorless}> "
                "block. Every non-heading block (<p>/<ul>/<ol>/<table>/<div>/"
                "<blockquote>/<pre>) must carry a unique non-empty id= or the "
                "next read_document_parts returns HTTP 500. Stamp ids "
                '(e.g. id="polarion_mcp_0") on each such block before updating; '
                "<h1>..<h6> headings are exempt."
            )

    has_attrs = (
        title is not None
        or status is not None
        or type is not None
        or home_page_content_html is not None
        or bool(custom_fields)
    )
    if not has_attrs and not workflow_action:
        raise ValueError(
            "update_document requires at least one of: title, status, "
            "type, home_page_content_html, custom_fields, or "
            "workflow_action."
        )
    if not has_attrs and workflow_action:
        raise ValueError(
            "workflow_action alone is not supported -- Polarion rejects "
            "PATCH bodies with no attributes. Pair workflow_action with "
            "at least one of title, status, type, home_page_content_html, "
            "or custom_fields."
        )

    client = get_client(ctx)
    # Guard type/status against type-agnostic options — less precise than
    # scoping by the document's type, but avoids the extra GET and still
    # catches obvious ghost ids.
    await guard_document_enums(
        client,
        project_id,
        document_type="~",
        type=type,
        status=status,
    )

    # Build first: merge_custom_fields rejects keys shadowing a standard
    # attribute — a more fundamental error, and cheaper than the guard's GET.
    payload = _build_update_document_payload(
        project_id=project_id,
        space_id=space_id,
        document_name=document_name,
        title=title,
        status=status,
        type=type,
        home_page_content_html=home_page_content_html,
        custom_fields=custom_fields,
    )
    # Hard guard: reject custom-field keys unseen on a prior get_document
    # (priming GET on miss) — Polarion persists unknown keys as silent ghosts.
    await guard_document_custom_field_keys(
        client,
        project_id,
        space_id,
        document_name,
        custom_fields or {},
    )

    if dry_run:
        return DocumentUpdateResult(
            updated=False,
            dry_run=True,
            payload_preview=payload,
        )

    base_path = (
        f"/projects/{encode_path_segment(project_id)}"
        f"/spaces/{encode_path_segment(space_id)}"
        f"/documents/{encode_path_segment(document_name)}"
    )
    query_params: dict[str, str] = {}
    if workflow_action:
        query_params["workflowAction"] = workflow_action
    patch_path = f"{base_path}?{urlencode(query_params)}" if query_params else base_path

    try:
        await client.patch(patch_path, json=cast(dict[str, object], payload))
    except PolarionAuthError as exc:
        raise PermissionError(
            "Cannot update document -- check your POLARION_TOKEN permissions."
        ) from exc
    except PolarionNotFoundError as exc:
        raise ValueError(
            f"Document '{document_name}' (space '{space_id}', "
            f"project '{project_id}') not found. "
            "Use `list_documents` to discover valid IDs."
        ) from exc
    except PolarionError as exc:
        raise RuntimeError(f"Failed to update document: {exc.message}") from exc

    return DocumentUpdateResult(
        updated=True,
        dry_run=False,
        payload_preview=None,
    )


@mcp.tool(
    tags={"write"},
    timeout=60.0,
    annotations={
        # Additive: non-destructive, but non-idempotent (duplicate module_name 409s).
        "readOnlyHint": False,
        "destructiveHint": False,
        "idempotentHint": False,
        "openWorldHint": True,
    },
)
async def create_document(  # noqa: PLR0913
    ctx: Context,
    project_id: str = Field(min_length=1, description="Polarion project ID."),
    space_id: str = Field(
        min_length=1,
        description="Space ID (use '_default' for the default space).",
    ),
    module_name: str = Field(
        min_length=1,
        description=(
            "Polarion document identifier (e.g. 'MySpecV1'); "
            "must be unique within ``space_id`` and appears in the document URL."
        ),
    ),
    title: str = Field(
        min_length=1,
        description="Human-readable document title (required, non-empty).",
    ),
    type: str = Field(
        min_length=1,
        description="Document type (e.g. 'req_specification', 'generic').",
    ),
    status: str | None = Field(
        default=None,
        description=(
            "Optional initial workflow status (project default applies if omitted)."
        ),
    ),
    home_page_content: str | None = Field(
        default=None,
        max_length=MAX_BODY_HTML_LEN,
        description="Optional Markdown body; converted to sanitized HTML on write.",
    ),
    custom_fields: dict[str, object] | None = Field(  # noqa: B008
        default=None,
        description=(
            "Optional custom fields keyed by Polarion field ID; take keys "
            "from a sibling document via get_document to avoid ghost keys; "
            "rich-text values must be ``{'type':'text/html','value':...}``."
        ),
    ),
    dry_run: bool = Field(
        default=False,
        description=(
            "When True, return the payload preview without writing; the enum "
            "guard still calls Polarion's getAvailableOptions, so that "
            "endpoint must be reachable."
        ),
    ),
) -> DocumentCreateResult:
    """Create a new Polarion document in a space.

    Before calling, you MUST call ``list_documents(project_id, space_id)`` and
    confirm ``module_name`` is not already present — it must be unique within
    the space, and a duplicate returns HTTP 409 (``RuntimeError``). For every
    enum-valued argument (``type``, ``status``, ``custom_fields`` enum entries)
    supply only ids returned by
    ``list_document_enum_options(project_id, field_id, document_type)`` —
    unverified ids persist as ghosts that never match Lucene. ``custom_fields``
    keys are unvalidated too — take them from a sibling document via
    ``get_document``.

    The document starts empty (or with the optional ``home_page_content``
    body); add headings / work-item parts later via ``update_document`` and
    ``move_work_item_to_document``. ``module_name`` is Polarion's persistent
    identifier within the space and appears in every subsequent URL.

    Format asymmetry: ``home_page_content`` is Markdown (converted to sanitized
    HTML on write); after creation the round-trip pair is
    ``get_document(include_homepage_content_html=True)`` ↔
    ``update_document(home_page_content_html=...)`` (raw HTML verbatim). The two
    formats never mix.

    When ``home_page_content`` is provided, every block-level element (``<p>``,
    ``<ul>``, ``<ol>``, ``<table>``, ``<div>``, ``<blockquote>``, ``<pre>``) is
    stamped with a unique ``id="polarion_mcp_N"`` anchor — without these the
    next ``read_document_parts`` returns HTTP 500. ``<h1>..<h4>`` are skipped
    (Polarion rewrites them to a ``polarion_wiki macro name=module-workitem``
    form on save).

    Args:
        ctx: MCP tool context (injected automatically).
        project_id: Polarion project ID.
        space_id: Space ID containing the new document.
        module_name: Polarion document identifier (unique within space).
        title: Human-readable document title.
        type: Document type.
        status: Optional initial workflow status.
        home_page_content: Optional Markdown body.
        custom_fields: Optional custom-field dict.
        dry_run: When True, return payload preview only.

    Returns:
        DocumentCreateResult with ``created``, ``dry_run``,
        ``document_name`` (None on dry-run), and ``payload_preview``
        (populated on dry-run; None on real create).

    Raises:
        ValueError: Project or space not found, or custom-field key
            collides with a standard Polarion attribute.
        PermissionError: Token lacks permission.
        RuntimeError: Other Polarion API errors (including duplicate
            ``module_name``), or accepted-but-no-ID response.
    """
    client = get_client(ctx)
    await guard_document_enums(
        client,
        project_id,
        document_type=type,
        type=type,
        status=status,
    )
    if custom_fields:
        # Create cannot hard-guard keys (no project-config endpoint, no prior
        # get to learn them) the way update_document does — warn instead.
        logger.warning(
            "create_document.custom_fields cannot be schema-validated "
            "(no project-config endpoint for custom-field keys); ensure keys "
            "come from a sibling document via get_document to avoid ghost "
            "attributes. project=%s module=%s keys=%s",
            project_id,
            module_name,
            sorted(custom_fields),
        )

    home_page_content_html = (
        stamp_block_ids(sanitize_html(markdown_to_html(home_page_content)))
        if home_page_content
        else ""
    )

    payload = _build_create_document_payload(
        module_name=module_name,
        title=title,
        type=type,
        home_page_content_html=home_page_content_html,
        status=status,
        custom_fields=custom_fields,
    )

    if dry_run:
        return DocumentCreateResult(
            created=False,
            dry_run=True,
            document_name=None,
            payload_preview=payload,
        )

    path = (
        f"/projects/{encode_path_segment(project_id)}"
        f"/spaces/{encode_path_segment(space_id)}/documents"
    )
    try:
        response = await client.post(path, json=cast(dict[str, object], payload))
    except PolarionAuthError as exc:
        raise PermissionError(
            "Cannot create document -- check your POLARION_TOKEN permissions."
        ) from exc
    except PolarionNotFoundError as exc:
        raise ValueError(
            f"Project '{project_id}' or space '{space_id}' not found. "
            "Use `list_projects` and `list_documents` to discover valid IDs."
        ) from exc
    except PolarionError as exc:
        raise RuntimeError(f"Failed to create document: {exc.message}") from exc

    new_name = _extract_created_module_name(response)
    if new_name is None:
        raise RuntimeError(
            "Polarion accepted the create request but returned no document name. "
            "The document may or may not exist; verify with list_documents."
        )

    # Drop the stale list_documents cache so the new doc shows on the next call.
    invalidate_documents_cache(project_id)

    return DocumentCreateResult(
        created=True,
        dry_run=False,
        document_name=new_name,
        payload_preview=None,
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


@mcp.tool(
    tags={"write"},
    timeout=60.0,
    annotations={
        # Additive: non-destructive, but non-idempotent (a retry duplicates comments).
        "readOnlyHint": False,
        "destructiveHint": False,
        "idempotentHint": False,
        "openWorldHint": True,
    },
)
async def create_document_comments(  # noqa: PLR0913
    ctx: Context,
    project_id: str = Field(min_length=1, description="Polarion project ID."),
    space_id: str = Field(
        min_length=1,
        description="Space ID (use '_default' for the default space).",
    ),
    document_name: str = Field(
        min_length=1,
        description="Document name within ``space_id``.",
    ),
    comments: list[DocumentCommentSpec] = Field(  # noqa: B008
        min_length=1,
        description="One or more comments to create in a single request.",
    ),
    dry_run: bool = Field(
        default=False,
        description="When True, return payload preview without calling Polarion.",
    ),
) -> DocumentCommentsCreateResult:
    """Create one or more comments on a Polarion document in a single request.

    All ``comments`` post together; Polarion returns 201 with the new IDs.

    Thread model: ``parent_comment_id=None`` is a top-level comment; to reply,
    set it to the short ID from ``list_document_comments`` (e.g. ``'c42'``) and
    the tool composes the required 4-segment path ``proj/space/doc/c42``.
    ``text_format`` ``'text/plain'`` (default) stores verbatim; ``'text/html'``
    is sent as-is (no sanitization). Omit ``resolved`` to default to False, or
    pass True for a pre-resolved comment; omit ``author_id`` to use the token's
    user. NOT idempotent — retrying creates duplicates.

    Args:
        ctx: MCP tool context (injected automatically).
        project_id: Polarion project ID.
        space_id: Space ID (use '_default' for the default space).
        document_name: Document name within ``space_id``.
        comments: One or more ``DocumentCommentSpec`` objects, each with
            ``text``, ``text_format``, ``resolved``, ``author_id``, and
            ``parent_comment_id``.
        dry_run: When True, build and return the payload preview without
            calling Polarion.

    Returns:
        ``DocumentCommentsCreateResult`` with:

        - ``created`` — True on success; False on dry-run.
        - ``dry_run`` — mirrors the input flag.
        - ``comment_ids`` — short IDs (last path segment) of the created
          comments in Polarion's return order; empty on dry-run.
        - ``payload_preview`` — JSON:API body sent (or that would be
          sent); populated on dry-run, None after a real operation.

    Raises:
        ValueError: Project, space, or document not found.
        PermissionError: Token lacks permission to create comments.
        RuntimeError: Other Polarion API errors.
    """
    payload = _build_document_comments_payload(
        specs=comments,
        project_id=project_id,
        space_id=space_id,
        document_name=document_name,
    )

    if dry_run:
        return DocumentCommentsCreateResult(
            created=False,
            dry_run=True,
            comment_ids=[],
            payload_preview=payload,
        )

    client = get_client(ctx)
    path = (
        f"/projects/{encode_path_segment(project_id)}"
        f"/spaces/{encode_path_segment(space_id)}"
        f"/documents/{encode_path_segment(document_name)}"
        "/comments"
    )
    try:
        response = await client.post(path, json=cast(dict[str, object], payload))
    except PolarionAuthError as exc:
        raise PermissionError(
            "Cannot create document comments -- check your POLARION_TOKEN permissions."
        ) from exc
    except PolarionNotFoundError as exc:
        raise ValueError(
            f"Document '{document_name}' (space '{space_id}',"
            f" project '{project_id}') not found."
            " Use `list_documents` to discover valid IDs."
        ) from exc
    except PolarionError as exc:
        raise RuntimeError(
            f"Failed to create document comments: {exc.message}"
        ) from exc

    raw_data = response.get("data", []) if isinstance(response, dict) else []
    comment_ids: list[str] = []
    if isinstance(raw_data, list):
        for entry in raw_data:
            if isinstance(entry, dict):
                full_id = safe_str(entry.get("id", ""))
                if full_id:
                    comment_ids.append(extract_short_id(full_id))

    if not comment_ids:
        raise RuntimeError(
            "Polarion returned no comment IDs after creation."
            " The POST may have succeeded — verify with `list_document_comments`."
        )

    return DocumentCommentsCreateResult(
        created=True,
        dry_run=False,
        comment_ids=comment_ids,
        payload_preview=None,
    )


@mcp.tool(
    tags={"write"},
    timeout=60.0,
    annotations={
        "readOnlyHint": False,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": True,
    },
)
async def update_document_comment(  # noqa: PLR0913
    ctx: Context,
    project_id: str = Field(min_length=1, description="Polarion project ID."),
    space_id: str = Field(
        min_length=1,
        description="Space ID (use '_default' for the default space).",
    ),
    document_name: str = Field(
        min_length=1,
        description="Document name within ``space_id``.",
    ),
    comment_id: str = Field(
        min_length=1,
        description=(
            "Short comment ID to update (e.g. 'c42' from ``list_document_comments``)."
        ),
    ),
    resolved: bool = Field(description="New resolved state for the comment."),
    dry_run: bool = Field(
        default=False,
        description="When True, return payload preview without calling Polarion.",
    ),
) -> DocumentCommentUpdateResult:
    """Resolve or re-open a single document comment.

    PATCHes ``{"resolved": <bool>}`` — the only patchable attribute. Use
    ``list_document_comments`` to find the short comment ID (last segment of the
    4-part path).

    Root comments only: Polarion accepts this PATCH only on top-level comments
    (``parent_comment_id is None``); on a reply it returns HTTP 400 ("Resolved
    field can be set only for root comments") → ``RuntimeError``, so filter
    first. Resolving the root marks the whole thread resolved. Idempotent.

    Args:
        ctx: MCP tool context (injected automatically).
        project_id: Polarion project ID.
        space_id: Space ID (use '_default' for the default space).
        document_name: Document name within ``space_id``.
        comment_id: Short comment ID (e.g. 'c42') from
            ``list_document_comments``.
        resolved: ``True`` to mark resolved; ``False`` to re-open.
        dry_run: When True, build and return the payload without
            calling Polarion.

    Returns:
        ``DocumentCommentUpdateResult`` with:

        - ``updated`` — True on success; False on dry-run.
        - ``dry_run`` — mirrors the input flag.
        - ``comment_id`` — the short ID patched; None on dry-run.
        - ``resolved`` — the value sent (or that would be sent).
        - ``payload_preview`` — JSON:API body; populated on dry-run,
          None after a real operation.

    Raises:
        ValueError: Project, space, document, or comment not found.
        PermissionError: Token lacks permission to update the comment.
        RuntimeError: Other Polarion API errors.
    """
    payload = _build_document_comment_update_payload(
        project_id=project_id,
        space_id=space_id,
        document_name=document_name,
        comment_id=comment_id,
        resolved=resolved,
    )

    if dry_run:
        return DocumentCommentUpdateResult(
            updated=False,
            dry_run=True,
            comment_id=None,
            resolved=resolved,
            payload_preview=payload,
        )

    client = get_client(ctx)
    path = (
        f"/projects/{encode_path_segment(project_id)}"
        f"/spaces/{encode_path_segment(space_id)}"
        f"/documents/{encode_path_segment(document_name)}"
        f"/comments/{encode_path_segment(comment_id)}"
    )
    try:
        await client.patch(path, json=cast(dict[str, object], payload))
    except PolarionAuthError as exc:
        raise PermissionError(
            "Cannot update document comment -- check your POLARION_TOKEN permissions."
        ) from exc
    except PolarionNotFoundError as exc:
        raise ValueError(
            f"Comment '{comment_id}' on document '{document_name}'"
            f" (space '{space_id}', project '{project_id}') not found."
            " Use `list_document_comments` to discover valid comment IDs."
        ) from exc
    except PolarionError as exc:
        raise RuntimeError(f"Failed to update document comment: {exc.message}") from exc

    return DocumentCommentUpdateResult(
        updated=True,
        dry_run=False,
        comment_id=comment_id,
        resolved=resolved,
        payload_preview=None,
    )
