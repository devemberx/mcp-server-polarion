"""Document tools — query, read, create, update, and copy."""

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
    DocumentCopyResult,
    DocumentCreateResult,
    DocumentDetail,
    DocumentPart,
    DocumentReadResult,
    DocumentSummary,
    DocumentUpdateResult,
    JsonValue,
    PaginatedResult,
)
from mcp_server_polarion.server import mcp
from mcp_server_polarion.tools._shared.cache import (
    DiscoveredDocument,
    get_cached_documents,
    invalidate_documents_cache,
    store_cached_documents,
)
from mcp_server_polarion.tools._shared.custom_fields import (
    STANDARD_DOCUMENT_ATTRIBUTES,
    extract_custom_fields,
    merge_custom_fields,
)
from mcp_server_polarion.tools._shared.fields import (
    DOCUMENT_DETAIL_FIELDS,
    WORK_ITEM_PART_FIELDS,
)
from mcp_server_polarion.tools._shared.guard import (
    guard_document_custom_fields,
    guard_document_enums,
    guard_work_item_link_roles,
)
from mcp_server_polarion.tools._shared.helpers import (
    encode_path_segment,
    get_client,
    safe_str,
)
from mcp_server_polarion.tools._shared.pagination import (
    DEFAULT_PAGE_SIZE,
    compute_has_more,
    extract_total_count,
    make_page,
)
from mcp_server_polarion.tools._shared.parse import (
    extract_relationship_id,
    extract_short_id,
    parse_included_user_name_map,
    parse_included_work_item_map,
    split_module_id,
)
from mcp_server_polarion.tools._shared.sql import one_heading_per_document_sql
from mcp_server_polarion.utils import (
    first_anchorless_block,
    html_to_markdown,
    markdown_to_html,
    polarionify_html,
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
    """Extract HTML from Polarion text field (``{type,value}`` dict or str)."""
    if isinstance(field, dict):
        return safe_str(field.get("value", ""))
    if isinstance(field, str):
        return field
    return ""


def _resolve_heading_level(attributes: dict[str, object]) -> int:
    """Heading part level: ``attributes.level``, else parsed ``<hN>``."""
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
    """Metadata for work item linked from document part."""
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
    """JSON:API document-part resource → model; ``None`` if invalid."""
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
    # Polarion report TOF / page-break as ``normal``; kind live in ID prefix.
    if part_type == "normal":
        if short_id.startswith("tof_"):
            part_type = "tof"
        elif short_id.startswith("pagebreak_"):
            part_type = "page_break"

    # Heading/workitem body live in title/description; content = empty <hN>
    # stub — skip conversion, avoid "#" noise.
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
    """Interleave page of parts into one Markdown string; chunks join on
    blank line, runs of 3+ newlines collapse to 2.
    """
    chunks: list[str] = []
    for part in parts:
        chunk = _render_part(part)
        if chunk:
            chunks.append(chunk)

    joined = "\n\n".join(chunks)
    return re.sub(r"\n{3,}", "\n\n", joined).strip()


def _render_part(part: DocumentPart) -> str:
    """Markdown chunk for one part, or ``""`` to skip."""
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
    """``workitem`` part lead-in line + optional body."""
    if not part.title and not part.description:
        return f"`{part.work_item_id}`"
    if not part.description:
        return f"**{part.title}** (`{part.work_item_id}`)"
    return f"**{part.title}** (`{part.work_item_id}`)\n\n{part.description}"


def _decorate_wikiblock(content: str) -> str:
    """Lift Velocity macro name (``#name(``) into fenced-code info-string;
    raw fence when none detectable.
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


@dataclass(frozen=True, slots=True)
class _DocumentMeta:
    """Document attributes + editor user ids; ids = join keys into included
    ``users`` for display names, surfaced short-form for requery too.
    """

    type: str = ""
    status: str = ""
    updated: str = ""
    author_id: str = ""
    updated_by_id: str = ""


def _extract_document_from_module(
    resource: object,
    documents: dict[tuple[str, str], _DocumentMeta],
) -> None:
    """Record an included ``module`` resource as (space_id, document_name) → meta.

    Attributes/relationships only present because discovery name them in
    ``fields[documents]``; plain ``module`` relationship carry id only.
    """
    if not isinstance(resource, dict) or resource.get("type") != "documents":
        return
    space_id, document_name = split_module_id(safe_str(resource.get("id", "")))
    if not space_id or not document_name:
        return
    attrs = resource.get("attributes")
    if not isinstance(attrs, dict):
        attrs = {}
    relationships = resource.get("relationships")
    if not isinstance(relationships, dict):
        relationships = {}
    documents[(space_id, document_name)] = _DocumentMeta(
        type=safe_str(attrs.get("type", "")),
        status=safe_str(attrs.get("status", "")),
        updated=safe_str(attrs.get("updated", "")),
        author_id=extract_relationship_id(relationships, "author"),
        updated_by_id=extract_relationship_id(relationships, "updatedBy"),
    )


async def _discover_documents(
    client: PolarionClient,
    project_id: str,
) -> list[DiscoveredDocument]:
    """Discover every document in *project_id* with display metadata; TTL-cached.

    ``GROUP BY mod.c_uri`` SQL collapse headings to one per document (pages of
    documents, not headings). ``module`` must be listed alongside its dot-path
    includes — dot-path alone drop the intermediate resource.
    """
    cached = get_cached_documents(project_id)
    if cached is not None:
        return cached

    base_params: dict[str, str | int] = {
        "fields[workitems]": "module",
        "include": "module,module.author,module.updatedBy",
        "fields[documents]": "type,status,updated,author,updatedBy",
        "fields[users]": "name",
        "query": one_heading_per_document_sql(project_id),
        "page[size]": DEFAULT_PAGE_SIZE,
    }

    documents: dict[tuple[str, str], _DocumentMeta] = {}
    user_names: dict[str, str] = {}
    page_number = 1

    while True:
        response = await client.get(
            f"/projects/{encode_path_segment(project_id)}/workitems",
            params={**base_params, "page[number]": page_number},
        )
        data = response.get("data", [])
        if not isinstance(data, list) or not data:
            break

        included = response.get("included", [])
        if isinstance(included, list):
            for resource in included:
                _extract_document_from_module(resource, documents)
        user_names.update(parse_included_user_name_map(response))

        raw_total = extract_total_count(response)
        if not compute_has_more(
            response, raw_total, page_number, DEFAULT_PAGE_SIZE, len(data)
        ):
            break
        page_number += 1

    sorted_docs = sorted(
        DiscoveredDocument(
            space_id=space,
            document_name=name,
            type=meta.type,
            status=meta.status,
            updated=meta.updated,
            author_name=user_names.get(meta.author_id, ""),
            updated_by_name=user_names.get(meta.updated_by_id, ""),
        )
        for (space, name), meta in documents.items()
    )
    store_cached_documents(project_id, sorted_docs)
    return sorted_docs


def _extract_first_resource_id(response: dict[str, object]) -> str | None:
    """First resource id from JSON:API ``{"data": [...]}`` body, else ``None``."""
    data = response.get("data")
    if not isinstance(data, list) or not data:
        return None
    first = data[0]
    if not isinstance(first, dict):
        return None
    full_id = safe_str(first.get("id", ""))
    return full_id or None


def _extract_created_module_name(response: dict[str, object]) -> str | None:
    """Document name from 201 create response (name may contain ``/``);
    ``None`` on unexpected shape.
    """
    full_id = _extract_first_resource_id(response)
    if full_id is None:
        return None
    _, document_name = split_module_id(full_id)
    return document_name or None


def _extract_copied_module_id(response: dict[str, object]) -> str | None:
    """Full module id from copy-action 201; ``data`` = single dict (not the
    list create returns), so ``_extract_first_resource_id`` not fit.
    """
    data = response.get("data")
    if not isinstance(data, dict):
        return None
    return safe_str(data.get("id", "")) or None


def _build_update_document_payload(  # noqa: PLR0913
    *,
    project_id: str,
    space_id: str,
    document_name: str,
    title: str | None,
    status: str | None,
    type: str | None,
    home_page_content_html: str | None = None,
    auto_suspect: bool | None = None,
    uses_outline_numbering: bool | None = None,
    custom_fields: dict[str, object] | None = None,
) -> dict[str, JsonValue]:
    """JSON:API PATCH body for ``.../documents/{d}``; skip unset.
    ``home_page_content_html`` wrapped verbatim (empty guard in tool layer).
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
    if auto_suspect is not None:
        attributes["autoSuspect"] = auto_suspect
    if uses_outline_numbering is not None:
        attributes["usesOutlineNumbering"] = uses_outline_numbering
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
    auto_suspect: bool | None = None,
    uses_outline_numbering: bool | None = None,
    custom_fields: dict[str, object] | None = None,
) -> dict[str, JsonValue]:
    """JSON:API POST body for ``.../spaces/{s}/documents``; skip unset."""
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
    if auto_suspect is not None:
        attributes["autoSuspect"] = auto_suspect
    if uses_outline_numbering is not None:
        attributes["usesOutlineNumbering"] = uses_outline_numbering
    merge_custom_fields(attributes, custom_fields, STANDARD_DOCUMENT_ATTRIBUTES)

    item: dict[str, JsonValue] = {
        "type": "documents",
        "attributes": attributes,
    }
    return {"data": [item]}


def _build_copy_document_payload(
    *,
    target_document_name: str,
    target_project_id: str | None,
    target_space_id: str | None,
    link_original_items_with_role: str | None,
    remove_outgoing_links: bool | None,
) -> dict[str, JsonValue]:
    """Flat ``copy`` action body (not JSON:API); skip unset. Explicit
    ``remove_outgoing_links=False`` still sent (``is not None``).
    """
    payload: dict[str, JsonValue] = {"targetDocumentName": target_document_name}
    if target_project_id is not None:
        payload["targetProjectId"] = target_project_id
    if target_space_id is not None:
        payload["targetSpaceId"] = target_space_id
    if link_original_items_with_role is not None:
        payload["linkOriginalItemsWithRole"] = link_original_items_with_role
    if remove_outgoing_links is not None:
        payload["removeOutgoingLinks"] = remove_outgoing_links
    return payload


@mcp.tool(
    tags={"read"},
    timeout=60.0,
    annotations={"readOnlyHint": True},
)
async def list_documents(
    ctx: Context,
    project_id: str = Field(description="Polarion project ID."),
    page_size: int = Field(default=DEFAULT_PAGE_SIZE, ge=1, le=100),
    page_number: int = Field(default=1, ge=1),
) -> PaginatedResult[DocumentSummary]:
    """List a project's documents.

    Returns space_id + document_name — the inputs to every other document
    tool — plus type, status, updated, and creator/editor display names.
    Use get_document for author/editor ids. Discovery scan cached 60s.
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

    items = [
        DocumentSummary(
            space_id=doc.space_id,
            document_name=doc.document_name,
            type=doc.type,
            status=doc.status,
            updated=doc.updated,
            author_name=doc.author_name,
            last_updated_by_name=doc.updated_by_name,
        )
        for doc in page_slice
    ]

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
        description="Space ID ('_default' = default space).",
    ),
    document_name: str = Field(
        description="Document name within space_id.",
    ),
    include_homepage_content_html: bool = Field(
        default=False,
        description="Fill content_html with raw HTML for round-trip editing.",
    ),
) -> DocumentDetail:
    """Get a document's metadata: title/type/status/timestamps/editors/custom fields.

    include_homepage_content_html=True fills content_html with raw
    homePageContent HTML — the required source for
    update_document(home_page_content_html=...). That body is inline prose
    only — headings and embedded work items render via read_document.
    Never feed back a blanked (flag=False) body.
    """
    client = get_client(ctx)
    path = (
        f"/projects/{encode_path_segment(project_id)}"
        f"/spaces/{encode_path_segment(space_id)}"
        f"/documents/{encode_path_segment(document_name)}"
    )

    # ``@all`` token: explicit field list silently drop inline customs here.
    try:
        response = await client.get(
            path,
            params={
                "fields[documents]": DOCUMENT_DETAIL_FIELDS,
                "include": "author,updatedBy",
                "fields[users]": "name",
            },
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
    relationships = data.get("relationships", {})
    if not isinstance(relationships, dict):
        relationships = {}
    # ids = join keys into included users; surfaced short-form for requery too.
    user_names = parse_included_user_name_map(response)
    author_full = extract_relationship_id(relationships, "author")
    updated_by_full = extract_relationship_id(relationships, "updatedBy")

    content_html = ""
    if include_homepage_content_html:
        # Verbatim {type,value} so it round-trip through update_document.
        content_obj = attributes.get("homePageContent", {})
        if isinstance(content_obj, dict):
            content_html = safe_str(content_obj.get("value", ""))

    detail = DocumentDetail(
        title=safe_str(attributes.get("title", "")),
        type=safe_str(attributes.get("type", "")),
        status=safe_str(attributes.get("status", "")),
        created=safe_str(attributes.get("created", "")),
        updated=safe_str(attributes.get("updated", "")),
        author_id=extract_short_id(author_full),
        author_name=user_names.get(author_full, ""),
        last_updated_by_id=extract_short_id(updated_by_full),
        last_updated_by_name=user_names.get(updated_by_full, ""),
        content_html=content_html,
        auto_suspect=bool(attributes.get("autoSuspect", False)),
        uses_outline_numbering=bool(attributes.get("usesOutlineNumbering", False)),
        custom_fields=extract_custom_fields(attributes, STANDARD_DOCUMENT_ATTRIBUTES),
    )
    return detail


@mcp.tool(
    tags={"read"},
    timeout=60.0,
    annotations={"readOnlyHint": True},
)
async def read_document_parts(  # noqa: PLR0913
    ctx: Context,
    project_id: str = Field(description="Polarion project ID."),
    space_id: str = Field(description="Space ID ('_default' = default space)."),
    document_name: str = Field(description="Document name within space_id."),
    page_size: int = Field(default=DEFAULT_PAGE_SIZE, ge=1, le=100),
    page_number: int = Field(default=1, ge=1),
) -> PaginatedResult[DocumentPart]:
    """List a document's structural parts in order.

    Use for structure: part ids (move_work_item_to_document anchors),
    heading levels, per-part Markdown. For plain reading use read_document;
    for a document's work items use list_work_items.
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
                # Narrow workitem fields — ``@all`` ship unused inline customs.
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

    work_item_map = parse_included_work_item_map(response)

    data = response.get("data", [])
    items: list[DocumentPart] = []
    if isinstance(data, list):
        for item in data:
            part = _parse_document_part(item, work_item_map)
            if part is not None:
                items.append(part)

    return make_page(items, response, page_number, page_size)


@mcp.tool(
    tags={"read"},
    timeout=60.0,
    annotations={"readOnlyHint": True},
)
async def read_document(  # noqa: PLR0913
    ctx: Context,
    project_id: str = Field(description="Polarion project ID."),
    space_id: str = Field(description="Space ID ('_default' = default space)."),
    document_name: str = Field(description="Document name within space_id."),
    page_size: int = Field(default=DEFAULT_PAGE_SIZE, ge=1, le=100),
    page_number: int = Field(default=1, ge=1),
) -> DocumentReadResult:
    """Render a document end-to-end as flowing Markdown — THE way to read a body.

    Interleaves headings, work-item descriptions, and prose. Synthesis
    output: NEVER feed it to update_document — round-trip via
    get_document(include_homepage_content_html=True). For metadata-only
    extraction use list_work_items with SQL.
    """
    # read_document_parts handle fetch + error mapping (@mcp.tool return
    # original function).
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
    """Resolve the ``type`` axis the custom-field guard key on. Run on dry_run
    too, so preview raise same not-found / auth errors as real write.
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
    doc_type = safe_str(attrs.get("type", "")) if isinstance(attrs, dict) else ""
    if not doc_type:
        raise RuntimeError(
            f"Document '{space_id}/{document_name}' in project '{project_id}' has no "
            f"resolvable type; cannot validate custom_fields. Pass `type` explicitly."
        )
    return doc_type


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
        description="Space ID ('_default' = default space).",
    ),
    document_name: str = Field(
        min_length=1,
        description="Document name within space_id.",
    ),
    title: str | None = Field(default=None, description="New document title."),
    status: str | None = Field(
        default=None,
        description="New status; prefer workflow_action for real transitions.",
    ),
    type: str | None = Field(
        default=None, description="New document type (e.g. 'req_specification')."
    ),
    home_page_content_html: str | None = Field(
        default=None,
        max_length=MAX_BODY_HTML_LEN,
        description=(
            "New body as raw HTML from "
            "get_document(include_homepage_content_html=True); '' rejected; "
            "anchorless blocks get id= auto-stamped. New tables, captions, or "
            "other Polarion constructs: call get_html_recipes first and adapt "
            "a template."
        ),
    ),
    auto_suspect: bool | None = Field(
        default=None,
        description="Flag linked work items suspect on change.",
    ),
    uses_outline_numbering: bool | None = Field(
        default=None,
        description="Enable auto outline numbers (1, 1.1, ...).",
    ),
    custom_fields: dict[str, object] | None = Field(  # noqa: B008
        default=None,
        description="Partial; rich-text values as {'type':'text/html','value':...}.",
    ),
    workflow_action: str | None = Field(
        default=None,
        description="Workflow action ID.",
    ),
    dry_run: bool = Field(
        default=False,
        description="Preview payload without writing; guards still query Polarion.",
    ),
) -> DocumentUpdateResult:
    """Update a document's metadata or body.

    PATCHes only supplied attributes — omitted fields stay unchanged. Fetch
    via get_document BEFORE updating. home_page_content_html is raw Polarion
    HTML, sent verbatim — source from
    get_document(include_homepage_content_html=True). An empty string is
    rejected — pass '<p></p>' for near-empty.

    Body rules:

    - Inline <h1>..<h4> auto-create heading work items — THE way to add a
      heading. For body text or work items use create_work_items +
      move_work_item_to_document, NOT this tool.
    - A polarion_wiki macro name=module-workitem <div> leaves the work item's
      module unset — attach via move_work_item_to_document.
    - Polarion-specific constructs (tables, captions, links, TOC/TOF widgets,
      page breaks) must be adapted from get_html_recipes templates, never
      hand-written.

    workflow_action must pair with at least one attribute. Unknown
    status/type ids and custom_fields keys outside the type schema are
    rejected, values are not validated — resolve via
    list_document_enum_options first.
    """
    if home_page_content_html is not None and not home_page_content_html.strip():
        raise ValueError(
            "home_page_content_html is empty or whitespace-only; sending "
            "this would wipe the document body and orphan every heading "
            "work item. Pass at minimum '<p></p>' or omit the parameter "
            "to leave the body unchanged."
        )
    if home_page_content_html is not None:
        home_page_content_html = stamp_block_ids(home_page_content_html)
        if first_anchorless_block(home_page_content_html) is not None:
            raise RuntimeError(
                "stamp_block_ids left an anchorless block; refusing to PATCH "
                "(next read_document_parts would return HTTP 500)."
            )

    has_attrs = (
        title is not None
        or status is not None
        or type is not None
        or home_page_content_html is not None
        or auto_suspect is not None
        or uses_outline_numbering is not None
        or bool(custom_fields)
    )
    if not has_attrs and not workflow_action:
        raise ValueError(
            "update_document requires at least one of: title, status, "
            "type, home_page_content_html, auto_suspect, "
            "uses_outline_numbering, custom_fields, or workflow_action."
        )
    if not has_attrs and workflow_action:
        raise ValueError(
            "workflow_action alone is not supported -- Polarion rejects "
            "PATCH bodies with no attributes. Pair workflow_action with "
            "at least one of title, status, type, home_page_content_html, "
            "auto_suspect, uses_outline_numbering, or custom_fields."
        )

    client = get_client(ctx)
    # Type-agnostic enum guard: avoid extra GET, still catch ghost ids.
    await guard_document_enums(
        client,
        project_id,
        document_type="~",
        type=type,
        status=status,
    )

    # Build first: merge_custom_fields collision check cheaper than guard GET.
    payload = _build_update_document_payload(
        project_id=project_id,
        space_id=space_id,
        document_name=document_name,
        title=title,
        status=status,
        type=type,
        home_page_content_html=home_page_content_html,
        auto_suspect=auto_suspect,
        uses_outline_numbering=uses_outline_numbering,
        custom_fields=custom_fields,
    )
    # Type change key custom-field schema on new type, else current.
    if custom_fields:
        effective_type = type or await _resolve_document_type(
            client, project_id, space_id, document_name
        )
        await guard_document_custom_fields(
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
        # Additive: non-destructive, non-idempotent (duplicate module_name 409s).
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
        description="Space ID ('_default' = default space).",
    ),
    module_name: str = Field(
        min_length=1,
        description=(
            "Document identifier (e.g. 'MySpecV1'); unique within space_id, "
            "appears in the document URL."
        ),
    ),
    title: str = Field(
        min_length=1,
        description="Human-readable document title.",
    ),
    type: str = Field(
        min_length=1,
        description="Document type (e.g. 'req_specification', 'generic').",
    ),
    status: str | None = Field(
        default=None,
        description="Initial workflow status (project default if omitted).",
    ),
    home_page_content: str | None = Field(
        default=None,
        max_length=MAX_BODY_HTML_LEN,
        description="Markdown body; converted to sanitized HTML.",
    ),
    auto_suspect: bool | None = Field(
        default=None,
        description="Flag linked work items suspect on change.",
    ),
    uses_outline_numbering: bool | None = Field(
        default=None,
        description="Enable auto outline numbers (1, 1.1, ...).",
    ),
    custom_fields: dict[str, object] | None = Field(  # noqa: B008
        default=None,
        description=(
            "Keyed by Polarion field ID (copy keys from a sibling document); "
            "rich-text values as {'type':'text/html','value':...}."
        ),
    ),
    dry_run: bool = Field(
        default=False,
        description="Preview payload without writing; guards still query Polarion.",
    ),
) -> DocumentCreateResult:
    """Create a document in a space.

    module_name must be unique in the space — a duplicate name conflicts;
    check list_documents first. type/status and custom_fields keys are
    validated on write — resolve ids via list_document_enum_options first.

    home_page_content is Markdown (greenfield only), converted to sanitized
    HTML. Markdown tables get native Polarion styling; a paragraph starting
    'Table:' directly after a table becomes a numbered caption widget.
    Post-create edits round-trip raw HTML via
    get_document(include_homepage_content_html=True) and update_document;
    add work items via move_work_item_to_document.
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
        await guard_document_custom_fields(client, project_id, type, custom_fields)

    home_page_content_html = (
        stamp_block_ids(
            polarionify_html(sanitize_html(markdown_to_html(home_page_content)))
        )
        if home_page_content
        else ""
    )
    if home_page_content_html and first_anchorless_block(home_page_content_html):
        raise RuntimeError(
            "stamp_block_ids left an anchorless block in new document body."
        )

    payload = _build_create_document_payload(
        module_name=module_name,
        title=title,
        type=type,
        home_page_content_html=home_page_content_html,
        status=status,
        auto_suspect=auto_suspect,
        uses_outline_numbering=uses_outline_numbering,
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

    # Drop stale list_documents cache so new doc show on next call.
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
        # Additive: non-destructive, non-idempotent (duplicate target name 409s).
        "readOnlyHint": False,
        "destructiveHint": False,
        "idempotentHint": False,
        "openWorldHint": True,
    },
)
async def copy_document(  # noqa: PLR0913
    ctx: Context,
    project_id: str = Field(min_length=1, description="Source project ID."),
    space_id: str = Field(
        min_length=1,
        description="Source space ID ('_default' = default space).",
    ),
    document_name: str = Field(min_length=1, description="Source document name."),
    target_document_name: str = Field(
        min_length=1,
        description="New document name; must not already exist at the destination.",
    ),
    target_project_id: str | None = Field(
        default=None,
        description="Destination project (source project if omitted).",
    ),
    target_space_id: str | None = Field(
        default=None,
        description="Destination space (source space if omitted).",
    ),
    link_original_items_with_role: str | None = Field(
        default=None,
        description=(
            "Link each copied item back to its original with this "
            "workitem-link-role id (e.g. 'duplicates')."
        ),
    ),
    remove_outgoing_links: bool | None = Field(
        default=None,
        description="Strip outgoing links from copied items (kept if omitted).",
    ),
    revision: str | None = Field(
        default=None,
        description="Copy the source as of this revision (HEAD if omitted).",
    ),
    dry_run: bool = Field(
        default=False,
        description="Preview payload without writing; guards still query Polarion.",
    ),
) -> DocumentCopyResult:
    """Copy a document, duplicating its structure, body, and contained work items.

    Rebuilding via create_document/update_document loses the contained
    items. target_document_name must be free at the destination — check
    list_documents first. Destination defaults to the source project/space.

    link_original_items_with_role is validated against the TARGET project's
    workitem-link-role enum; remove_outgoing_links strips links carried over
    from the source.
    """
    client = get_client(ctx)
    if link_original_items_with_role is not None:
        await guard_work_item_link_roles(
            client,
            target_project_id or project_id,
            [link_original_items_with_role],
        )

    payload = _build_copy_document_payload(
        target_document_name=target_document_name,
        target_project_id=target_project_id,
        target_space_id=target_space_id,
        link_original_items_with_role=link_original_items_with_role,
        remove_outgoing_links=remove_outgoing_links,
    )

    if dry_run:
        # revision = query param, not body — fold into preview so dry_run reveal it.
        preview = {**payload, "revision": revision} if revision else payload
        return DocumentCopyResult(
            copied=False,
            dry_run=True,
            target_project_id=None,
            target_space_id=None,
            document_name=None,
            payload_preview=preview,
        )

    path = (
        f"/projects/{encode_path_segment(project_id)}"
        f"/spaces/{encode_path_segment(space_id)}"
        f"/documents/{encode_path_segment(document_name)}/actions/copy"
    )
    if revision:
        path = f"{path}?{urlencode({'revision': revision})}"
    try:
        response = await client.post(path, json=cast(dict[str, object], payload))
    except PolarionAuthError as exc:
        raise PermissionError(
            "Cannot copy document -- check your POLARION_TOKEN permissions."
        ) from exc
    except PolarionNotFoundError as exc:
        raise ValueError(
            f"Source document '{document_name}' (space '{space_id}', project "
            f"'{project_id}') not found. Use `list_documents` to discover valid IDs."
        ) from exc
    except PolarionError as exc:
        raise RuntimeError(
            f"Failed to copy document to '{target_document_name}': {exc.message}. "
            "If that name already exists at the destination, choose a different "
            "target_document_name (check list_documents)."
        ) from exc

    full_id = _extract_copied_module_id(response)
    if full_id is None:
        raise RuntimeError(
            "Polarion accepted the copy request but returned no document id. "
            "The copy may or may not exist; verify with list_documents."
        )
    new_project = full_id.split("/", 2)[0]
    new_space, new_name = split_module_id(full_id)

    # Drop stale list cache only where copy lands.
    invalidate_documents_cache(target_project_id or project_id)

    return DocumentCopyResult(
        copied=True,
        dry_run=False,
        target_project_id=new_project or None,
        target_space_id=new_space or None,
        document_name=new_name or None,
        payload_preview=None,
    )
