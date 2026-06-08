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
    """Extract HTML from a Polarion text field (``{type,value}`` dict or str)."""
    if isinstance(field, dict):
        return safe_str(field.get("value", ""))
    if isinstance(field, str):
        return field
    return ""


def _resolve_heading_level(attributes: dict[str, object]) -> int:
    """Return a heading part's level: ``attributes.level``, else parsed ``<hN>``."""
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
    """Parse a JSON:API document-part resource into a model; ``None`` if invalid."""
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
    # Polarion reports TOF / page-break as ``normal``; kind is in the ID prefix.
    if part_type == "normal":
        if short_id.startswith("tof_"):
            part_type = "tof"
        elif short_id.startswith("pagebreak_"):
            part_type = "page_break"

    # Heading/workitem body lives in title/description, not attributes.content
    # (empty <hN> stub) — skip conversion to avoid "#" noise.
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
    """Interleave a page of parts into one flowing Markdown string.

    Per-type rendering lives in ``_render_part``. Chunks join on a blank line;
    runs of 3+ newlines collapse to 2.
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

    ``#macroName(...)`` in a fence → ` ```macroName `. Falls back to the raw
    fence when no ``#name(`` token is detectable.
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
    """Return a heading work item's ``module`` ID (``proj/space/doc``), or ``""``."""
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
    """Add a heading work item's (space_id, document_name) to *documents*.

    Module ``data.id`` format: ``{projectId}/{spaceId}/{documentName}``.
    """
    mod_id = _get_module_id(item)
    if mod_id:
        parts = mod_id.split("/")
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
    unique ``module`` relationship IDs. TTL-cached so paginated callers reuse
    the discovery.
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

    ``id`` is ``projectId/spaceId/documentName`` (name may contain ``/``).
    ``None`` on an unexpected shape.
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
    """Build the JSON:API PATCH body for ``.../documents/{d}``.

    Single ``data`` object, required ``id`` ``"{project}/{space}/{document}"``,
    skips unset. ``home_page_content_html`` wrapped verbatim into
    ``{type,value}`` (empty-string guard lives in the tool layer).
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
    """Build the JSON:API POST body for ``.../spaces/{s}/documents``.

    ``data`` list, ``type=documents``, inline ``attributes``, skips unset.
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

    Returns ``(space_id, document_name)`` pairs other document tools accept.
    First call per project runs a discovery scan cached 60s.
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

    Returns title/type/status/custom fields. With
    ``include_homepage_content_html=True``, ``content_html`` carries
    ``homePageContent`` as raw HTML — the round-trip shape for
    ``update_document``. ``homePageContent`` is inline prose only (headings and
    embedded work items are separate); use ``read_document`` end-to-end. Feed
    ``content_html`` back only when the flag was True (False blanks it; ``""``
    rejected on write).
    """
    client = get_client(ctx)
    path = (
        f"/projects/{encode_path_segment(project_id)}"
        f"/spaces/{encode_path_segment(space_id)}"
        f"/documents/{encode_path_segment(document_name)}"
    )

    # ``@all`` token: an explicit field list silently drops inline customs here.
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
        # Verbatim {type,value} so it round-trips through update_document.
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
    custom-enum value — Polarion does NOT validate on write (unknown ids
    persist as ghosts). Document fields only. Returns the FULL set;
    ``document_type='~'`` is type-agnostic, and an unknown type silently falls
    back to ``~``, so verify the type id first.
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

    Use for part IDs (``move_work_item_to_document``), heading levels, or
    per-work-item type/status; each ``workitem`` part already carries its
    ``description`` as Markdown. For plain reading prefer ``read_document``; to
    filter by type/status/custom-field prefer ``list_work_items`` with a
    ``SQL:(...)`` query (recipes via ``get_sql_query_recipes``).
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
                # Narrow workitem fields — ``@all`` ships unused inline customs.
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

    # Seen-item count only when the server gives no usable total and the page
    # is non-empty, else an out-of-range page inflates it.
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

    Paginates ``read_document_parts`` and interleaves headings, work-item
    descriptions, and prose into one stream — the canonical way to read a body.
    Read-only synthesis: do NOT feed back to ``update_document`` (collapses
    ID anchors, orphans headings); use
    ``get_document(include_homepage_content_html=True)`` for round-trip. For
    metadata-only extraction prefer ``list_work_items`` SQL.
    """
    # read_document_parts handles fetch + error mapping (decorator returns the
    # original function, so it forwards directly).
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


async def _resolve_document_type(
    client: PolarionClient,
    project_id: str,
    space_id: str,
    document_name: str,
) -> str:
    """Resolve the ``type`` axis the custom-field guard keys on. Runs on dry_run
    too, so the preview raises the same not-found / auth errors as the real write.
    """
    path = (
        f"/projects/{encode_path_segment(project_id)}"
        f"/spaces/{encode_path_segment(space_id)}"
        f"/documents/{encode_path_segment(document_name)}"
    )
    try:
        response = await client.get(path, params={"fields[documents]": "type"})
    except PolarionNotFoundError as exc:
        raise ValueError(
            f"Document '{document_name}' not found in space '{space_id}' of project "
            f"'{project_id}'. Use `list_documents` to verify the space ID and name."
        ) from exc
    except PolarionAuthError as exc:
        raise PermissionError(
            "Cannot read document -- check your POLARION_TOKEN permissions."
        ) from exc
    except PolarionError as exc:
        raise RuntimeError(
            f"Failed to read document type for guard: {exc.message}"
        ) from exc
    data = response.get("data", {})
    attrs = data.get("attributes", {}) if isinstance(data, dict) else {}
    return safe_str(attrs.get("type", "")) if isinstance(attrs, dict) else ""


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

    PATCHes only set attributes (omitted preserved); no follow-up GET — call
    ``get_document`` for refreshed state. ``home_page_content_html`` is the
    round-trip pair for ``get_document(include_homepage_content_html=True)``,
    verbatim, no sanitization (NEVER pass untrusted input). Empty string
    rejected (orphans headings); pass ``'<p></p>'`` for near-empty.

    Body-write rules:

    - **Headings auto-create**: inline ``<h1>..<h4>`` become heading work items
      with ``module`` / ``outline_number`` set; bare ``<hN>Title</hN>`` is safe.
    - **Anchorless blocks break parts**: ``<h3>X</h3><p>Body</p>`` PATCHes 200
      but the next ``read_document_parts`` returns HTTP 500. Every anchorless
      ``<p>``/``<ul>``/``<ol>``/``<table>``/``<div>``/``<blockquote>``/``<pre>``
      needs a unique ``id=`` (tool raises ``ValueError`` before PATCH, on
      dry_run too). No auto-stamping here; for body text prefer
      ``create_work_items`` + ``move_work_item_to_document``.
    - **No macro references**: a ``polarion_wiki macro name=module-workitem``
      ``<div>`` makes a part but leaves the work item's ``module`` unset (half
      attached) — attach via ``move_work_item_to_document``.

    Prefer ``workflow_action`` over raw ``status``; it MUST pair with ≥1
    attribute (empty PATCH 400s). Unknown ``status`` / ``type`` raise
    ``ValueError``; a ``custom_fields`` key absent from the document type's
    sampled schema is rejected (a type with no populated customs blocks it).
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
    # Guard type/status against type-agnostic options: avoids an extra GET,
    # still catches obvious ghost ids.
    await guard_document_enums(
        client,
        project_id,
        document_type="~",
        type=type,
        status=status,
    )

    # Build first: merge_custom_fields rejects standard-attribute collisions —
    # more fundamental, and cheaper than the guard's GET.
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
    # Unknown custom-field keys persist as silent ghosts; validate against the
    # type's schema. A type change keys on the new type, else the current one.
    if custom_fields:
        effective_type = type or await _resolve_document_type(
            client, project_id, space_id, document_name
        )
        await guard_document_custom_field_keys(
            client, project_id, effective_type, custom_fields
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

    First call ``list_documents`` and confirm ``module_name`` is unique in the
    space (a duplicate returns HTTP 409). Supply only enum ids from
    ``list_document_enum_options`` — unverified ids persist as ghosts.
    ``custom_fields`` keys are validated against the document type's existing
    schema; a key no document of that ``type`` uses is rejected.

    Starts empty (or with optional ``home_page_content``); add parts later via
    ``update_document`` / ``move_work_item_to_document``. ``module_name`` is the
    persistent URL identifier.

    ``home_page_content`` is Markdown → sanitized HTML; post-create round-trip is
    raw HTML via ``get_document(include_homepage_content_html=True)`` ↔
    ``update_document``. Each block element is stamped a unique
    ``id="polarion_mcp_N"`` (else the next ``read_document_parts`` 500s);
    ``<h1>..<h4>`` are skipped (rewritten to macro form on save).
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
        await guard_document_custom_field_keys(client, project_id, type, custom_fields)

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
