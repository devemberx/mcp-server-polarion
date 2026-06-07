"""Document tools — query, read, create, and update."""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from typing import Final, Literal, cast
from urllib.parse import urlencode

from fastmcp import Context
from pydantic import Field

from mcp_server_polarion.core.client import PolarionClient
from mcp_server_polarion.core.exceptions import (
    PolarionAuthError,
    PolarionError,
    PolarionNotFoundError,
)
from mcp_server_polarion.models import (
    MAX_BODY_HTML_LEN,
    DocumentCreateResult,
    DocumentDetail,
    DocumentPart,
    DocumentReadResult,
    DocumentSummary,
    DocumentUpdateResult,
    EnumOption,
    JsonValue,
    PaginatedResult,
)
from mcp_server_polarion.server import mcp
from mcp_server_polarion.tools._shared.cache import (
    get_cached_documents,
    invalidate_documents_cache,
    record_document_custom_field_keys,
    store_cached_documents,
)
from mcp_server_polarion.tools._shared.guard import (
    guard_document_custom_field_keys,
    guard_document_enums,
)
from mcp_server_polarion.tools._shared.helpers import (
    DEFAULT_PAGE_SIZE,
    DOCUMENT_DETAIL_FIELDS,
    STANDARD_DOCUMENT_ATTRIBUTES,
    WORK_ITEM_PART_FIELDS,
    build_enum_option,
    build_included_work_item_map,
    compute_has_more,
    encode_path_segment,
    extract_custom_fields,
    extract_relationship_id,
    extract_total_count,
    get_client,
    merge_custom_fields,
    safe_str,
    split_module_id,
)
from mcp_server_polarion.utils import (
    first_anchorless_block,
    html_to_markdown,
    markdown_to_html,
    sanitize_html,
    stamp_block_ids,
)

logger = logging.getLogger("mcp_server_polarion.tools.documents")


type _PartType = Literal[
    "heading", "workitem", "normal", "toc", "wikiblock", "tof", "page_break"
]


_POLARION_PART_TYPES: Final[frozenset[str]] = frozenset(
    {"heading", "workitem", "normal", "toc", "wikiblock"},
)


_MAX_HEADING_LEVEL: Final[int] = 6


@dataclass(frozen=True, slots=True)
class _LinkedWorkItem:
    """Metadata extracted from an included work-item resource."""

    short_id: str = ""
    title: str = ""
    type: str = ""
    status: str = ""
    description_html: str = ""
    outline_number: str = ""


def _extract_html_value(field: object) -> str:
    """Extract the HTML payload from a Polarion text field.

    Polarion serialises rich-text fields either as a dict
    ``{"type": "text/html", "value": "..."}`` or, in some responses, as
    a plain string. Both shapes resolve to a string here.
    """
    if isinstance(field, dict):
        return safe_str(field.get("value", ""))
    if isinstance(field, str):
        return field
    return ""


def _resolve_heading_level(attributes: dict[str, object]) -> int:
    """Return the heading level for a heading part.

    Prefers ``attributes.level`` when present, otherwise falls back to
    parsing the leading ``<hN>`` tag in ``attributes.content``.
    """
    attr_level = attributes.get("level")
    if isinstance(attr_level, int):
        return attr_level
    head_html = _extract_html_value(attributes.get("content"))
    match = re.match(r"<h(\d)", head_html, re.IGNORECASE)
    return int(match.group(1)) if match else 0


def _resolve_linked_work_item(
    relationships: dict[str, object],
    work_item_map: dict[str, dict[str, object]],
) -> _LinkedWorkItem:
    """Return metadata for the work item linked from a document part."""
    work_item_full_id = extract_relationship_id(relationships, "workItem")
    if not work_item_full_id:
        return _LinkedWorkItem()

    short_id = (
        work_item_full_id.split("/", maxsplit=1)[-1]
        if "/" in work_item_full_id
        else work_item_full_id
    )
    work_item_attrs = work_item_map.get(work_item_full_id, {}).get("attributes", {})
    if not isinstance(work_item_attrs, dict):
        return _LinkedWorkItem(short_id=short_id)

    return _LinkedWorkItem(
        short_id=short_id,
        title=safe_str(work_item_attrs.get("title", "")),
        type=safe_str(work_item_attrs.get("type", "")),
        status=safe_str(work_item_attrs.get("status", "")),
        description_html=_extract_html_value(work_item_attrs.get("description")),
        outline_number=safe_str(work_item_attrs.get("outlineNumber", "")),
    )


def _parse_document_part(
    item: object,
    work_item_map: dict[str, dict[str, object]],
) -> DocumentPart | None:
    """Parse a single JSON:API document-part resource into a model.

    Args:
        item: A single resource object from the ``data`` array.
        work_item_map: Included work-item lookup built by
            ``build_included_work_item_map``.

    Returns:
        A ``DocumentPart`` instance, or ``None`` if *item* is invalid.
    """
    if not isinstance(item, dict):
        return None
    attributes = item.get("attributes", {})
    if not isinstance(attributes, dict):
        attributes = {}

    full_id = safe_str(item.get("id", ""))
    short_id = full_id.rsplit("/", maxsplit=1)[-1] if "/" in full_id else full_id

    raw_type = safe_str(attributes.get("type", ""))
    part_type: _PartType = cast(
        _PartType,
        raw_type if raw_type in _POLARION_PART_TYPES else "normal",
    )
    # Polarion reports TOF and page-break parts as plain ``normal``; the
    # kind is only encoded in the ID prefix.
    if part_type == "normal":
        if short_id.startswith("tof_"):
            part_type = "tof"
        elif short_id.startswith("pagebreak_"):
            part_type = "page_break"

    # Heading/workitem body lives in title/description, not attributes.content
    # (an empty <hN> stub there) — skip conversion to avoid emitting "#" noise.
    content_html = (
        _extract_html_value(attributes.get("content"))
        if part_type not in {"heading", "workitem"}
        else ""
    )
    level = _resolve_heading_level(attributes) if part_type == "heading" else 0

    relationships = item.get("relationships", {})
    if not isinstance(relationships, dict):
        relationships = {}
    linked = _resolve_linked_work_item(relationships, work_item_map)

    next_full_id = extract_relationship_id(relationships, "nextPart")
    next_short_id = (
        next_full_id.rsplit("/", maxsplit=1)[-1]
        if "/" in next_full_id
        else next_full_id
    )

    return DocumentPart(
        id=short_id,
        title=linked.title,
        content=html_to_markdown(content_html) if content_html else "",
        type=part_type,
        level=level,
        description=(
            html_to_markdown(linked.description_html) if part_type == "workitem" else ""
        ),
        work_item_id=linked.short_id,
        work_item_type=linked.type,
        work_item_status=linked.status,
        external=bool(attributes.get("external", False)),
        outline_number=linked.outline_number,
        next_part_id=next_short_id,
    )


_CONSTANT_CHUNKS: Final[dict[str, str]] = {
    "toc": "*[Table of Contents (Polarion widget)]*",
    "tof": "*[Table of Figures (Polarion widget)]*",
    "page_break": "---",
}


_WIKIBLOCK_MACRO_RE: Final[re.Pattern[str]] = re.compile(
    r"#([A-Za-z_][A-Za-z0-9_]*)\s*\("
)


_FENCED_MIN_LINES: Final[int] = 2


def _render_parts_to_markdown(parts: list[DocumentPart]) -> str:
    """Interleave a page of parts into a single flowing Markdown string.

    Rendering rules per ``DocumentPart.type``:

    - ``heading``: ``{'#' * level} {outline_number} {title}`` when the
      heading has an outline number (e.g. ``### 1.1 Purpose``), otherwise
      ``{'#' * level} {title}``. Level clamped to 1-6.
    - ``workitem``: bold lead-in ``**{title}** (`{work_item_id}`)``
      followed by the description body. Falls back to bare backticked
      ID when both title and description are empty.
    - ``normal``: ``content`` verbatim, skipped when whitespace-only.
      Tables stored as ``normal`` flow through unchanged.
    - ``wikiblock``: ``content`` verbatim, with the Velocity macro name
      lifted into the fenced-code info-string when detectable (e.g.
      ``#documentPanel(...)`` → ``` ```documentPanel ```). Falls back
      to the raw fenced block when no macro token is present.
    - ``page_break``: rendered as a ``---`` thematic break.
    - ``toc`` / ``tof``: rendered as a one-line italic placeholder.
      Widget content is not synthesised — heading text and figure
      captions already appear inline elsewhere.
    - Unknown types: skipped.

    Chunks are joined with a blank line; runs of three or more newlines
    are collapsed to two for visual parity with ``get_document``.
    """
    chunks: list[str] = []
    for part in parts:
        chunk = _render_part(part)
        if chunk:
            chunks.append(chunk)

    joined = "\n\n".join(chunks)
    return re.sub(r"\n{3,}", "\n\n", joined).strip()


def _render_part(part: DocumentPart) -> str:
    """Return the Markdown chunk for one part, or ``""`` to skip it."""
    constant = _CONSTANT_CHUNKS.get(part.type)
    if constant is not None:
        return constant
    if part.type == "heading":
        level = max(1, min(part.level or 1, _MAX_HEADING_LEVEL))
        title = (
            f"{part.outline_number} {part.title}" if part.outline_number else part.title
        )
        return f"{'#' * level} {title}"
    if part.type == "workitem":
        return _render_workitem_part(part)
    if part.type == "wikiblock":
        return _decorate_wikiblock(part.content)
    if part.type == "normal":
        return part.content if part.content.strip() else ""
    return ""


def _render_workitem_part(part: DocumentPart) -> str:
    """Format a ``workitem`` part's lead-in line and optional body."""
    if not part.title and not part.description:
        return f"`{part.work_item_id}`"
    if not part.description:
        return f"**{part.title}** (`{part.work_item_id}`)"
    return f"**{part.title}** (`{part.work_item_id}`)\n\n{part.description}"


def _decorate_wikiblock(content: str) -> str:
    """Lift the Velocity macro name into the fenced-code info-string.

    Wikiblock content arrives as ``` ```\\n#macroName(...)\\n``` ``` from
    ``markdownify``. Rewrap the fence so the macro name becomes the
    info-string, giving the LLM an unambiguous macro identifier. Falls
    back to the raw fence when no ``#name(`` token is detectable.
    """
    stripped = content.strip()
    if not stripped:
        return ""
    if not stripped.startswith("```"):
        return content
    lines = stripped.split("\n")
    if len(lines) < _FENCED_MIN_LINES or lines[-1].strip() != "```":
        return content
    body = "\n".join(lines[1:-1])
    match = _WIKIBLOCK_MACRO_RE.search(body)
    if not match:
        return content
    macro = match.group(1)
    return f"```{macro}\n{body}\n```"


def _get_module_id(item: object) -> str:
    """Extract the ``module`` relationship ID from a heading work item.

    Args:
        item: A single JSON:API resource object.

    Returns:
        The module ID string (e.g. ``projectId/spaceId/docName``),
        or ``""`` if not available.
    """
    if not isinstance(item, dict):
        return ""
    relationships = item.get("relationships", {})
    if not isinstance(relationships, dict):
        return ""
    return extract_relationship_id(relationships, "module")


def _extract_document_pair(
    item: object,
    documents: set[tuple[str, str]],
) -> None:
    """Parse ``module`` relationship from a heading work item and add the
    (space_id, document_name) pair to *documents*.

    The module relationship ``data.id`` has the format
    ``{projectId}/{spaceId}/{documentName}``.

    Args:
        item: A single JSON:API resource object from the ``data`` array.
        documents: Mutable set to collect unique (space, document) pairs into.
    """
    mod_id = _get_module_id(item)
    if mod_id:
        parts = mod_id.split("/")
        # Format: "projectId/spaceId/docName" → parts[1], parts[2:]
        if len(parts) >= 3:  # noqa: PLR2004
            space_id = parts[1]
            document_name = "/".join(parts[2:])
            documents.add((space_id, document_name))


async def _discover_documents(
    client: PolarionClient,
    project_id: str,
) -> list[tuple[str, str]]:
    """Discover all unique (space_id, document_name) pairs via linear scan.

    Iterates every heading-workitem page (page_size=100) and accumulates
    unique ``module`` relationship IDs. Results are TTL-cached in
    ``tools._shared.helpers`` so paginated callers reuse the discovery.

    Args:
        client: Active ``PolarionClient`` instance.
        project_id: Polarion project ID.

    Returns:
        Sorted list of (space_id, document_name) tuples.
    """
    cached = get_cached_documents(project_id)
    if cached is not None:
        return cached

    base_params: dict[str, str | int] = {
        "fields[workitems]": "module",
        "query": "type:heading",
        "page[size]": DEFAULT_PAGE_SIZE,
    }

    documents: set[tuple[str, str]] = set()
    page_number = 1

    while True:
        response = await client.get(
            f"/projects/{encode_path_segment(project_id)}/workitems",
            params={**base_params, "page[number]": page_number},
        )
        data = response.get("data", [])
        if not isinstance(data, list) or not data:
            break

        for item in data:
            _extract_document_pair(item, documents)

        raw_total = extract_total_count(response)
        if not compute_has_more(
            response, raw_total, page_number, DEFAULT_PAGE_SIZE, len(data)
        ):
            break
        page_number += 1

    sorted_docs = sorted(documents)
    store_cached_documents(project_id, sorted_docs)
    return sorted_docs


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


@mcp.tool(
    tags={"read"},
    timeout=300.0,
    annotations={"readOnlyHint": True},
)
async def list_documents(
    ctx: Context,
    project_id: str = Field(description="Polarion project ID."),
    page_size: int = Field(default=DEFAULT_PAGE_SIZE, ge=1, le=100),
    page_number: int = Field(default=1, ge=1),
) -> PaginatedResult[DocumentSummary]:
    """List all documents in a Polarion project.

    Returns ``(space_id, document_name)`` pairs that other document tools
    accept. The first call per project runs a full discovery scan cached for
    60s, so paginated follow-ups are cheap.

    Args:
        ctx: MCP tool context (injected automatically).
        project_id: Polarion project ID.
        page_size: Items per page (1-100, default 100).
        page_number: 1-based page number (default 1).

    Returns:
        PaginatedResult of ``DocumentSummary`` items with ``space_id``
        and ``document_name``.

    Raises:
        ValueError: Project not found.
        PermissionError: Token lacks permission.
        RuntimeError: Other Polarion API errors.
    """
    client = get_client(ctx)

    try:
        documents = await _discover_documents(client, project_id)
    except PolarionNotFoundError as exc:
        raise ValueError(
            f"Project '{project_id}' not found. "
            "Use `list_projects` to discover valid project IDs."
        ) from exc
    except PolarionAuthError as exc:
        raise PermissionError(
            "Cannot list documents -- check your POLARION_TOKEN permissions."
        ) from exc
    except PolarionError as exc:
        raise RuntimeError(
            f"Failed to list documents for project '{project_id}': {exc.message}"
        ) from exc

    total = len(documents)

    start = (page_number - 1) * page_size
    end = start + page_size
    page_slice = documents[start:end]

    items = [DocumentSummary(space_id=s, document_name=d) for s, d in page_slice]

    return PaginatedResult[DocumentSummary](
        items=items,
        total_count=total,
        page=page_number,
        page_size=page_size,
        has_more=end < total,
    )


@mcp.tool(
    tags={"read"},
    timeout=60.0,
    annotations={"readOnlyHint": True},
)
async def get_document(
    ctx: Context,
    project_id: str = Field(description="Polarion project ID."),
    space_id: str = Field(
        description="Space ID containing the document (e.g. '_default').",
    ),
    document_name: str = Field(
        description="Document name within the space (spaces handled automatically).",
    ),
    include_homepage_content_html: bool = Field(
        default=False,
        description=(
            "When True, fill ``content_html`` with raw HTML for round-trip editing."
        ),
    ),
) -> DocumentDetail:
    """Get a document's metadata (and optionally its raw body source).

    Returns title, type, status, and custom fields. With
    ``include_homepage_content_html=True``, ``content_html`` carries
    ``homePageContent`` as raw Polarion HTML — the round-trip shape for
    ``update_document(home_page_content_html=...)`` (no Markdown/sanitization).

    ``homePageContent`` is the inline prose only — heading text and embedded
    work-item bodies are separate work items; use ``read_document`` for
    end-to-end reading, ``read_document_parts`` for structure. Only feed
    ``content_html`` back to ``update_document`` when the read flag was True
    (False blanks it, and the empty string is rejected on write).

    Args:
        ctx: MCP tool context (injected automatically).
        project_id: Polarion project ID.
        space_id: Space ID containing the document.
        document_name: Document name within the space.
        include_homepage_content_html: When True, also return the raw
            ``homePageContent`` HTML in ``content_html``. Default False
            to keep the response small.

    Returns:
        DocumentDetail with ``title``, ``type``, ``status``,
        ``content_html`` (only when the flag is True; otherwise empty),
        and ``custom_fields`` (``{fieldId: value}``; rich-text values
        are returned as ``{type: 'text/html', value: ...}`` dicts).

    Raises:
        ValueError: Document, space, or project not found.
        PermissionError: Token lacks permission.
        RuntimeError: Other Polarion API errors.
    """
    client = get_client(ctx)
    path = (
        f"/projects/{encode_path_segment(project_id)}"
        f"/spaces/{encode_path_segment(space_id)}"
        f"/documents/{encode_path_segment(document_name)}"
    )

    # DOCUMENT_DETAIL_FIELDS uses the ``@all`` token; an explicit field list
    # silently drops inline custom attributes on this server.
    try:
        response = await client.get(
            path,
            params={"fields[documents]": DOCUMENT_DETAIL_FIELDS},
        )
    except PolarionNotFoundError as exc:
        raise ValueError(
            f"Document '{document_name}' not found in space "
            f"'{space_id}' of project '{project_id}'. "
            "Use `list_documents` to verify the space ID and document name."
        ) from exc
    except PolarionAuthError as exc:
        raise PermissionError(
            "Cannot access document -- check your POLARION_TOKEN permissions."
        ) from exc
    except PolarionError as exc:
        raise RuntimeError(
            f"Failed to get document '{document_name}': {exc.message}"
        ) from exc

    data = response.get("data", {})
    if not isinstance(data, dict):
        data = {}
    attributes = data.get("attributes", {})
    if not isinstance(attributes, dict):
        attributes = {}

    content_html = ""
    if include_homepage_content_html:
        # Pass homePageContent {"type","value"} through verbatim so it
        # round-trips through update_document(home_page_content_html=...).
        content_obj = attributes.get("homePageContent", {})
        if isinstance(content_obj, dict):
            content_html = safe_str(content_obj.get("value", ""))

    detail = DocumentDetail(
        title=safe_str(attributes.get("title", "")),
        type=safe_str(attributes.get("type", "")),
        status=safe_str(attributes.get("status", "")),
        content_html=content_html,
        custom_fields=extract_custom_fields(attributes, STANDARD_DOCUMENT_ATTRIBUTES),
    )
    # Prime the guard cache so update_document.custom_fields can validate keys.
    record_document_custom_field_keys(
        project_id, space_id, document_name, detail.custom_fields.keys()
    )
    return detail


@mcp.tool(
    tags={"read"},
    timeout=60.0,
    annotations={"readOnlyHint": True},
)
async def list_document_enum_options(  # noqa: PLR0913
    ctx: Context,
    project_id: str = Field(description="Polarion project ID."),
    field_id: str = Field(
        description=("Field id (e.g. 'status', 'type', or a custom field id)."),
    ),
    document_type: str = Field(
        description=(
            "Document type id (e.g. 'systemReqSpecification')."
            " Pass '~' for type-agnostic options."
        ),
    ),
    page_size: int = Field(default=DEFAULT_PAGE_SIZE, ge=1, le=100),
    page_number: int = Field(default=1, ge=1),
) -> PaginatedResult[EnumOption]:
    """List valid enum options for a document field of the given document type.

    Call before ``update_document`` to resolve a ``status`` / ``type`` /
    custom-enum value. Polarion does NOT validate these on write (unknown ids
    persist as ghosts), so resolve first. Document fields only — use
    ``list_work_item_enum_options`` for work items. Returns the FULL set (not
    filtered by current workflow state); ``document_type='~'`` is the
    type-agnostic set, and an unknown type is silently treated as ``~``, so
    verify the type id (e.g. via ``get_document``) first.

    Args:
        ctx: MCP tool context (injected automatically).
        project_id: Polarion project ID.
        field_id: Field id whose options to list.
        document_type: Document type id, or '~' for type-agnostic.
        page_size: Items per page (1-100, default 100).
        page_number: 1-based page number (default 1).

    Returns:
        PaginatedResult of ``EnumOption`` items with:
        - ``id``: Option id to pass back to write tools.
        - ``name``: Human-readable display name.
        - ``description``: Option description; empty when Polarion has none.
        - ``default``: True if Polarion uses this option as the default.
        - ``hidden``: True if the option is hidden in the UI.
        - ``terminal``: For status fields, True for workflow end-states.

    Raises:
        ValueError: Project or field not found. An unknown
            ``document_type`` does NOT raise; Polarion silently
            falls back to the ``~`` set.
        PermissionError: Token lacks permission.
        RuntimeError: Other Polarion API errors.
    """
    client = get_client(ctx)
    path = (
        f"/projects/{encode_path_segment(project_id)}"
        f"/documents/fields/{encode_path_segment(field_id)}"
        "/actions/getAvailableOptions"
    )
    params: dict[str, str | int] = {
        "type": document_type,
        "page[size]": page_size,
        "page[number]": page_number,
    }
    try:
        response = await client.get(path, params=params)
    except PolarionNotFoundError as exc:
        raise ValueError(
            f"No enum options for field '{field_id}' on document type "
            f"'{document_type}' in project '{project_id}'."
        ) from exc
    except PolarionAuthError as exc:
        raise PermissionError(
            "Cannot list document enum options"
            " -- check your POLARION_TOKEN permissions."
        ) from exc
    except PolarionError as exc:
        raise RuntimeError(
            f"Failed to list enum options for field '{field_id}': {exc.message}"
        ) from exc

    data = response.get("data", [])
    items: list[EnumOption] = []
    if isinstance(data, list):
        for entry in data:
            if isinstance(entry, dict):
                items.append(build_enum_option(entry))

    raw_total = extract_total_count(response)
    total = raw_total
    if total <= 0 and items:
        total = (page_number - 1) * page_size + len(items)

    return PaginatedResult[EnumOption](
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
async def read_document_parts(  # noqa: PLR0913
    ctx: Context,
    project_id: str = Field(description="Polarion project ID."),
    space_id: str = Field(description="Space ID containing the document."),
    document_name: str = Field(description="Document name within the space."),
    page_size: int = Field(default=DEFAULT_PAGE_SIZE, ge=1, le=100),
    page_number: int = Field(default=1, ge=1),
) -> PaginatedResult[DocumentPart]:
    """List the structural parts of a document in order.

    Use this for part IDs (``move_work_item_to_document``), heading levels, or
    per-work-item type/status; each ``workitem`` part already carries its
    ``description`` as Markdown, so no follow-up ``get_work_item`` is needed
    when scanning bodies. For plain reading prefer ``read_document``. To filter
    work items by type/status/title/custom-field (e.g. "non-heading only"),
    prefer ``list_work_items`` with a ``SQL:(...)`` query (smaller payload, no
    per-part pagination) — recipes via the ``get_sql_query_recipes`` tool.

    Args:
        ctx: MCP tool context (injected automatically).
        project_id: Polarion project ID.
        space_id: Space ID containing the document.
        document_name: Document name within the space.
        page_size: Items per page (1-100, default 100).
        page_number: 1-based page number (default 1).

    Returns:
        PaginatedResult of ``DocumentPart`` with:
        - ``id``: Short part identifier (e.g. 'heading_MCPT-001',
          'workitem_MCPT-042', 'polarion_1'). Pass to
          ``move_work_item_to_document`` as ``previous_part_id`` /
          ``next_part_id``.
        - ``title``: Linked work-item title (heading/workitem parts).
        - ``content``: Part body as Markdown; empty for heading and
          workitem parts (text lives in ``title``/``level`` and
          ``description``).
        - ``type``: 'heading' | 'workitem' | 'normal' | 'toc' | 'wikiblock'
          | 'tof' | 'page_break'. The last two are inferred from the part
          ID prefix because Polarion reports both as plain 'normal'.
        - ``level``: Heading level 1-4 (0 for non-headings).
        - ``description``: Markdown body (workitem parts only).
        - ``work_item_id`` / ``work_item_type`` / ``work_item_status``:
          Linked work-item metadata.
        - ``external``: True when the work item belongs to another project.
        - ``outline_number``: Hierarchical position (e.g. '1.2.3') for
          heading and workitem parts; empty otherwise.
        - ``next_part_id``: Short ID of the next part; empty on the last.

    Raises:
        ValueError: Document, space, or project not found.
        PermissionError: Token lacks permission.
        RuntimeError: Other Polarion API errors.
    """
    client = get_client(ctx)
    path = (
        f"/projects/{encode_path_segment(project_id)}"
        f"/spaces/{encode_path_segment(space_id)}"
        f"/documents/{encode_path_segment(document_name)}/parts"
    )

    try:
        response = await client.get(
            path,
            params={
                "fields[document_parts]": "@all",
                # Narrow workitem fields — ``@all`` would ship every inline
                # custom field (KBs/page) the part never uses.
                "fields[workitems]": WORK_ITEM_PART_FIELDS,
                "include": "workItem",
                "page[size]": page_size,
                "page[number]": page_number,
            },
        )
    except PolarionNotFoundError as exc:
        raise ValueError(
            f"Document '{document_name}' not found in space "
            f"'{space_id}' of project '{project_id}'. "
            "Use `list_documents` to discover valid space IDs and document names."
        ) from exc
    except PolarionAuthError as exc:
        raise PermissionError(
            "Cannot access document parts -- check your POLARION_TOKEN permissions."
        ) from exc
    except PolarionError as exc:
        raise RuntimeError(
            f"Failed to get parts for '{document_name}': {exc.message}"
        ) from exc

    work_item_map = build_included_work_item_map(response)

    data = response.get("data", [])
    items: list[DocumentPart] = []
    if isinstance(data, list):
        for item in data:
            part = _parse_document_part(item, work_item_map)
            if part is not None:
                items.append(part)

    # Fall back to the seen-item count only when the server gives no usable
    # total and the page is non-empty, else an out-of-range page inflates it.
    raw_doc_total = extract_total_count(response)
    document_total = raw_doc_total
    if document_total <= 0 and items:
        document_total = (page_number - 1) * page_size + len(items)

    return PaginatedResult[DocumentPart](
        items=items,
        total_count=document_total,
        page=page_number,
        page_size=page_size,
        has_more=compute_has_more(
            response, raw_doc_total, page_number, page_size, len(items)
        ),
    )


@mcp.tool(
    tags={"read"},
    timeout=60.0,
    annotations={"readOnlyHint": True},
)
async def read_document(  # noqa: PLR0913
    ctx: Context,
    project_id: str = Field(description="Polarion project ID."),
    space_id: str = Field(description="Space ID containing the document."),
    document_name: str = Field(description="Document name within the space."),
    page_size: int = Field(default=DEFAULT_PAGE_SIZE, ge=1, le=100),
    page_number: int = Field(default=1, ge=1),
) -> DocumentReadResult:
    """Render a Polarion document end-to-end as flowing Markdown.

    Paginates ``read_document_parts`` internally and interleaves heading titles,
    embedded work-item descriptions, and inline prose into one Markdown stream —
    the canonical way to read a document body (empty placeholders skipped).

    Output is read-only synthesis: do NOT feed it back to ``update_document``
    (the Markdown collapses Polarion's ID-anchored placeholders and the
    round-trip would orphan headings). Use
    ``get_document(include_homepage_content_html=True)`` for raw-HTML round-trip
    editing. For metadata-only extraction (ids / types / statuses / custom
    fields) prefer ``list_work_items`` with a ``SQL:(...)`` query, since this
    tool always materializes full body Markdown.

    Args:
        ctx: MCP tool context (injected automatically).
        project_id: Polarion project ID.
        space_id: Space ID containing the document.
        document_name: Document name within the space.
        page_size: Parts per page (1-100, default 100).
        page_number: 1-based page number (default 1).

    Returns:
        DocumentReadResult with ``content`` (Markdown for the page),
        ``part_count``, and pagination metadata
        (``page`` / ``page_size`` / ``total_parts`` / ``has_more``).

    Raises:
        ValueError: Document, space, or project not found.
        PermissionError: Token lacks permission.
        RuntimeError: Other Polarion API errors.
    """
    # Delegate fetch + error mapping to read_document_parts (the @mcp.tool
    # decorator returns the original function, so it forwards directly).
    page = await read_document_parts(
        ctx,
        project_id=project_id,
        space_id=space_id,
        document_name=document_name,
        page_size=page_size,
        page_number=page_number,
    )
    return DocumentReadResult(
        content=_render_parts_to_markdown(page.items),
        part_count=len(page.items),
        page=page.page,
        page_size=page.page_size,
        total_parts=page.total_count,
        has_more=page.has_more,
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
