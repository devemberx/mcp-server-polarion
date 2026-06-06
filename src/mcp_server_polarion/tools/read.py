"""Read-only MCP tools for querying Polarion ALM.

Body fields use one of two formats depending on the tool's purpose:

* **Round-trip paths** (``get_work_item``, ``get_document``) return raw
  Polarion HTML so the value round-trips through the matching ``update_*``
  tool without lossy Markdown conversion.
* **Synthesis paths** (``read_document``, ``read_document_parts``,
  ``read_work_item``) convert HTML to Markdown for LLM consumption; the
  output is read-only and cannot be fed back to write tools.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from importlib import resources
from typing import Final, Literal, cast

from fastmcp import Context
from pydantic import Field

from mcp_server_polarion.core.client import PolarionClient
from mcp_server_polarion.core.exceptions import (
    PolarionAuthError,
    PolarionError,
    PolarionNotFoundError,
)
from mcp_server_polarion.models import (
    DocumentComment,
    DocumentDetail,
    DocumentPart,
    DocumentReadResult,
    DocumentSummary,
    EnumOption,
    PaginatedResult,
    ProjectSummary,
    SqlRecipeGallery,
    WorkItemDetail,
    WorkItemLink,
    WorkItemRead,
    WorkItemSummary,
)
from mcp_server_polarion.server import mcp
from mcp_server_polarion.tools._cache import (
    get_cached_documents,
    record_document_custom_field_keys,
    record_work_item_custom_field_keys,
    store_cached_documents,
)
from mcp_server_polarion.tools._helpers import (
    DEFAULT_PAGE_SIZE,
    DOCUMENT_COMMENT_LIST_FIELDS,
    DOCUMENT_DETAIL_FIELDS,
    STANDARD_DOCUMENT_ATTRIBUTES,
    WORK_ITEM_DETAIL_FIELDS,
    WORK_ITEM_LIST_FIELDS,
    WORK_ITEM_PART_FIELDS,
    build_document_comment,
    build_included_work_item_map,
    compute_has_more,
    encode_path_segment,
    extract_custom_fields,
    extract_relationship_id,
    extract_short_id,
    extract_total_count,
    get_client,
    parse_work_item_detail,
    parse_work_item_summaries,
    safe_str,
    split_module_id,
    summary_to_back_link,
    validate_work_item_id_for_lucene,
)
from mcp_server_polarion.utils import html_to_markdown

# Polarion's ``attributes.type`` only emits the first 5; ``tof`` and
# ``page_break`` are recovered from the part ID prefix in ``_parse_document_part``.
type _PartType = Literal[
    "heading", "workitem", "normal", "toc", "wikiblock", "tof", "page_break"
]
_POLARION_PART_TYPES: Final[frozenset[str]] = frozenset(
    {"heading", "workitem", "normal", "toc", "wikiblock"},
)

# Defensive clamp for heading levels emitted by ``read_document``.
# Polarion exposes 1-4, but ``_resolve_heading_level`` falls back to
# regex-parsing ``<hN>`` which could in principle hit 5-6.
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


# Constant Markdown chunks for content-less widget part types.
_CONSTANT_CHUNKS: Final[dict[str, str]] = {
    "toc": "*[Table of Contents (Polarion widget)]*",
    "tof": "*[Table of Figures (Polarion widget)]*",
    "page_break": "---",
}
_WIKIBLOCK_MACRO_RE: Final[re.Pattern[str]] = re.compile(
    r"#([A-Za-z_][A-Za-z0-9_]*)\s*\("
)
# Minimum line count for a fenced wikiblock: opener + closer.
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


async def _discover_documents(
    client: PolarionClient,
    project_id: str,
) -> list[tuple[str, str]]:
    """Discover all unique (space_id, document_name) pairs via linear scan.

    Iterates every heading-workitem page (page_size=100) and accumulates
    unique ``module`` relationship IDs. Results are TTL-cached in
    ``tools._helpers`` so paginated callers reuse the discovery.

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


@mcp.tool(
    tags={"read"},
    timeout=60.0,
    annotations={"readOnlyHint": True},
)
async def list_projects(
    ctx: Context,
    query: str | None = Field(
        default=None,
        description=(
            "Optional Lucene filter (e.g. 'name:ILCU*'); trailing wildcards only."
        ),
    ),
    page_size: int = Field(default=DEFAULT_PAGE_SIZE, ge=1, le=100),
    page_number: int = Field(default=1, ge=1),
) -> PaginatedResult[ProjectSummary]:
    """List Polarion projects the authenticated user can access.

    Use this to discover project IDs for other tools. Lucene allows trailing
    wildcards (``name:ILCU*``) but rejects leading ones (``*foo*``, HTTP 400).

    Args:
        ctx: MCP tool context (injected automatically).
        query: Optional Lucene filter; omit to return all accessible projects.
        page_size: Items per page (1-100, default 100).
        page_number: 1-based page number (default 1).

    Returns:
        PaginatedResult of ``ProjectSummary`` items with ``id``, ``name``,
        and ``active`` (False = archived; defaults to True if absent).

    Raises:
        PermissionError: Auth token invalid or lacks permission.
        RuntimeError: Other Polarion API errors.
    """
    client = get_client(ctx)
    params: dict[str, str | int] = {
        "fields[projects]": "id,name,active",
        "page[size]": page_size,
        "page[number]": page_number,
    }
    if query is not None:
        params["query"] = query
    try:
        response = await client.get("/projects", params=params)
    except PolarionAuthError as exc:
        raise PermissionError(
            "Cannot list projects -- check your POLARION_TOKEN permissions."
        ) from exc
    except PolarionError as exc:
        raise RuntimeError(f"Failed to list projects: {exc.message}") from exc

    data = response.get("data", [])
    items: list[ProjectSummary] = []
    if isinstance(data, list):
        for item in data:
            if not isinstance(item, dict):
                continue
            attributes = item.get("attributes", {})
            if not isinstance(attributes, dict):
                attributes = {}
            active_attr = attributes.get("active")
            active = active_attr if isinstance(active_attr, bool) else True
            items.append(
                ProjectSummary(
                    id=safe_str(item.get("id", "")),
                    name=safe_str(attributes.get("name", "")),
                    active=active,
                )
            )

    raw_total = extract_total_count(response)
    total = raw_total
    if total <= 0 and items:
        total = (page_number - 1) * page_size + len(items)

    return PaginatedResult[ProjectSummary](
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


def _build_enum_option(entry: dict[str, object]) -> EnumOption:
    def _bool(key: str) -> bool:
        value = entry.get(key)
        return value if isinstance(value, bool) else False

    return EnumOption(
        id=safe_str(entry.get("id", "")),
        name=safe_str(entry.get("name", "")),
        description=safe_str(entry.get("description", "")),
        default=_bool("default"),
        hidden=_bool("hidden"),
        terminal=_bool("terminal"),
    )


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
                items.append(_build_enum_option(entry))

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
async def list_work_item_enum_options(  # noqa: PLR0913
    ctx: Context,
    project_id: str = Field(description="Polarion project ID."),
    field_id: str = Field(
        description=(
            "Field id (e.g. 'status', 'type', 'severity', 'priority',"
            " or a custom field id)."
        ),
    ),
    work_item_type: str = Field(
        description=(
            "Work item type id (e.g. 'task', 'requirement')."
            " Pass '~' for type-agnostic options."
        ),
    ),
    page_size: int = Field(default=DEFAULT_PAGE_SIZE, ge=1, le=100),
    page_number: int = Field(default=1, ge=1),
) -> PaginatedResult[EnumOption]:
    """List valid enum options for a work item field of the given type.

    Call before ``create_work_items`` / ``update_work_item`` to resolve a
    ``type`` / ``status`` / ``severity`` / ``priority`` / custom-enum value.
    Polarion does NOT validate these on write (unknown ids persist as ghosts;
    ``priority`` only coerces non-numeric input to the project default), so
    resolve first. Work-item fields only — use ``list_document_enum_options``
    for documents. Returns the FULL set (not filtered by current workflow
    state); ``work_item_type='~'`` is the type-agnostic set, and an unknown
    type is silently treated as ``~``, so verify the type id (e.g. via
    ``get_work_item``) first.

    Args:
        ctx: MCP tool context (injected automatically).
        project_id: Polarion project ID.
        field_id: Field id whose options to list.
        work_item_type: Work item type id, or '~' for type-agnostic.
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
            ``work_item_type`` does NOT raise; Polarion silently
            falls back to the ``~`` set.
        PermissionError: Token lacks permission.
        RuntimeError: Other Polarion API errors.
    """
    client = get_client(ctx)
    path = (
        f"/projects/{encode_path_segment(project_id)}"
        f"/workitems/fields/{encode_path_segment(field_id)}"
        "/actions/getAvailableOptions"
    )
    params: dict[str, str | int] = {
        "type": work_item_type,
        "page[size]": page_size,
        "page[number]": page_number,
    }
    try:
        response = await client.get(path, params=params)
    except PolarionNotFoundError as exc:
        raise ValueError(
            f"No enum options for field '{field_id}' on work item type "
            f"'{work_item_type}' in project '{project_id}'."
        ) from exc
    except PolarionAuthError as exc:
        raise PermissionError(
            "Cannot list work item enum options"
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
                items.append(_build_enum_option(entry))

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


_SQL_QUERY_RECIPES: Final[str] = (
    resources.files("mcp_server_polarion.tools")
    .joinpath("guides", "sql_query_recipes.md")
    .read_text(encoding="utf-8")
)


@mcp.tool(
    tags={"read"},
    annotations={"readOnlyHint": True},
)
async def get_sql_query_recipes() -> SqlRecipeGallery:
    """Fetch copy-paste SQL recipes for the ``list_work_items`` ``SQL:(...)`` prefix.

    Call this before writing a module-scoped, custom-field, or traceability
    SQL query, then adapt a recipe instead of hand-writing joins from memory.
    Returns the table schema plus parameterised recipes as one Markdown
    document; loaded on demand so it never occupies always-on context.
    """
    return SqlRecipeGallery(recipes=_SQL_QUERY_RECIPES)


@mcp.tool(
    tags={"read"},
    timeout=60.0,
    annotations={"readOnlyHint": True},
)
async def list_work_items(
    ctx: Context,
    project_id: str = Field(description="Polarion project ID."),
    query: str | None = Field(
        default=None,
        description=(
            "Optional Lucene filter (e.g. 'type:requirement', 'title:SRS*') "
            "OR a 'SQL:(...)' prefix for native SQL."
        ),
    ),
    page_size: int = Field(default=DEFAULT_PAGE_SIZE, ge=1, le=100),
    page_number: int = Field(default=1, ge=1),
) -> PaginatedResult[WorkItemSummary]:
    """List and search work items in a Polarion project.

    Pass a Lucene ``query`` (`type:requirement`, `status:approved AND
    type:requirement`, `title:SRS*`) or omit it for all work items. Leading
    wildcards (`*foo*`) return HTTP 400. ``module`` and description body text
    are NOT indexed — use the *SQL prefix* below for module scope, and
    ``read_document_parts`` / ``read_document`` for body content.

    **SQL prefix.** A ``query`` starting with ``SQL:(`` runs as native SQL,
    unlocking patterns Lucene cannot express (module-scoped lookup,
    leading-wildcard ``LIKE``, custom-field joins, role-preserving
    traceability). Escape ``'`` as ``''``; there are no bind parameters, so
    escape any user-supplied value before substituting. ``C_DESCRIPTION LIKE``
    does NOT match (CLOB stored elsewhere — use ``read_document_parts`` for
    body search), and ``LIKE`` is rejected inside ``EXISTS (SELECT ...)`` —
    keep it in the top-level ``WHERE`` via ``INNER JOIN``. For module-scoped,
    custom-field, or traceability queries you MUST call ``get_sql_query_recipes``
    and adapt a recipe before writing SQL; do not hand-write these joins from
    memory.

    Args:
        ctx: MCP tool context (injected automatically).
        project_id: Polarion project ID.
        query: Optional Lucene filter, OR a ``SQL:(...)`` prefix for native SQL.
        page_size: Items per page (1-100, default 100).
        page_number: 1-based page number (default 1).

    Returns:
        PaginatedResult of ``WorkItemSummary`` items with ``id``,
        ``title``, ``type``, ``status``, ``priority``, ``updated``,
        ``space_id``, ``document_name``, and ``assignee_ids``.

    Raises:
        ValueError: Project not found.
        PermissionError: Token lacks permission.
        RuntimeError: Other Polarion API errors (incl. bad Lucene or SQL syntax).
    """
    client = get_client(ctx)
    params: dict[str, str | int] = {
        "fields[workitems]": WORK_ITEM_LIST_FIELDS,
        # To-many ``assignee.data`` is only inlined when explicitly included.
        "include": "assignee",
        "page[size]": page_size,
        "page[number]": page_number,
    }
    if query is not None:
        params["query"] = query
    try:
        response = await client.get(
            f"/projects/{encode_path_segment(project_id)}/workitems",
            params=params,
        )
    except PolarionNotFoundError as exc:
        raise ValueError(
            f"Project '{project_id}' not found. "
            "Use `list_projects` to discover valid project IDs."
        ) from exc
    except PolarionAuthError as exc:
        raise PermissionError(
            "Cannot list work items -- check your POLARION_TOKEN permissions."
        ) from exc
    except PolarionError as exc:
        raise RuntimeError(f"Failed to list work items: {exc.message}") from exc

    data = response.get("data", [])
    items = parse_work_item_summaries(data)

    # Fall back to the seen-item count only when the API total is missing/zero
    # and the page is non-empty.
    raw_wi_total = extract_total_count(response)
    work_item_total = raw_wi_total
    if work_item_total == 0 and items:
        work_item_total = (page_number - 1) * page_size + len(items)

    return PaginatedResult[WorkItemSummary](
        items=items,
        total_count=work_item_total,
        page=page_number,
        page_size=page_size,
        has_more=compute_has_more(
            response, raw_wi_total, page_number, page_size, len(items)
        ),
    )


@mcp.tool(
    tags={"read"},
    timeout=60.0,
    annotations={"readOnlyHint": True},
)
async def get_work_item(
    ctx: Context,
    project_id: str = Field(description="Polarion project ID."),
    work_item_id: str = Field(description="Work Item ID (e.g. 'MCPT-001')."),
    include_description_html: bool = Field(
        default=False,
        description=(
            "When True, fill ``description_html`` with raw HTML for round-trip editing."
        ),
    ),
) -> WorkItemDetail:
    """Get full details of a single Polarion work item.

    With ``include_description_html=True``, ``description_html`` carries the raw
    Polarion HTML body — the round-trip shape for
    ``update_work_item(description_html=...)``. Only feed it back when the flag
    was True (False blanks it, and the update tool treats ``""`` as
    ``leave unchanged``).

    Args:
        ctx: MCP tool context (injected automatically).
        project_id: Polarion project ID.
        work_item_id: Work Item ID (e.g. 'MCPT-001').
        include_description_html: When True, populate
            ``description_html`` with the raw HTML body. Default False.

    Returns:
        WorkItemDetail with:
        - ``id``: Work Item ID.
        - ``title``: Work Item title.
        - ``type``: Work Item type.
        - ``status``: Workflow status.
        - ``priority``: Priority value as a string (empty when unset).
        - ``updated``: ISO-8601 last-modified timestamp.
        - ``created``: ISO-8601 creation timestamp.
        - ``space_id`` / ``document_name``: Document this work item
          belongs to (both empty when not module-bound).
        - ``outline_number``: Hierarchical position inside the document
          (e.g. '1.2.3'); empty when not in a document.
        - ``assignee_ids``: Short user IDs of assignees.
        - ``author_id``: Short user ID of the author.
        - ``resolution``: Resolution outcome for closed items
          (e.g. 'fixed', 'wontfix'); empty otherwise.
        - ``severity``: Severity classification, used for defects
          (e.g. 'blocker', 'critical'); empty otherwise.
        - ``hyperlinks``: External hyperlinks as ``Hyperlink`` items
          with ``role``, ``title``, ``uri`` fields.
        - ``description_html``: Raw Polarion HTML body — populated only
          when ``include_description_html=True``, otherwise empty.
          Pass this string verbatim back to
          ``update_work_item(description_html=...)`` for a lossless
          round-trip.
        - ``project_id``: Containing project.
        - ``custom_fields``: User-defined custom fields as a
          ``{fieldId: value}`` dict. Keys vary per project and work-item
          type; values are returned verbatim (primitives or
          ``{type: 'text/html', value: '<...>'}`` for rich-text fields).
          Empty dict when no custom fields are populated.

    Raises:
        ValueError: If the work item or project is not found.
        PermissionError: If the token lacks permissions.
        RuntimeError: On unexpected Polarion API errors.
    """
    client = get_client(ctx)
    path = (
        f"/projects/{encode_path_segment(project_id)}"
        f"/workitems/{encode_path_segment(work_item_id)}"
    )
    try:
        response = await client.get(
            path,
            params={
                "fields[workitems]": WORK_ITEM_DETAIL_FIELDS,
                "include": "assignee",
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
            "Cannot access work item -- check your POLARION_TOKEN permissions."
        ) from exc
    except PolarionError as exc:
        raise RuntimeError(
            f"Failed to get work item '{work_item_id}': {exc.message}"
        ) from exc

    data = response.get("data", {})
    if not isinstance(data, dict):
        data = {}

    detail = parse_work_item_detail(
        data,
        project_id=project_id,
        fallback_id=work_item_id,
    )
    # Prime the guard cache so update_work_item.custom_fields can validate keys.
    if detail.type:
        record_work_item_custom_field_keys(
            project_id, detail.type, detail.custom_fields.keys()
        )
    if not include_description_html:
        # The body always travels over the wire (no sparse-fieldset excludes it);
        # blank it here to honour the include_description_html=False contract.
        detail = detail.model_copy(update={"description_html": ""})
    return detail


@mcp.tool(
    tags={"read"},
    timeout=60.0,
    annotations={"readOnlyHint": True},
)
async def read_work_item(
    ctx: Context,
    project_id: str = Field(description="Polarion project ID."),
    work_item_id: str = Field(description="Work Item ID (e.g. 'MCPT-001')."),
) -> WorkItemRead:
    """Read a Polarion work item with its body rendered as Markdown.

    Synthesis variant of ``get_work_item``: same metadata fields plus
    ``description`` as Markdown (converted from HTML) instead of
    ``description_html`` — use when an LLM needs to read or summarise the body.
    The converter collapses Polarion-specific spans and ID anchors, so the
    Markdown is read-only — do NOT feed it back to ``update_work_item``. For
    round-trip editing, pair ``get_work_item(include_description_html=True)``
    with ``update_work_item(description_html=...)``.

    Args:
        ctx: MCP tool context (injected automatically).
        project_id: Polarion project ID.
        work_item_id: Work Item ID (e.g. 'MCPT-001').

    Returns:
        WorkItemRead with all the metadata fields of ``WorkItemDetail``
        (``id``, ``title``, ``type``, ``status``, ``priority``,
        ``updated``, ``created``, ``space_id`` / ``document_name``,
        ``outline_number``, ``assignee_ids``, ``author_id``,
        ``resolution``, ``severity``, ``hyperlinks``, ``project_id``,
        ``custom_fields``) plus ``description`` carrying the Markdown
        body (empty when the work item has no description).

    Raises:
        ValueError: If the work item or project is not found.
        PermissionError: If the token lacks permissions.
        RuntimeError: On unexpected Polarion API errors.
    """
    # Delegate fetch + error mapping to get_work_item; pull raw HTML so the
    # Markdown converter runs without a second round trip.
    detail = await get_work_item(
        ctx,
        project_id=project_id,
        work_item_id=work_item_id,
        include_description_html=True,
    )
    description = (
        html_to_markdown(detail.description_html) if detail.description_html else ""
    )
    return WorkItemRead(
        **detail.model_dump(exclude={"description_html"}),
        description=description,
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
async def list_document_comments(  # noqa: PLR0913
    ctx: Context,
    project_id: str = Field(description="Polarion project ID."),
    space_id: str = Field(
        description="Space ID containing the document (e.g. '_default')."
    ),
    document_name: str = Field(description="Document name within the space."),
    page_size: int = Field(default=DEFAULT_PAGE_SIZE, ge=1, le=100),
    page_number: int = Field(default=1, ge=1),
) -> PaginatedResult[DocumentComment]:
    """List comments attached to a Polarion document.

    Comments come back as a flat page; reconstruct threads client-side via
    ``parent_comment_id`` (set on replies) and ``child_comment_ids`` (top-level
    comments have ``parent_comment_id`` of ``None``). ``text`` is verbatim, with
    ``text_format`` ``'text/html'`` or ``'text/plain'``; HTML is NOT sanitized
    (round-trips losslessly) — treat as untrusted input if rendering.

    Args:
        ctx: MCP tool context (injected automatically).
        project_id: Polarion project ID.
        space_id: Space ID (use '_default' for the default space).
        document_name: Document name within the space.
        page_size: Items per page (1-100, default 100).
        page_number: 1-based page number (default 1).

    Returns:
        PaginatedResult of ``DocumentComment`` items with ``id``,
        ``created``, ``resolved``, ``text``, ``text_format``, ``author_id``,
        ``parent_comment_id``, and ``child_comment_ids``.

    Raises:
        ValueError: Project, space, or document not found.
        PermissionError: Token lacks permission.
        RuntimeError: Other Polarion API errors.
    """
    client = get_client(ctx)
    path = (
        f"/projects/{encode_path_segment(project_id)}"
        f"/spaces/{encode_path_segment(space_id)}"
        f"/documents/{encode_path_segment(document_name)}"
        "/comments"
    )
    try:
        response = await client.get(
            path,
            params={
                "fields[document_comments]": DOCUMENT_COMMENT_LIST_FIELDS,
                # To-many ``childComments.data`` is only inlined when included.
                "include": "childComments",
                "page[size]": page_size,
                "page[number]": page_number,
            },
        )
    except PolarionNotFoundError as exc:
        raise ValueError(
            f"Document '{space_id}/{document_name}' not found in project "
            f"'{project_id}'. Use `list_documents` to discover valid IDs."
        ) from exc
    except PolarionAuthError as exc:
        raise PermissionError(
            "Cannot access document comments -- check your POLARION_TOKEN permissions."
        ) from exc
    except PolarionError as exc:
        raise RuntimeError(
            f"Failed to list comments for '{space_id}/{document_name}': {exc.message}"
        ) from exc

    raw_data = response.get("data", []) if isinstance(response, dict) else []
    comment_items: list[DocumentComment] = []
    if isinstance(raw_data, list):
        for entry in raw_data:
            if isinstance(entry, dict):
                comment_items.append(build_document_comment(entry))

    raw_total = extract_total_count(response)
    total = raw_total
    if total <= 0 and comment_items:
        total = (page_number - 1) * page_size + len(comment_items)

    return PaginatedResult[DocumentComment](
        items=comment_items,
        total_count=total,
        page=page_number,
        page_size=page_size,
        has_more=compute_has_more(
            response, raw_total, page_number, page_size, len(comment_items)
        ),
    )
