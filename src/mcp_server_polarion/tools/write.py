"""Write MCP tools for Polarion ALM.

Currently provides ``create_work_item``, ``update_work_item``,
``move_work_item_to_document``, ``update_document``,
``create_document``, ``create_work_item_links``,
``delete_work_item_links``, ``update_work_item_links``,
``create_document_comments``, and ``update_document_comment``. All
write tools follow the strict
patterns documented in ``CLAUDE.md``: they convert Markdown input to
sanitized HTML, build minimal request payloads (skipping unset fields
rather than sending empty values), and map domain exceptions to
user-facing ones at the tool layer.
"""

from __future__ import annotations

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
    DocumentCommentsCreateResult,
    DocumentCommentSpec,
    DocumentCommentUpdateResult,
    DocumentCreateResult,
    DocumentUpdateResult,
    Hyperlink,
    JsonValue,
    WorkItemCreateResult,
    WorkItemLinkRef,
    WorkItemLinksCreateResult,
    WorkItemLinksDeleteResult,
    WorkItemLinkSpec,
    WorkItemLinkUpdateResult,
    WorkItemLinkUpdateSpec,
    WorkItemMoveResult,
    WorkItemUpdateResult,
)
from mcp_server_polarion.server import mcp
from mcp_server_polarion.tools._helpers import (
    STANDARD_DOCUMENT_ATTRIBUTES,
    STANDARD_WORK_ITEM_ATTRIBUTES,
    WORK_ITEM_DETAIL_FIELDS,
    encode_path_segment,
    extract_short_id,
    get_client,
    invalidate_documents_cache,
    merge_custom_fields,
    parse_work_item_detail,
    safe_str,
    split_module_id,
)
from mcp_server_polarion.utils import markdown_to_html, sanitize_html, stamp_block_ids

# Caps tool-layer body payloads so a prompt-injected caller cannot ship a
# multi-megabyte blob to Polarion. Observed real document bodies stay
# under ~30 KB, so 2 MiB leaves ~70x headroom.
MAX_BODY_HTML_LEN: Final[int] = 2_000_000

# ---------------------------------------------------------------------------
# Private helpers
# ---------------------------------------------------------------------------


def _build_work_item_payload(  # noqa: PLR0913
    *,
    title: str,
    type: str,
    description_html: str,
    status: str | None,
    priority: str | None,
    severity: str | None,
    assignee_ids: list[str] | None,
    due_date: str | None,
    initial_estimate: str | None,
    hyperlinks: list[Hyperlink] | None,
    custom_fields: dict[str, object] | None = None,
) -> dict[str, JsonValue]:
    """Build the JSON:API request body for ``POST /projects/{p}/workitems``.

    Only attaches keys for values that are explicitly set — ``None``,
    empty strings, and empty lists are skipped so we never overwrite
    Polarion defaults with empty values on creation. ``custom_fields``
    entries are inlined into ``attributes`` alongside the standard
    fields via ``merge_custom_fields``; colliding keys raise
    ``ValueError`` so the caller cannot accidentally shadow an explicit
    standard parameter.
    """
    attributes: dict[str, JsonValue] = {
        "title": title,
        "type": type,
    }
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
        "attributes": attributes,
    }
    if relationships:
        item["relationships"] = relationships

    return {"data": [item]}


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


def _extract_created_id(response: dict[str, object]) -> str | None:
    """Extract the short work-item ID from a 201 create response.

    Polarion returns ``{"data": [{"type": "workitems",
    "id": "projectId/MCPT-042", ...}]}``.  Returns the short ID
    (``"MCPT-042"``) or ``None`` if the response shape is unexpected.
    """
    full_id = _extract_first_resource_id(response)
    if full_id is None:
        return None
    return extract_short_id(full_id)


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
    """Return composite link ids verbatim from a bulk create-link response.

    Polarion returns ``{"data": [{"type": "linkedworkitems",
    "id": "<srcProj>/<srcWI>/<role>/<tgtProj>/<tgtWI>", ...}, ...]}``.
    Preserves input order and is the path identifier for subsequent
    PATCH / DELETE of the same links. Empty list on malformed shapes;
    callers should treat empty as a failure.
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

    Mirrors ``_build_work_item_payload`` but produces a PATCH-shaped body:
    ``data`` is a single resource object (not a list), with a required
    ``id`` of the form ``"{project_id}/{work_item_id}"``. Only attaches
    keys for values that are explicitly set — ``None``, empty strings,
    and empty lists are skipped so we never overwrite Polarion attributes
    with empty values on update. ``custom_fields`` entries are inlined
    into ``attributes`` alongside the standard fields via
    ``merge_custom_fields``; colliding keys raise ``ValueError``.
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

    Note: this endpoint is NOT JSON:API — the body is a flat object
    with ``targetDocument``, plus AT MOST one of ``previousPart`` or
    ``nextPart``. Both omitted is valid and means "append at end" per
    the Polarion REST API. The tool layer validates the at-most-one
    invariant before calling this helper, but we re-check here so a
    future direct caller cannot ship a body Polarion would reject.
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

    Mirrors ``_build_update_work_item_payload``'s PATCH shape: ``data`` is
    a single resource object (NOT a list) with a required ``id`` of the
    form ``"{project_id}/{space_id}/{document_name}"``. Only attaches
    keys for values that are explicitly set -- ``None`` values are
    skipped so JSON:API omit-preserve takes effect (the server keeps
    the existing server-side value). ``custom_fields`` entries are
    inlined into ``attributes`` via ``merge_custom_fields``; colliding
    keys raise ``ValueError``.

    ``home_page_content_html`` is treated as RAW Polarion HTML and is
    wrapped verbatim into ``{"type":"text/html","value":...}`` — no
    sanitization, no Markdown conversion. The body-clearing guard
    (rejecting empty strings) lives in the tool layer (``update_document``)
    rather than here so direct callers can opt out if needed.
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

    Mirrors ``_build_work_item_payload``'s POST shape: ``data`` is a
    single-element list with ``type=documents`` and inline ``attributes``.
    Only attaches keys for values that are explicitly set -- ``None`` and
    empty strings are skipped so we never overwrite Polarion defaults
    with empty values on creation. ``custom_fields`` entries are inlined
    into ``attributes`` alongside the standard fields via
    ``merge_custom_fields``; colliding keys raise ``ValueError``.
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


# ---------------------------------------------------------------------------
# Write tools
# ---------------------------------------------------------------------------


@mcp.tool(
    tags={"write"},
    timeout=60.0,
    annotations={
        # Pure additive operation per MCP spec — creates a new work item without
        # mutating existing data, so destructiveHint is False. Not idempotent
        # because retrying with the same input creates a duplicate.
        "readOnlyHint": False,
        "destructiveHint": False,
        "idempotentHint": False,
        "openWorldHint": True,
    },
)
async def create_work_item(  # noqa: PLR0913
    ctx: Context,
    project_id: str = Field(description="Polarion project ID."),
    title: str = Field(
        min_length=1, description="Work item title (required, non-empty)."
    ),
    type: str = Field(
        min_length=1,
        description="Work item type (e.g. 'requirement', 'task', 'testCase').",
    ),
    description: str | None = Field(
        default=None,
        max_length=MAX_BODY_HTML_LEN,
        description="Optional Markdown body; converted to sanitized HTML on write.",
    ),
    status: str | None = Field(
        default=None,
        description=(
            "Optional initial workflow status (project default applies if omitted)."
        ),
    ),
    priority: str | None = Field(
        default=None,
        description="Optional priority string (e.g. '50.0').",
    ),
    severity: str | None = Field(
        default=None,
        description="Optional severity classification (e.g. 'major', 'critical').",
    ),
    assignee_ids: list[str] | None = Field(  # noqa: B008
        default=None,
        description="Optional short user IDs to assign (e.g. ['alice', 'bob']).",
    ),
    due_date: str | None = Field(
        default=None,
        description="Optional due date 'YYYY-MM-DD'.",
    ),
    initial_estimate: str | None = Field(
        default=None,
        description="Optional Polarion duration (e.g. '5 1/2d', '1w 2d', '4h').",
    ),
    hyperlinks: list[Hyperlink] | None = Field(  # noqa: B008
        default=None,
        description=(
            "Optional external hyperlinks; each must have ``role`` and ``uri``."
        ),
    ),
    custom_fields: dict[str, object] | None = Field(  # noqa: B008
        default=None,
        description=(
            "Optional custom fields keyed by Polarion field ID; "
            "rich-text values must be ``{'type':'text/html','value':...}``."
        ),
    ),
    dry_run: bool = Field(
        default=False,
        description="When True, return payload preview without calling Polarion.",
    ),
) -> WorkItemCreateResult:
    """Create a new Polarion work item in a project.

    The work item is created free-floating — to place it inside a
    document at a specific outline position, follow up with
    ``move_work_item_to_document``. Direct creation into a document via
    the ``module`` relationship is intentionally NOT exposed: per the
    Polarion API, such work items land in the document's recycle bin
    until a separate Document Part is created, leaving them invisible
    in the document body. Always pair create + move for a single,
    visible result.

    Format asymmetry: ``description`` here is Markdown (converted to
    sanitized HTML on write) because greenfield authoring is natural for
    LLMs. After creation the round-trip pair is
    ``get_work_item(include_description_html=True)`` ↔
    ``update_work_item(description_html=...)`` which speaks raw HTML
    verbatim. The two formats never mix.

    Polarion does NOT validate enum membership server-side. Unknown
    ``type`` / ``status`` / ``severity`` ids are stored verbatim as
    ghost values that look real on later reads but never match Lucene
    queries. ``priority`` is the only partial exception: a non-numeric
    string coerces to the project default, but a numeric string outside
    the enum set (e.g. ``"999.0"``) also stores verbatim. Resolve valid
    ids first via ``list_work_item_enum_options(project_id, field_id,
    work_item_type)``. ``custom_fields`` is the same story: unknown
    field IDs — including brand-new IDs that no work item of this type
    has ever used — silently persist as ghost attributes. Pass keys
    taken from a prior ``get_work_item``.

    Args:
        ctx: MCP tool context (injected automatically).
        project_id: Polarion project ID.
        title: Work item title.
        type: Work item type.
        description: Optional Markdown body.
        status: Optional workflow status.
        priority: Optional priority string.
        severity: Optional severity classification.
        assignee_ids: Optional short user IDs.
        due_date: Optional ISO-8601 date.
        initial_estimate: Optional duration string.
        hyperlinks: Optional ``Hyperlink`` list.
        custom_fields: Optional custom-field dict.
        dry_run: When True, return payload preview only.

    Returns:
        WorkItemCreateResult with ``created``, ``dry_run``,
        ``work_item_id`` (None on dry-run), and ``payload_preview``
        (populated on dry-run; None on real create).

    Raises:
        ValueError: Project not found, or custom-field key collides with
            a standard Polarion attribute.
        PermissionError: Token lacks permission.
        RuntimeError: Other Polarion API errors, or accepted-but-no-ID.
    """
    description_html = (
        sanitize_html(markdown_to_html(description)) if description else ""
    )

    payload = _build_work_item_payload(
        title=title,
        type=type,
        description_html=description_html,
        status=status,
        priority=priority,
        severity=severity,
        assignee_ids=assignee_ids,
        due_date=due_date,
        initial_estimate=initial_estimate,
        hyperlinks=hyperlinks,
        custom_fields=custom_fields,
    )

    if dry_run:
        return WorkItemCreateResult(
            created=False,
            dry_run=True,
            work_item_id=None,
            payload_preview=payload,
        )

    client = get_client(ctx)
    path = f"/projects/{encode_path_segment(project_id)}/workitems"
    try:
        response = await client.post(path, json=cast(dict[str, object], payload))
    except PolarionAuthError as exc:
        raise PermissionError(
            "Cannot create work item -- check your POLARION_TOKEN permissions."
        ) from exc
    except PolarionNotFoundError as exc:
        raise ValueError(
            f"Project '{project_id}' not found. "
            "Use `list_projects` to discover valid project IDs."
        ) from exc
    except PolarionError as exc:
        raise RuntimeError(f"Failed to create work item: {exc.message}") from exc

    new_id = _extract_created_id(response)
    if new_id is None:
        raise RuntimeError(
            "Polarion accepted the create request but returned no work-item ID. "
            "The work item may or may not exist; verify with list_work_items."
        )

    return WorkItemCreateResult(
        created=True,
        dry_run=False,
        work_item_id=new_id,
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
    title: str | None = Field(
        default=None, description="New title (None to leave unchanged)."
    ),
    description_html: str | None = Field(
        default=None,
        max_length=MAX_BODY_HTML_LEN,
        description=(
            "New raw Polarion HTML body (round-trip shape from get_work_item); "
            "'' is a no-op."
        ),
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
    severity: str | None = Field(
        default=None, description="New severity classification."
    ),
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
        description="When True, return payload preview without calling Polarion.",
    ),
) -> WorkItemUpdateResult:
    """Update an existing Polarion work item.

    PATCHes the supplied fields and follows up with a GET so the caller
    can confirm the change in ``current``. ``None`` / empty string /
    empty list all mean ``leave unchanged`` — there is no way to clear
    a field via this tool in v1.

    ``description_html`` is RAW Polarion HTML, sent verbatim with no
    sanitization, so XSS/script filtering is delegated to Polarion's
    renderer — NEVER pass untrusted input. Pair with
    ``get_work_item(include_description_html=True)`` for the round-trip.
    For greenfield authoring use ``create_work_item(description=...)``
    (Markdown) — the two format paths never mix.

    ``hyperlinks`` and ``assignee_ids`` REPLACE the existing lists (pass
    the full list, not a delta). ``custom_fields`` is partial — omitted
    keys are preserved. Unknown custom-field IDs are silently stored as
    ghost attributes; pass keys from a prior read to avoid creating them.

    The ``module`` relationship is NOT exposed here: Polarion rejects
    PATCHes that attempt to modify it. To attach, detach, or move a
    work item between documents, use ``move_work_item_to_document`` /
    ``move_work_item_from_document``.

    Prefer ``workflow_action`` over a raw ``status`` edit so the project's
    transition rules run. ``workflow_action`` and ``change_type_to`` MUST
    be paired with at least one body field — Polarion rejects empty PATCH
    bodies (HTTP 400 "At least one of the members is required"). Direct
    ``status`` / ``severity`` / ``resolution`` writes are NOT validated
    server-side: unknown ids are stored verbatim as ghost values.
    ``priority`` only coerces non-numeric inputs to the project default;
    numeric strings outside the enum set also store verbatim. Resolve
    valid ids first via ``list_work_item_enum_options(project_id,
    field_id, work_item_type)``.

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
        changes["custom_fields"] = cast(JsonValue, dict(custom_fields))
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

    # Polarion's PATCH endpoint rejects bodies with neither attributes
    # nor relationships ("At least one of the members is required"),
    # even when only the workflowAction / changeTypeTo query params are
    # used. Catch this at the tool layer with an actionable message
    # rather than letting Polarion 400.
    payload_data = cast(dict[str, JsonValue], payload["data"])
    if "attributes" not in payload_data and "relationships" not in payload_data:
        raise ValueError(
            "Polarion's PATCH endpoint requires at least one body field "
            "(attribute or relationship) even when triggering "
            "workflow_action or change_type_to. Pair the action with one "
            "of: title, description, status, priority, severity, due_date, "
            "initial_estimate, resolution, hyperlinks, or assignee_ids."
        )

    if dry_run:
        return WorkItemUpdateResult(
            updated=False,
            dry_run=True,
            current=None,
            changes=changes,
            payload_preview=payload,
        )

    client = get_client(ctx)
    base_path = (
        f"/projects/{encode_path_segment(project_id)}"
        f"/workitems/{encode_path_segment(work_item_id)}"
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
        # Mirror ``get_work_item(include_description_html=False)`` —
        # the body still travels over the wire (Polarion @all surfaces
        # customs), but we blank it here to keep the LLM-facing
        # ``current.description_html`` small for the common metadata-
        # only update.
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
        # idempotentHint=False: the moveToDocument action endpoint is not
        # verified to be safe on repeat — a second call against an already-
        # moved work item may 400 instead of no-opping (per Polarion's heading-move
        # behaviour, see CLAUDE.md). Conservative until confirmed.
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

    Calls the ``moveToDocument`` action endpoint, which updates the work item's
    ``module`` relationship, inserts a document part at the specified
    position, and assigns a proper ``outline_number`` — atomically. This
    is the ONLY supported way to attach a work item body to a document; editing
    ``homePageContent`` directly to inject a macro reference leaves the
    ``module`` relationship unset and produces an inconsistent state.

    Heading-type work items are rejected (HTTP 400 "Cannot move
    headings"); headings must be created inside their target document.
    If the work item is already in a different document, this detaches it from
    the source — the operation is a move, not a copy. To detach a work
    item back to free-floating (no document), use
    ``move_work_item_from_document`` — the ``module`` relationship
    cannot be cleared via PATCH.

    Provide AT MOST one of ``previous_part_id`` (insert AFTER) /
    ``next_part_id`` (insert BEFORE); if both are omitted the work item
    is appended at the end of the target document. Discover part IDs
    with ``read_document_parts``.

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
        # idempotentHint=False: calling moveFromDocument twice against the
        # same work item returns HTTP 400 the second time (the work item
        # is already free-floating).
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

    Inverse of ``move_work_item_to_document``. Calls the
    ``moveFromDocument`` action endpoint, which clears the work item's
    ``module`` relationship and removes the corresponding document part.
    This is the ONLY supported detach path because Polarion rejects PATCH
    attempts on the ``module`` relationship.

    The work item resource itself is preserved (with all history, links,
    and attributes) and reappears as a free-floating work item visible to
    ``list_work_items`` but not to any document. To re-attach, call
    ``move_work_item_to_document``.

    Calling this on a work item that is already free-floating returns
    HTTP 400, surfaced here as ``RuntimeError`` — not idempotent.
    Heading-type work items CAN be detached; the heading becomes a
    free-floating work item with ``space_id=""`` and ``outline_number=""``
    (orphan-like state, but the work item is intact).

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
    title: str | None = Field(
        default=None, description="New title (None to leave unchanged)."
    ),
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
        description="When True, return payload preview without calling Polarion.",
    ),
) -> DocumentUpdateResult:
    """Update a Polarion document's metadata or body.

    PATCHes only the attributes you set; omitted fields are preserved
    server-side. Unlike ``update_work_item`` this does NOT follow up
    with a GET — call ``get_document`` if you need the refreshed state.

    ``home_page_content_html`` is the round-trip pair for
    ``get_document(include_homepage_content_html=True)``. The HTML is
    sent verbatim with no sanitization, so XSS/script filtering is
    delegated to Polarion's renderer — NEVER pass untrusted input.
    Empty string is rejected (would wipe the body and orphan every
    heading); pass ``'<p></p>'`` for a near-empty body.

    Body-write behaviour:

    - **Heading auto-create**: inline ``<h1>..<h4>`` tags become heading
      work items with ``module`` and ``outline_number`` set automatically.
      A bare ``<hN>Title</hN>`` alone is safe.
    - **Orphan headings**: removing an ``<hN>`` removes the part but
      leaves the heading work item behind (still ``module``-linked, no
      ``outline_number``).
    - **DO NOT inject anchorless ``<p>`` paragraphs**: ``<h3>X</h3>
      <p>Body</p>`` lets the PATCH succeed but the next
      ``read_document_parts`` returns HTTP 500. Polarion's stored
      paragraphs all carry ``id="polarion_..."`` anchors; raw ``<p>``
      breaks server-side part derivation. The same applies to anchorless
      ``<ul>``, ``<ol>``, ``<table>``, ``<div>``, ``<blockquote>``, and
      ``<pre>``. The caller is responsible for stamping a unique
      non-empty ``id=`` on every such block before PATCH; ``<h1>..<h4>``
      do not need ids (Polarion rewrites them to a macro form on save).
      Unlike ``create_document`` no Markdown auto-stamping convenience is
      available on this path. For body text, create a new work item and
      attach via ``create_work_item`` + ``move_work_item_to_document``.
    - **DO NOT inject work item macro references**: appending
      ``<div id="polarion_wiki macro name=module-workitem;params=id=NEW">``
      creates a ``workitem_<NEW>`` part visible in
      ``read_document_parts`` but leaves the work item's ``module`` relationship
      unset (``space_id=""``, ``outline_number=""``) — an inconsistent
      half-attached state. Always attach via
      ``move_work_item_to_document``.

    Workflow: prefer ``workflow_action`` over a raw ``status`` edit so
    project rules run. ``workflow_action`` MUST be paired with at least
    one attribute field — Polarion rejects empty PATCH bodies. Direct
    ``status`` / ``type`` writes are NOT validated server-side: unknown
    ids are stored verbatim as ghost values. Resolve valid ids first via
    ``list_document_enum_options(project_id, field_id, document_type)``.
    Unknown ``custom_fields`` IDs also become ghost attributes; pass
    keys from a prior ``get_document``.

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

    if dry_run:
        return DocumentUpdateResult(
            updated=False,
            dry_run=True,
            payload_preview=payload,
        )

    client = get_client(ctx)
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
        # Pure additive operation per MCP spec -- creates a new document without
        # mutating existing data, so destructiveHint is False. Not idempotent
        # because retrying with the same module_name returns HTTP 409.
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
            "Optional custom fields keyed by Polarion field ID; "
            "rich-text values must be ``{'type':'text/html','value':...}``."
        ),
    ),
    dry_run: bool = Field(
        default=False,
        description="When True, return payload preview without calling Polarion.",
    ),
) -> DocumentCreateResult:
    """Create a new Polarion document in a space.

    Greenfield document creation. The document starts empty (or with the
    optional ``home_page_content`` body) and headings / work item parts
    can be added later via ``update_document`` and
    ``move_work_item_to_document``.

    Format asymmetry: ``home_page_content`` here is Markdown (converted
    to sanitized HTML on write) because greenfield authoring is natural
    for LLMs. After creation the round-trip pair is
    ``get_document(include_homepage_content_html=True)`` ↔
    ``update_document(home_page_content_html=...)`` which speaks raw HTML
    verbatim. The two formats never mix.

    When ``home_page_content`` is provided, every block-level element of
    the rendered HTML (``<p>``, ``<ul>``, ``<ol>``, ``<table>``, ``<div>``,
    ``<blockquote>``, ``<pre>``) is stamped with a unique
    ``id="polarion_mcp_N"`` anchor. Without these ids the document saves
    but the next ``read_document_parts`` returns HTTP 500. Headings
    ``<h1>..<h4>`` are intentionally skipped — Polarion rewrites their
    ids into a ``polarion_wiki macro name=module-workitem`` macro form on
    save.

    ``module_name`` is Polarion's persistent identifier within the space
    and is used in every subsequent URL (``get_document``,
    ``update_document``, etc.). It must be unique within ``space_id``;
    a duplicate name causes Polarion to return HTTP 409, surfaced here
    as ``RuntimeError``.

    Polarion does NOT validate enum membership server-side. Unknown
    ``type`` / ``status`` ids are stored verbatim as ghost values that
    look real on later reads but never match Lucene queries. Resolve
    valid ids first via ``list_document_enum_options(project_id,
    field_id, document_type)``. ``custom_fields`` is the same story:
    unknown field IDs silently persist as ghost attributes -- pass keys
    taken from a prior ``get_document``.

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

    client = get_client(ctx)
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

    # Drop any stale list_documents entry so the new document appears on the
    # very next call instead of waiting for the 60s TTL to expire.
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
        # Pure additive operation per MCP spec -- adds link records without
        # mutating existing work items. Not idempotent: retrying the same
        # (role, target) on an already-linked pair returns HTTP 409.
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
        description="One or more links to create under the source work item.",
    ),
    dry_run: bool = Field(
        default=False,
        description="When True, return payload preview without calling Polarion.",
    ),
) -> WorkItemLinksCreateResult:
    """Create one or more outgoing links from a single source work item.

    All links share the same source (``project_id`` / ``work_item_id``)
    and are sent as a single bulk JSON:API request. For each spec
    ``target_project_id`` defaults to ``project_id`` for the common
    same-project case; pass it explicitly only for cross-project links.
    The orientation matches ``list_work_item_links(direction="forward")``
    on the source.

    Polarion does not validate link roles server-side: an unknown ``role``
    is stored verbatim and never matches subsequent queries. Resolve valid
    roles by reading an existing link with
    ``list_work_item_links(direction="forward")`` on a similar work item
    in the same project.

    Each spec's ``revision`` pins the link to a specific Polarion revision
    when set, otherwise the link targets the current HEAD. ``suspect``
    marks the link as needing re-review (usually False for new links).

    The returned ``link_ids`` are five-segment composites
    ``<srcProj>/<srcWI>/<role>/<tgtProj>/<tgtWI>`` in input order, and
    are the path identifiers for future delete/PATCH of the same links
    via ``delete_work_item_links``.

    Bulk semantics -- partial-failure hazard: behavior on mixed-success
    (e.g. one duplicate among otherwise valid links) is not currently
    characterised on this server. On any 4xx response, assume nothing was
    committed and re-query with
    ``list_work_item_links(direction="forward")`` before retrying. If you
    need per-link diagnostics, send one spec per call.

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
        ValueError: Source project or work item not found.
        PermissionError: Token lacks permission.
        RuntimeError: Other Polarion API errors (including duplicate link),
            or accepted-but-no-ID response.
    """
    payload = _build_create_links_payload(
        source_project_id=project_id,
        links=links,
    )

    if dry_run:
        return WorkItemLinksCreateResult(
            created=False,
            dry_run=True,
            link_ids=[],
            payload_preview=payload,
        )

    client = get_client(ctx)
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
    if not link_ids:
        raise RuntimeError(
            "Polarion accepted the bulk create-link request but returned no "
            "link ids. The links may or may not exist; verify with "
            "list_work_item_links."
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
        # Destructive: removes existing link records. Idempotent at the
        # body level -- Polarion silently ignores ids that don't match
        # an existing link and returns 204 regardless.
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
        description="One or more existing outgoing links to delete.",
    ),
    dry_run: bool = Field(
        default=False,
        description="When True, return payload preview without calling Polarion.",
    ),
) -> WorkItemLinksDeleteResult:
    """Delete one or more outgoing links from a single source work item.

    Mirrors ``create_work_item_links``: same source coordinates, structured
    refs for each target. Only **outgoing** ("forward") links are removed
    through this endpoint. Back links are removed by calling this tool on
    the *other* work item (the one owning the outgoing side). External
    hyperlinks live on ``hyperlinks`` and are managed via
    ``update_work_item``.

    How to identify links:
    - From a prior ``create_work_item_links`` call: reuse the same specs
      (drop ``suspect`` / ``revision`` -- delete needs only role + target).
    - From ``list_work_item_links(direction="forward")``: each item's
      ``role`` and ``id`` (the target's short ID) form one ref;
      ``target_project_id`` defaults to ``project_id`` for same-project.

    Idempotent at the body level: Polarion silently ignores refs whose
    composite id does not match an existing link, deletes any refs that
    do match, and returns 204 either way. So re-deleting an
    already-removed link is a no-op, and a mixed batch (some real,
    some stale) succeeds for the real ones without surfacing the
    stale ones. ``ValueError`` is reserved for path-level 404 -- the
    source work item itself does not exist; the body-level "link not
    found" case never reaches the tool layer.

    Args:
        ctx: MCP tool context (injected automatically).
        project_id: Source work item's project ID.
        work_item_id: Source work item ID.
        links: One or more ``WorkItemLinkRef`` (role + target + optional
            target_project_id).
        dry_run: When True, return payload preview only.

    Returns:
        WorkItemLinksDeleteResult with ``deleted``, ``dry_run``,
        ``link_ids`` (composite ids that were/would be deleted, in input
        order, always populated since they are reconstructed from the
        request), and ``payload_preview`` (populated on dry-run; None on
        real delete).

    Raises:
        ValueError: Source work item itself not found (path-level 404).
            Body-level "link not found" is silently ignored by Polarion
            and does not raise.
        PermissionError: Token lacks permission.
        RuntimeError: Other Polarion API errors (e.g. 400 on a malformed
            composite id -- but this tool constructs valid ids from
            structured refs, so 400 should be unreachable).
    """
    link_ids, payload = _build_delete_links_payload(
        source_project_id=project_id,
        source_work_item_id=work_item_id,
        links=links,
    )

    if dry_run:
        return WorkItemLinksDeleteResult(
            deleted=False,
            dry_run=True,
            link_ids=link_ids,
            payload_preview=payload,
        )

    client = get_client(ctx)
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
        payload_preview=None,
    )


@mcp.tool(
    tags={"write"},
    timeout=60.0,
    annotations={
        # Not destructive (no deletion). Not idempotent: re-PATCHing with the
        # same values still increments Polarion's revision history.
        "readOnlyHint": False,
        "destructiveHint": False,
        "idempotentHint": False,
        "openWorldHint": True,
    },
)
async def update_work_item_links(  # noqa: PLR0913
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

    Use this to clear a ``suspect`` flag after a reviewer signs off, or to
    pin a link to a specific revision. Identify the link first with
    ``list_work_item_links(direction="forward")``: the ``role`` and target
    work item ID together address one specific link.

    ``suspect`` and ``revision`` are tri-state: an explicit ``True`` /
    ``False`` / string value updates that attribute, while ``None`` (the
    default) leaves the existing server-side value alone. This differs
    from ``create_work_item_links`` where ``suspect`` defaults to ``False``
    (a concrete value). At least one of ``suspect`` / ``revision`` must be
    provided -- passing both as ``None`` is rejected because Polarion
    would 400 on the resulting empty PATCH body.

    To update multiple links, call this tool once per link. Unlike
    ``create_work_item_links`` / ``delete_work_item_links``, the PATCH
    endpoint has no server-side bulk equivalent.

    Polarion does not validate link role ids server-side, so a typo in
    ``role`` returns 404 -- the link simply does not exist under that role.

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
        # Pure additive operation — creates new comments without mutating
        # existing data, so destructiveHint is False. Not idempotent
        # because retrying with the same input creates duplicate comments.
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

    All comments in ``comments`` are sent in a single POST to
    ``/projects/{p}/spaces/{s}/documents/{d}/comments``.  Polarion
    returns a 201 with the IDs of all created comments.

    **Thread model**: a comment with ``parent_comment_id=None`` is a
    top-level review comment.  To reply to an existing comment, set
    ``parent_comment_id`` to the short ID returned in
    ``list_document_comments`` (e.g. ``'c42'``); the tool composes the
    full four-segment path ``proj/space/doc/c42`` that the Polarion API
    requires.

    **Text format**: ``'text/plain'`` (default) stores the body
    verbatim.  Use ``'text/html'`` for HTML-formatted bodies — the HTML
    is sent as-is, no sanitization.

    **resolved**: omitting the field (``None``) lets Polarion default to
    ``False``; pass ``True`` to create a pre-resolved comment.

    **author_id**: omit to have Polarion use the authenticated token's
    user.

    This operation is NOT idempotent — retrying with the same input
    creates duplicate comments.

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

    Sends PATCH to
    ``/projects/{p}/spaces/{s}/documents/{d}/comments/{commentId}``
    with ``{"resolved": <bool>}`` — the only patchable attribute on a
    document comment. Use ``list_document_comments`` to discover the
    short comment ID (last segment of the full 4-part path).

    This operation is idempotent: marking a comment resolved twice leaves
    the same server state as marking it resolved once.

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
