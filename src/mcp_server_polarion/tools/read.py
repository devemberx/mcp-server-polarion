"""Read-only MCP tools for querying Polarion ALM.

Nine tools that retrieve projects, documents, work items, and their
relationships.  Every tool returns Pydantic models -- never raw ``dict``.

Body fields use two different formats depending on the tool's purpose:

* **Round-trip paths** -- ``get_work_item`` and ``get_document`` return
  raw Polarion HTML (``description_html``, ``content_html``). Same shape
  round-trips back through the matching ``update_*`` tool without lossy
  Markdown conversion.
* **Synthesis paths** -- ``read_document`` and ``read_document_parts``
  convert HTML to Markdown via ``html_to_markdown()`` for LLM
  consumption. Output from these tools is read-only and cannot be fed
  back to write tools.
"""

from __future__ import annotations

import re
import time
from dataclasses import dataclass
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
    DocumentDetail,
    DocumentPart,
    DocumentReadResult,
    DocumentSummary,
    EnumOption,
    LinkedWorkItemSummary,
    PaginatedResult,
    ProjectSummary,
    WorkItemDetail,
    WorkItemRead,
    WorkItemSummary,
)
from mcp_server_polarion.server import mcp
from mcp_server_polarion.tools._helpers import (
    DEFAULT_PAGE_SIZE,
    DOC_DETAIL_FIELDS,
    STANDARD_DOCUMENT_ATTRS,
    WI_DETAIL_FIELDS,
    WI_LIST_FIELDS,
    WI_PART_FIELDS,
    build_included_workitem_map,
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
    summary_to_back_linked,
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


def _resolve_heading_level(attrs: dict[str, object]) -> int:
    """Return the heading level for a heading part.

    Prefers ``attributes.level`` when present, otherwise falls back to
    parsing the leading ``<hN>`` tag in ``attributes.content``.
    """
    attr_level = attrs.get("level")
    if isinstance(attr_level, int):
        return attr_level
    head_html = _extract_html_value(attrs.get("content"))
    match = re.match(r"<h(\d)", head_html, re.IGNORECASE)
    return int(match.group(1)) if match else 0


def _resolve_linked_work_item(
    rels: dict[str, object],
    wi_map: dict[str, dict[str, object]],
) -> _LinkedWorkItem:
    """Return metadata for the work item linked from a document part."""
    wi_full_id = extract_relationship_id(rels, "workItem")
    if not wi_full_id:
        return _LinkedWorkItem()

    short_id = (
        wi_full_id.split("/", maxsplit=1)[-1] if "/" in wi_full_id else wi_full_id
    )
    wi_attrs = wi_map.get(wi_full_id, {}).get("attributes", {})
    if not isinstance(wi_attrs, dict):
        return _LinkedWorkItem(short_id=short_id)

    return _LinkedWorkItem(
        short_id=short_id,
        title=safe_str(wi_attrs.get("title", "")),
        type=safe_str(wi_attrs.get("type", "")),
        status=safe_str(wi_attrs.get("status", "")),
        description_html=_extract_html_value(wi_attrs.get("description")),
        outline_number=safe_str(wi_attrs.get("outlineNumber", "")),
    )


def _parse_document_part(
    item: object,
    wi_map: dict[str, dict[str, object]],
) -> DocumentPart | None:
    """Parse a single JSON:API document-part resource into a model.

    Args:
        item: A single resource object from the ``data`` array.
        wi_map: Included work-item lookup built by
            ``build_included_workitem_map``.

    Returns:
        A ``DocumentPart`` instance, or ``None`` if *item* is invalid.
    """
    if not isinstance(item, dict):
        return None
    attrs = item.get("attributes", {})
    if not isinstance(attrs, dict):
        attrs = {}

    full_id = safe_str(item.get("id", ""))
    short_id = full_id.rsplit("/", maxsplit=1)[-1] if "/" in full_id else full_id

    raw_type = safe_str(attrs.get("type", ""))
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

    # Polarion stores heading text in work-item titles and workitem body in
    # the work-item description, so attributes.content for those types is
    # either an empty <hN></hN> stub or empty entirely. Skip the conversion
    # to avoid emitting noise like "#"/"##" or empty strings to the LLM.
    content_html = (
        _extract_html_value(attrs.get("content"))
        if part_type not in {"heading", "workitem"}
        else ""
    )
    level = _resolve_heading_level(attrs) if part_type == "heading" else 0

    rels = item.get("relationships", {})
    if not isinstance(rels, dict):
        rels = {}
    linked = _resolve_linked_work_item(rels, wi_map)

    next_full_id = extract_relationship_id(rels, "nextPart")
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
        external=bool(attrs.get("external", False)),
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


def _parse_linked_items(
    response: dict[str, object],
    *,
    direction: Literal["forward", "back"],
) -> list[LinkedWorkItemSummary]:
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
        List of parsed ``LinkedWorkItemSummary`` instances.
    """
    wi_map = build_included_workitem_map(response)

    items: list[LinkedWorkItemSummary] = []
    data = response.get("data", [])
    if not isinstance(data, list):
        return items

    for item in data:
        if not isinstance(item, dict):
            continue
        attrs = item.get("attributes", {})
        if not isinstance(attrs, dict):
            attrs = {}

        role = safe_str(attrs.get("role", ""))
        suspect = bool(attrs.get("suspect", False))

        # Resolve target WI via relationships.
        rels = item.get("relationships", {})
        if not isinstance(rels, dict):
            rels = {}
        wi_full_id = extract_relationship_id(rels, "workItem")
        wi_id = extract_short_id(wi_full_id)

        # Resolve the target via relationships only — raw ID parsing
        # is intentionally avoided.
        if not wi_id:
            continue

        # Resolve title and metadata from the included target work item.
        title = ""
        wi_type = ""
        wi_status = ""
        space_id = ""
        document_name = ""
        wi = wi_map.get(wi_full_id, {})
        wi_attrs = wi.get("attributes", {})
        if isinstance(wi_attrs, dict):
            title = safe_str(wi_attrs.get("title", ""))
            wi_type = safe_str(wi_attrs.get("type", ""))
            wi_status = safe_str(wi_attrs.get("status", ""))
        wi_rels = wi.get("relationships", {})
        if isinstance(wi_rels, dict):
            space_id, document_name = split_module_id(
                extract_relationship_id(wi_rels, "module")
            )

        items.append(
            LinkedWorkItemSummary(
                id=wi_id,
                title=title,
                role=role,
                direction=direction,
                suspect=suspect,
                type=wi_type,
                status=wi_status,
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
        documents: Mutable set to collect unique (space, doc) pairs into.
    """
    mod_id = _get_module_id(item)
    if mod_id:
        parts = mod_id.split("/")
        # Format: "projectId/spaceId/docName" → parts[1], parts[2:]
        if len(parts) >= 3:  # noqa: PLR2004
            space_id = parts[1]
            doc_name = "/".join(parts[2:])
            documents.add((space_id, doc_name))


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
    rels = item.get("relationships", {})
    if not isinstance(rels, dict):
        return ""
    return extract_relationship_id(rels, "module")


# Document-discovery TTL cache (in-process, keyed by project_id).
# A short TTL keeps paginated `list_documents` calls cheap while ensuring
# newly-created documents appear within ~1 minute. Future write tools can
# invalidate by popping the project_id key.
_CACHE_TTL_SECONDS: Final[float] = 60.0


@dataclass(frozen=True, slots=True)
class _DocCacheEntry:
    expires_at: float
    documents: tuple[tuple[str, str], ...]


_documents_cache: dict[str, _DocCacheEntry] = {}


def _get_cached_documents(project_id: str) -> list[tuple[str, str]] | None:
    entry = _documents_cache.get(project_id)
    if entry is None:
        return None
    if time.monotonic() >= entry.expires_at:
        _documents_cache.pop(project_id, None)
        return None
    return list(entry.documents)


def _store_cached_documents(
    project_id: str,
    documents: list[tuple[str, str]],
) -> None:
    _documents_cache[project_id] = _DocCacheEntry(
        expires_at=time.monotonic() + _CACHE_TTL_SECONDS,
        documents=tuple(documents),
    )


async def _discover_documents(
    client: PolarionClient,
    project_id: str,
) -> list[tuple[str, str]]:
    """Discover all unique (space_id, document_name) pairs via linear scan.

    Iterates every heading-workitem page (page_size=100) and accumulates
    unique ``module`` relationship IDs. Results are cached for
    ``_CACHE_TTL_SECONDS`` so paginated callers reuse the discovery.

    Args:
        client: Active ``PolarionClient`` instance.
        project_id: Polarion project ID.

    Returns:
        Sorted list of (space_id, document_name) tuples.
    """
    cached = _get_cached_documents(project_id)
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
            f"/projects/{project_id}/workitems",
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
    _store_cached_documents(project_id, sorted_docs)
    return sorted_docs


# ---------------------------------------------------------------------------
# Read tools
# ---------------------------------------------------------------------------


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
    page_size: int = Field(
        default=DEFAULT_PAGE_SIZE,
        ge=1,
        le=100,
        description="Number of projects per page (1-100, default 100).",
    ),
    page_number: int = Field(
        default=1,
        ge=1,
        description="Page number to retrieve (1-based, default 1).",
    ),
) -> PaginatedResult[ProjectSummary]:
    """List Polarion projects the authenticated user can access.

    Use this to discover project IDs for other tools. Polarion's Lucene
    supports trailing wildcards (``name:ILCU*``) but rejects leading
    wildcards (``*foo*``) with HTTP 400.

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
            attrs = item.get("attributes", {})
            if not isinstance(attrs, dict):
                attrs = {}
            active_attr = attrs.get("active")
            active = active_attr if isinstance(active_attr, bool) else True
            items.append(
                ProjectSummary(
                    id=safe_str(item.get("id", "")),
                    name=safe_str(attrs.get("name", "")),
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
    page_size: int = Field(
        default=DEFAULT_PAGE_SIZE,
        ge=1,
        le=100,
        description="Number of documents per page (1-100, default 100).",
    ),
    page_number: int = Field(
        default=1,
        ge=1,
        description="Page number to retrieve (1-based, default 1).",
    ),
) -> PaginatedResult[DocumentSummary]:
    """List all documents in a Polarion project.

    Returns ``(space_id, document_name)`` pairs that other document
    tools accept. First call per project performs a full discovery scan
    and caches the result for 60 seconds, so subsequent paginated calls
    are cheap.

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

    # Linear scan over heading work items, with a 60s TTL cache keyed by
    # project_id so paginated callers reuse the same discovery result.
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

    # Manual pagination over the discovered (already sorted) list.
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
    ``include_homepage_content_html=True`` the ``content_html`` field
    carries ``homePageContent`` as raw Polarion HTML — the exact shape
    that round-trips through ``update_document(home_page_content_html=...)``
    losslessly (no Markdown conversion, no sanitization).

    ``homePageContent`` is the inline prose only — heading text and
    embedded work-item bodies live in separate work items. For end-to-end
    reading use ``read_document``; for structural metadata
    use ``read_document_parts``. Only feed ``content_html`` back to
    ``update_document`` when the read flag was True (a False read blanks
    the field, and the empty string is rejected at the write side).

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
    encoded_name = encode_path_segment(document_name)
    path = (
        f"/projects/{project_id}"
        f"/spaces/{encode_path_segment(space_id)}"
        f"/documents/{encoded_name}"
    )

    # ``@all`` is the only sparse-fieldset token this Polarion server
    # honours for surfacing inline custom document attributes. Explicit
    # field lists silently drop them; ``customFields.@all`` / ``@custom``
    # are no-ops on this server. The bandwidth cost — ``homePageContent``
    # always travels over the wire — is paid in network bytes only; the
    # tool still hides the body from the LLM when
    # ``include_homepage_content_html=False``.
    try:
        response = await client.get(
            path,
            params={"fields[documents]": DOC_DETAIL_FIELDS},
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
    attrs = data.get("attributes", {})
    if not isinstance(attrs, dict):
        attrs = {}

    content_html = ""
    if include_homepage_content_html:
        # homePageContent is always HTML:
        # { "type": "text/html", "value": "<p>...</p>" }
        # Pass through verbatim — no markdownify, no sanitize — so the
        # value round-trips through update_document(home_page_content_html=)
        # without losing Polarion-specific spans / data attributes.
        content_obj = attrs.get("homePageContent", {})
        if isinstance(content_obj, dict):
            content_html = safe_str(content_obj.get("value", ""))

    return DocumentDetail(
        title=safe_str(attrs.get("title", "")),
        type=safe_str(attrs.get("type", "")),
        status=safe_str(attrs.get("status", "")),
        content_html=content_html,
        custom_fields=extract_custom_fields(attrs, STANDARD_DOCUMENT_ATTRS),
    )


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
    doc_type: str = Field(
        description=(
            "Document type id (e.g. 'systemReqSpecification')."
            " Pass '~' for type-agnostic options."
        ),
    ),
    page_size: int = Field(
        default=DEFAULT_PAGE_SIZE,
        ge=1,
        le=100,
        description="Number of options per page (1-100, default 100).",
    ),
    page_number: int = Field(
        default=1,
        ge=1,
        description="Page number to retrieve (1-based, default 1).",
    ),
) -> PaginatedResult[EnumOption]:
    """List valid enum options for a document field of the given document type.

    Call this before ``update_document`` when you need to pick a value
    for a document's ``status`` / ``type`` / custom enum field. Polarion
    validates these values leniently on write -- unknown ids are silently
    coerced or stored verbatim -- so resolve them first. This tool covers
    DOCUMENT fields only; work-item fields use a separate endpoint and
    are not surfaced here.

    Returns the FULL enum set for the field on the given doc type.
    Workflow transitions are NOT filtered by the document's current
    state; that is a separate Polarion endpoint not exposed here.
    ``doc_type='~'`` returns the type-agnostic option set. An unknown
    ``doc_type`` is silently treated as ``~`` by Polarion with no error,
    so verify the type id (e.g. via ``get_document``) before trusting
    the result.

    Args:
        ctx: MCP tool context (injected automatically).
        project_id: Polarion project ID.
        field_id: Field id whose options to list.
        doc_type: Document type id, or '~' for type-agnostic.
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
        ValueError: Project, field, or document type not found.
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
        "type": doc_type,
        "page[size]": page_size,
        "page[number]": page_number,
    }
    try:
        response = await client.get(path, params=params)
    except PolarionNotFoundError as exc:
        raise ValueError(
            f"No enum options for field '{field_id}' on document type "
            f"'{doc_type}' in project '{project_id}'."
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
async def read_document_parts(  # noqa: PLR0913
    ctx: Context,
    project_id: str = Field(description="Polarion project ID."),
    space_id: str = Field(description="Space ID containing the document."),
    document_name: str = Field(description="Document name within the space."),
    page_size: int = Field(
        default=DEFAULT_PAGE_SIZE,
        ge=1,
        le=100,
        description="Number of parts per page (1-100, default 100).",
    ),
    page_number: int = Field(
        default=1,
        ge=1,
        description="Page number to retrieve (1-based, default 1).",
    ),
) -> PaginatedResult[DocumentPart]:
    """List the structural parts of a document in order.

    Use this when you need part IDs for ``move_work_item_to_document``,
    heading levels, or per-work-item type/status. Each ``workitem`` part
    already carries its ``description`` as Markdown, so a follow-up
    ``get_work_item`` is unnecessary when scanning bodies. For plain
    reading prefer ``read_document``.

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
    encoded_name = encode_path_segment(document_name)
    path = (
        f"/projects/{project_id}"
        f"/spaces/{encode_path_segment(space_id)}"
        f"/documents/{encoded_name}/parts"
    )

    try:
        response = await client.get(
            path,
            params={
                "fields[document_parts]": "@all",
                # DocumentPart only consumes title/type/status/description
                # from the linked WI — sending ``@all`` here would ship
                # every inline custom field (often KBs per page) for no
                # downstream use. WI_PART_FIELDS keeps the payload tight.
                "fields[workitems]": WI_PART_FIELDS,
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

    # Build lookup of included work items (keyed by full JSON:API id).
    wi_map = build_included_workitem_map(response)

    data = response.get("data", [])
    items: list[DocumentPart] = []
    if isinstance(data, list):
        for item in data:
            part = _parse_document_part(item, wi_map)
            if part is not None:
                items.append(part)

    # Only use the seen-item count as a lower bound when the server did not
    # provide a usable total and the current page is non-empty. Using the
    # requested offset for an empty out-of-range page can massively inflate
    # the reported total_count.
    raw_doc_total = extract_total_count(response)
    doc_total = raw_doc_total
    if doc_total <= 0 and items:
        doc_total = (page_number - 1) * page_size + len(items)

    return PaginatedResult[DocumentPart](
        items=items,
        total_count=doc_total,
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
    page_size: int = Field(
        default=DEFAULT_PAGE_SIZE,
        ge=1,
        le=100,
        description="Number of parts per page (1-100, default 100).",
    ),
    page_number: int = Field(
        default=1,
        ge=1,
        description="Page number to retrieve (1-based, default 1).",
    ),
) -> DocumentReadResult:
    """Render a Polarion document end-to-end as flowing Markdown.

    Paginates ``read_document_parts`` internally and interleaves heading
    titles, embedded work-item descriptions, and inline prose into a
    single Markdown stream — the canonical way to read a document body.
    Empty placeholder paragraphs are skipped.

    Output is read-only synthesis: do NOT feed it back to ``update_document``
    (the rendered Markdown collapses Polarion's ID-anchored placeholder
    structure and the round-trip would orphan headings). Use
    ``get_document(include_homepage_content_html=True)`` for round-trip
    editing of the raw HTML source.

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
    # FastMCP 3.0's ``@mcp.tool()`` returns the original function unchanged,
    # so direct invocation forwards both the fetch and the error mapping.
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
            "Optional Lucene filter (e.g. 'type:requirement', 'title:SRS*'); "
            "trailing wildcards only."
        ),
    ),
    page_size: int = Field(
        default=DEFAULT_PAGE_SIZE,
        ge=1,
        le=100,
        description="Number of work items per page (1-100, default 100).",
    ),
    page_number: int = Field(
        default=1,
        ge=1,
        description="Page number to retrieve (1-based, default 1).",
    ),
) -> PaginatedResult[WorkItemSummary]:
    """List and search work items in a Polarion project.

    Pass a Lucene ``query`` (`type:requirement`, `status:approved AND
    type:requirement`, `title:SRS*`) or omit it for all WIs. Leading
    wildcards (`*foo*`) return HTTP 400. ``module`` is not indexed.

    Description body text is NOT indexed — for content search, scan
    ``read_document_parts`` (each ``workitem`` part already carries its
    description) or use ``read_document`` for end-to-end reading.

    Args:
        ctx: MCP tool context (injected automatically).
        project_id: Polarion project ID.
        query: Optional Lucene filter.
        page_size: Items per page (1-100, default 100).
        page_number: 1-based page number (default 1).

    Returns:
        PaginatedResult of ``WorkItemSummary`` items with ``id``,
        ``title``, ``type``, ``status``, ``priority``, ``updated``,
        ``space_id``, ``document_name``, and ``assignee_ids``.

    Raises:
        ValueError: Project not found.
        PermissionError: Token lacks permission.
        RuntimeError: Other Polarion API errors (incl. bad Lucene syntax).
    """
    client = get_client(ctx)
    params: dict[str, str | int] = {
        "fields[workitems]": WI_LIST_FIELDS,
        # Polarion does not inline ``data`` for to-many relationships
        # unless the resource is included; ``include=assignee`` ensures
        # ``relationships.assignee.data`` is populated so that
        # ``assignee_ids`` can be extracted.
        "include": "assignee",
        "page[size]": page_size,
        "page[number]": page_number,
    }
    if query is not None:
        params["query"] = query
    try:
        response = await client.get(
            f"/projects/{project_id}/workitems",
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

    # Trust a non-zero API total when present. Only use the seen-item
    # count as a lower bound when the API total is missing/zero and the
    # current page actually contains items.
    raw_wi_total = extract_total_count(response)
    wi_total = raw_wi_total
    if wi_total == 0 and items:
        wi_total = (page_number - 1) * page_size + len(items)

    return PaginatedResult[WorkItemSummary](
        items=items,
        total_count=wi_total,
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

    With ``include_description_html=True`` the ``description_html`` field
    carries the raw Polarion HTML body — the exact shape that round-trips
    through ``update_work_item(description_html=...)`` losslessly. Only
    feed it back when this flag was True (a False read blanks the field,
    and the update tool treats ``""`` as ``leave unchanged``).

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
    path = f"/projects/{project_id}/workitems/{work_item_id}"
    try:
        response = await client.get(
            path,
            params={
                "fields[workitems]": WI_DETAIL_FIELDS,
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
    if not include_description_html:
        # Polarion has no sparse-fieldset that surfaces customs while
        # excluding ``description``, so the body still travels over the
        # wire. Blanking it here saves LLM context tokens — that is the
        # contract advertised on the ``include_description_html`` flag.
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

    Synthesis variant of ``get_work_item``: returns the same metadata
    fields plus ``description`` as Markdown (converted from Polarion
    HTML via ``html_to_markdown()``) instead of ``description_html``.
    Use this when an LLM needs to read or summarise the body. The
    converter collapses Polarion-specific spans and ID anchors, so the
    Markdown is read-only — do NOT feed it back to ``update_work_item``.
    For round-trip editing, pair
    ``get_work_item(include_description_html=True)`` with
    ``update_work_item(description_html=...)``.

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
    # Delegate fetch + error mapping to ``get_work_item``; FastMCP 3.0's
    # ``@mcp.tool`` returns the original function unchanged, so the call
    # forwards directly. Pulling the raw HTML lets the converter run
    # without an extra round trip.
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
async def get_linked_work_items(  # noqa: PLR0913
    ctx: Context,
    project_id: str = Field(description="Polarion project ID."),
    work_item_id: str = Field(description="Work Item ID (e.g. 'MCPT-001')."),
    direction: Literal["forward", "back"] = Field(
        default="forward",
        description=(
            "'forward' (outgoing) or 'back' (incoming); call twice if both needed."
        ),
    ),
    page_size: int = Field(
        default=DEFAULT_PAGE_SIZE,
        ge=1,
        le=100,
        description="Number of links per page (1-100, default 100).",
    ),
    page_number: int = Field(
        default=1,
        ge=1,
        description="Page number to retrieve (1-based, default 1).",
    ),
) -> PaginatedResult[LinkedWorkItemSummary]:
    """Get linked work items (forward or back) for a work item.

    A single call returns one direction; call twice when both are needed.
    The ``suspect`` flag indicates whether the linked item has changed
    since the link was last reviewed (forward only).

    Args:
        ctx: MCP tool context (injected automatically).
        project_id: Polarion project ID.
        work_item_id: Work Item ID (e.g. 'MCPT-001').
        direction: 'forward' (outgoing) or 'back' (incoming). Default 'forward'.
        page_size: Items per page (1-100, default 100).
        page_number: 1-based page number (default 1).

    Returns:
        PaginatedResult of ``LinkedWorkItemSummary`` items with ``id``,
        ``title``, ``direction``, ``role``, ``suspect``, ``type``,
        ``status``, ``space_id``, and ``document_name``.

        ``role`` is ``None`` for back-direction links — Polarion's
        ``linkedWorkItems:`` query does not expose the originating role
        on this server. Recover it by calling forward on the source WI.

    Raises:
        ValueError: Work item or project not found.
        PermissionError: Token lacks permission.
        RuntimeError: Other Polarion API errors.
    """
    client = get_client(ctx)

    if direction == "forward":
        return await _get_forward_linked_page(
            client,
            project_id=project_id,
            work_item_id=work_item_id,
            page_size=page_size,
            page_number=page_number,
        )
    return await _get_back_linked_page(
        client,
        project_id=project_id,
        work_item_id=work_item_id,
        page_size=page_size,
        page_number=page_number,
    )


async def _get_forward_linked_page(
    client: PolarionClient,
    *,
    project_id: str,
    work_item_id: str,
    page_size: int,
    page_number: int,
) -> PaginatedResult[LinkedWorkItemSummary]:
    """Fetch a single page of forward (outgoing) links."""
    path = f"/projects/{project_id}/workitems/{work_item_id}/linkedworkitems"
    try:
        response = await client.get(
            path,
            params={
                "fields[linkedworkitems]": "@all",
                "fields[workitems]": WI_LIST_FIELDS,
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

    items = _parse_linked_items(response, direction="forward")

    raw_total = extract_total_count(response)
    total = raw_total
    if total <= 0 and items:
        total = (page_number - 1) * page_size + len(items)

    return PaginatedResult[LinkedWorkItemSummary](
        items=items,
        total_count=total,
        page=page_number,
        page_size=page_size,
        has_more=compute_has_more(
            response, raw_total, page_number, page_size, len(items)
        ),
    )


async def _get_back_linked_page(
    client: PolarionClient,
    *,
    project_id: str,
    work_item_id: str,
    page_size: int,
    page_number: int,
) -> PaginatedResult[LinkedWorkItemSummary]:
    """Fetch a single page of back (incoming) links via Lucene query."""
    try:
        response = await client.get(
            f"/projects/{project_id}/workitems",
            params={
                "query": f"linkedWorkItems:{work_item_id}",
                "fields[workitems]": WI_LIST_FIELDS,
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
    items = [summary_to_back_linked(s) for s in summaries]

    raw_total = extract_total_count(response)
    total = raw_total
    if total <= 0 and items:
        total = (page_number - 1) * page_size + len(items)

    return PaginatedResult[LinkedWorkItemSummary](
        items=items,
        total_count=total,
        page=page_number,
        page_size=page_size,
        has_more=compute_has_more(
            response, raw_total, page_number, page_size, len(items)
        ),
    )
