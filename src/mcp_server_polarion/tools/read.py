"""Read-only MCP tools for querying Polarion ALM.

Seven tools that retrieve projects, documents, work items, and their
relationships.  Every tool returns Pydantic models -- never raw
``dict`` -- and converts HTML descriptions to Markdown via
``html_to_markdown()``.
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
    DocumentSummary,
    LinkedWorkItemsList,
    LinkedWorkItemSummary,
    PaginatedResult,
    ProjectSummary,
    WorkItemDetail,
    WorkItemSummary,
)
from mcp_server_polarion.server import mcp
from mcp_server_polarion.tools._helpers import (
    DEFAULT_PAGE_SIZE,
    WI_DETAIL_FIELDS,
    WI_LIST_FIELDS,
    build_included_workitem_map,
    build_work_item_summary_kwargs,
    compute_has_more,
    encode_path_segment,
    extract_relationship_id,
    extract_total_count,
    get_client,
    has_links_next,
    parse_work_item_summaries,
    safe_str,
)
from mcp_server_polarion.utils import html_to_markdown

# Valid document part types returned by Polarion.
type _PartType = Literal["heading", "workitem", "normal", "toc", "wikiblock"]
_VALID_PART_TYPES: Final[frozenset[str]] = frozenset(
    {"heading", "workitem", "normal", "toc", "wikiblock"},
)

# ---------------------------------------------------------------------------
# Read-specific helpers
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class _LinkedWorkItem:
    """Metadata extracted from an included work-item resource."""

    short_id: str = ""
    title: str = ""
    type: str = ""
    status: str = ""
    description_html: str = ""


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

    raw_type = safe_str(attrs.get("type", ""))
    part_type: _PartType = cast(
        _PartType,
        raw_type if raw_type in _VALID_PART_TYPES else "normal",
    )

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

    full_id = safe_str(item.get("id", ""))
    short_id = full_id.rsplit("/", maxsplit=1)[-1] if "/" in full_id else full_id

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
        next_part_id=next_short_id,
    )


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
        wi_id = (
            wi_full_id.split("/", maxsplit=1)[-1] if "/" in wi_full_id else wi_full_id
        )

        # Resolve title from included work items.
        title = ""
        if wi_full_id:
            wi = wi_map.get(wi_full_id, {})
            wi_attrs = wi.get("attributes", {})
            if isinstance(wi_attrs, dict):
                title = safe_str(wi_attrs.get("title", ""))

        # Skip items where the target work item cannot be resolved
        # via the relationships object (per project conventions, we do
        # not parse the raw linked-work-item ID).
        if not wi_id:
            continue

        items.append(
            LinkedWorkItemSummary(
                id=wi_id,
                title=title,
                role=role,
                direction=direction,
                suspect=suspect,
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

    Iterates every heading-workitem page (page_size=100) in order and
    accumulates unique ``module`` relationship IDs into a set. Results
    are cached for ``_CACHE_TTL_SECONDS`` to amortise the cost across
    paginated callers.

    A linear scan is preferred over binary search because, in practice,
    each page returns work items belonging to ~1 document (so N ≈ D and
    binary search wastes log(N) probes per transition).

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
            "Lucene query string for server-side filtering "
            "(e.g. 'name:ILCU*'). Trailing wildcards are supported; "
            "leading wildcards are not supported by Polarion. "
            "Omit to return all accessible projects."
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
    """List all accessible Polarion projects, with optional server-side filtering.

    Returns a paginated list of Polarion projects the authenticated user
    can access. Use this tool to discover valid project IDs for other
    tools, and request additional pages when ``has_more`` is ``True``.

    When ``query`` is omitted, all projects are fetched. When ``query``
    is provided, Polarion performs a server-side Lucene search (trailing
    wildcards supported, e.g. ``name:ILCU*``).

    Args:
        ctx: MCP tool context (injected automatically).
        query: Lucene expression to filter projects on the server
            (e.g. ``'name:ILCU*'``). Leading wildcards (``*foo*``) are
            rejected by Polarion with HTTP 400. Omit for no filter.
        page_size: Number of projects per page (1-100, default 100).
        page_number: Page number to retrieve (1-based, default 1).

    Returns:
        PaginatedResult containing ``ProjectSummary`` items with:
        - ``id``: Project identifier.
        - ``name``: Human-readable project name.
        - ``active``: Whether the project is active (True) or archived
          (False). Use this to skip archived projects when picking a
          target. Defaults to True when the server does not report the
          flag.
        - ``total_count``: Total number of matching projects.
        - ``page`` / ``page_size``: Current pagination state.
        - ``has_more``: True if more pages exist.

    Raises:
        PermissionError: If the authentication token is invalid or
            lacks permissions.
        RuntimeError: On unexpected Polarion API errors.
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
    project_id: str = Field(
        description=(
            "Polarion project ID (e.g. 'myproject'). "
            "Use ``list_projects`` to discover valid IDs."
        ),
    ),
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

    Discovers all documents server-side, then returns a paginated
    subset.  Callers may need multiple pages depending on ``page_size``.

    Returns space IDs and document names so the LLM can call
    ``get_document`` or ``get_document_parts`` with the correct
    parameters.

    Since ``GET /projects/{projectId}/documents`` is not available on
    the target Polarion version, this tool queries heading-type work
    items with ``fields[workitems]=module`` and ``query=type:heading``
    to extract unique (space_id, document_name) pairs from the
    ``relationships.module.data.id`` field (format:
    ``projectId/spaceId/documentName``).

    Args:
        ctx: MCP tool context (injected automatically).
        project_id: Polarion project ID.
        page_size: Number of documents per page (1-100, default 100).
        page_number: Page number to retrieve (1-based, default 1).

    Returns:
        PaginatedResult containing ``DocumentSummary`` items with:
        - ``space_id``: Space that contains the document.
        - ``document_name``: Document name within the space.
        - ``total_count``: Total number of documents found.

    Raises:
        ValueError: If the project ID is invalid or not found.
        PermissionError: If the token lacks permissions.
        RuntimeError: On unexpected Polarion API errors.
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
    project_id: str = Field(
        description="Polarion project ID.",
    ),
    space_id: str = Field(
        description=(
            "Space ID that contains the document (e.g. '_default'). "
            "Use ``list_documents`` to discover valid IDs."
        ),
    ),
    document_name: str = Field(
        description=(
            "Document name within the space "
            "(e.g. 'Software Requirement Specification'). "
            "Spaces in the name are handled automatically."
        ),
    ),
    include_content: bool = Field(
        default=False,
        description=(
            "When True, also fetch and return the document's homePageContent "
            "as Markdown in the ``content`` field. Off by default because "
            "homePageContent can be large (tens of KB) and most callers only "
            "need metadata. Note: Polarion stores actual document body in "
            "work-item titles/descriptions — use ``get_document_parts`` for "
            "the structured body."
        ),
    ),
) -> DocumentDetail:
    """Get metadata for a Polarion document.

    Returns the document's title, type, and workflow status. By default
    the ``content`` field is empty; set ``include_content=True`` to also
    fetch ``homePageContent`` (converted to Markdown). Empty headings
    inside the home-page content are stripped automatically.

    Polarion stores **heading text in work-item titles**, not in
    ``homePageContent``. For the structured body of a document, call
    ``get_document_parts`` — this tool is for fast metadata lookup.

    Use ``list_documents`` first to discover valid space IDs and
    document names.

    Args:
        ctx: MCP tool context (injected automatically).
        project_id: Polarion project ID.
        space_id: Space ID containing the document.
        document_name: Document name within the space.
        include_content: When True, also return the homePageContent
            converted to Markdown. Default False to keep the response
            small.

    Returns:
        DocumentDetail with:
        - ``title``: Document title.
        - ``type``: Document type (e.g. 'req_specification').
        - ``status``: Workflow status (e.g. 'draft').
        - ``content``: Document body in Markdown — only populated when
        - ``include_content=True``, otherwise empty.

    Raises:
        ValueError: If the document, space, or project is not found.
        PermissionError: If the token lacks permissions.
        RuntimeError: On unexpected Polarion API errors.
    """
    client = get_client(ctx)
    encoded_name = encode_path_segment(document_name)
    path = (
        f"/projects/{project_id}"
        f"/spaces/{encode_path_segment(space_id)}"
        f"/documents/{encoded_name}"
    )

    fields = "title,type,status"
    if include_content:
        fields = f"{fields},homePageContent"

    try:
        response = await client.get(
            path,
            params={"fields[documents]": fields},
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

    content_md = ""
    if include_content:
        # homePageContent is always HTML:
        # { "type": "text/html", "value": "<p>...</p>" }
        content_obj = attrs.get("homePageContent", {})
        content_html = ""
        if isinstance(content_obj, dict):
            content_html = safe_str(content_obj.get("value", ""))

        content_md = html_to_markdown(content_html)
        # Polarion stores heading text in work-item titles, so the
        # document body often contains empty headings (e.g. "## \n").
        # Strip them to reduce noise for the LLM.
        content_md = re.sub(r"^#{1,6}\s*$", "", content_md, flags=re.MULTILINE)
        content_md = re.sub(r"\n{3,}", "\n\n", content_md).strip()

    return DocumentDetail(
        title=safe_str(attrs.get("title", "")),
        type=safe_str(attrs.get("type", "")),
        status=safe_str(attrs.get("status", "")),
        content=content_md,
    )


@mcp.tool(
    tags={"read"},
    timeout=60.0,
    annotations={"readOnlyHint": True},
)
async def get_document_parts(  # noqa: PLR0913
    ctx: Context,
    project_id: str = Field(
        description="Polarion project ID.",
    ),
    space_id: str = Field(
        description=(
            "Space ID that contains the document. "
            "Use ``list_documents`` to discover valid IDs."
        ),
    ),
    document_name: str = Field(
        description="Document name within the space.",
    ),
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
    """List the structural parts (headings and work items) of a document.

    Returns the ordered list of part IDs, titles, types, and linked
    work-item metadata that make up a document's body. Use this tool
    **only** when you need:

    - Part IDs for positioning with ``create_document_part`` — pass any
      existing part's ``id`` as ``next_part_id`` (insert before) or
      ``previous_part_id`` (insert after). Results are returned in
      document order.
    - The hierarchical structure (heading levels) of the document.
    - The type/status of work items embedded in the document, without a
      separate ``get_work_item`` call.

    **Do NOT call this tool just to read or summarise a document.**
    ``get_document`` already returns the complete document body.

    Args:
        ctx: MCP tool context (injected automatically).
        project_id: Polarion project ID.
        space_id: Space ID containing the document.
        document_name: Document name within the space.
        page_size: Number of parts per page (1-100, default 100).
        page_number: Page number to retrieve (1-based, default 1).

    Returns:
        PaginatedResult containing ``DocumentPart`` items with:
        - ``id``: Short part identifier within the document
          (e.g. 'heading_MCPT-001', 'workitem_MCPT-042', 'polarion_1').
          Use as ``next_part_id`` or ``previous_part_id`` when calling
          ``create_document_part``.
        - ``title``: Part title (from the linked work item for heading
          and workitem parts).
        - ``content``: Part body in Markdown. Empty for 'heading' and
          'workitem' parts (their text lives in ``title`` / ``level``
          and ``description`` respectively).
        - ``type``: 'heading', 'workitem', 'normal', 'toc',
          or 'wikiblock'.
        - ``level``: Heading level (1-4) or 0 for non-headings.
        - ``description``: Work-item description in Markdown
          (workitem parts only).
        - ``work_item_id``: Short linked work-item ID
          (e.g. 'MCPT-001'). Use directly with ``get_work_item`` /
          ``get_linked_work_items``.
        - ``work_item_type``: Linked work-item type
          (e.g. 'requirement', 'testCase').
        - ``work_item_status``: Linked work-item workflow status.
        - ``external``: True when the part references a work item from
          another project (re-used / typically read-only).
        - ``next_part_id``: Short ID of the next part in document order
          (e.g. 'workitem_MCPT-002'). Empty on the last part.

    Raises:
        ValueError: If the document, space, or project is not found.
        PermissionError: If the token lacks permissions.
        RuntimeError: On unexpected Polarion API errors.
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
                "fields[workitems]": WI_DETAIL_FIELDS,
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
async def list_work_items(
    ctx: Context,
    project_id: str = Field(
        description=(
            "Polarion project ID. Use ``list_projects`` to discover valid IDs."
        ),
    ),
    query: str | None = Field(
        default=None,
        description=(
            "Optional Lucene query string for filtering work items. "
            "Examples: 'type:requirement', "
            "'status:approved AND type:requirement', "
            "'title:SRS*' (trailing wildcard only — leading wildcards "
            "like '*SRS*' cause 400 errors). "
            "The ``module`` field is NOT indexed and cannot be queried. "
            "Omit to list all work items without filtering."
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

    Returns a paginated list of work items with the metadata most often
    needed for triage and traceability. Pass a Lucene ``query`` to
    filter results, or omit it to list all work items.

    Common Lucene query examples:

    - ``type:requirement`` — all requirements
    - ``status:approved AND type:requirement`` — approved requirements
    - ``title:Login`` — work items with "Login" in the title
    - ``title:SRS*`` — trailing wildcard (leading wildcards not supported)
    - ``type:testCase AND status:draft`` — draft test cases

    Use ``get_work_item`` for full details including the description.

    Args:
        ctx: MCP tool context (injected automatically).
        project_id: Polarion project ID.
        query: Optional Lucene query string for filtering.
        page_size: Number of work items per page (1-100, default 100).
        page_number: Page number to retrieve (1-based, default 1).

    Returns:
        PaginatedResult containing ``WorkItemSummary`` items with:
        - ``id``: Work Item ID (e.g. 'MCPT-001').
        - ``title``: Work Item title.
        - ``type``: Work Item type (e.g. 'requirement').
        - ``status``: Workflow status (e.g. 'draft').
        - ``priority``: Priority value as a string (empty when unset).
        - ``updated``: ISO-8601 timestamp of the last modification.
        - ``space_id`` / ``document_name``: Document this work item
          belongs to (both empty when not module-bound). Use with
          ``get_document`` / ``get_document_parts``.
        - ``assignee_ids``: Short user IDs of assignees (empty list
          when unassigned).

    Raises:
        ValueError: If the project is not found.
        PermissionError: If the token lacks permissions.
        RuntimeError: On unexpected Polarion API errors (including
            invalid Lucene query syntax).
    """
    client = get_client(ctx)
    params: dict[str, str | int] = {
        "fields[workitems]": WI_LIST_FIELDS,
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
    project_id: str = Field(
        description="Polarion project ID.",
    ),
    work_item_id: str = Field(
        description=(
            "Work Item ID (e.g. 'MCPT-001'). "
            "Use ``list_work_items`` to discover valid IDs."
        ),
    ),
) -> WorkItemDetail:
    """Get full details of a single Polarion work item.

    Retrieves the complete work item including its description (converted
    to Markdown).  Use ``list_work_items`` first to discover valid work
    item IDs.

    Args:
        ctx: MCP tool context (injected automatically).
        project_id: Polarion project ID.
        work_item_id: Work Item ID (e.g. 'MCPT-001').

    Returns:
        WorkItemDetail with:
        - ``id``: Work Item ID.
        - ``title``: Work Item title.
        - ``type``: Work Item type.
        - ``status``: Workflow status.
        - ``priority``: Priority value as a string (empty when unset).
        - ``updated``: ISO-8601 last-modified timestamp.
        - ``space_id`` / ``document_name``: Document this work item
          belongs to (both empty when not module-bound).
        - ``assignee_ids``: Short user IDs of assignees.
        - ``description``: Full description in Markdown.
        - ``project_id``: Containing project.

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
            params={"fields[workitems]": WI_DETAIL_FIELDS},
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
    attrs = data.get("attributes", {})
    if not isinstance(attrs, dict):
        attrs = {}

    desc_obj = attrs.get("description", {})
    desc_html = ""
    if isinstance(desc_obj, dict):
        desc_html = safe_str(desc_obj.get("value", ""))

    summary_kwargs = build_work_item_summary_kwargs(data)
    if not summary_kwargs["id"]:
        summary_kwargs["id"] = work_item_id

    return WorkItemDetail(
        **summary_kwargs,
        description=html_to_markdown(desc_html),
        project_id=project_id,
    )


@mcp.tool(
    tags={"read"},
    timeout=60.0,
    annotations={"readOnlyHint": True},
)
async def get_linked_work_items(
    ctx: Context,
    project_id: str = Field(
        description="Polarion project ID.",
    ),
    work_item_id: str = Field(
        description=(
            "Work Item ID (e.g. 'MCPT-001'). "
            "Use ``list_work_items`` to discover valid IDs."
        ),
    ),
) -> LinkedWorkItemsList:
    """Get all linked work items (forward and back) for a work item.

    Retrieves both outgoing (forward) and incoming (back) links for a
    work item.  This provides complete traceability information such as
    parent, relates_to, verifies, and depends_on relationships.

    The ``suspect`` flag indicates whether the linked item has changed
    since the link was last reviewed.

    Use ``list_work_items`` first to discover valid work item IDs.

    Args:
        ctx: MCP tool context (injected automatically).
        project_id: Polarion project ID.
        work_item_id: Work Item ID (e.g. 'MCPT-001').

    Returns:
        LinkedWorkItemsList with:
        - ``items``: All linked work items (forward and back).
        - ``forward_count``: Number of outgoing links.
        - ``back_count``: Number of incoming links.
        - ``total_count``: Total number of linked items (forward + back).

    Raises:
        ValueError: If the work item or project is not found.
        PermissionError: If the token lacks permissions.
        RuntimeError: On unexpected Polarion API errors.
    """
    client = get_client(ctx)
    base_path = f"/projects/{project_id}/workitems/{work_item_id}"

    try:
        forward_response = await client.get(
            f"{base_path}/linkedworkitems",
            params={
                "fields[linkedworkitems]": "@all",
                "fields[workitems]": WI_LIST_FIELDS,
                "include": "workItem",
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

    forward_items = _parse_linked_items(forward_response, direction="forward")

    back_items: list[LinkedWorkItemSummary] = []
    try:
        back_page = 1
        back_total: int | None = None

        while True:
            back_response = await client.get(
                f"/projects/{project_id}/workitems",
                params={
                    "query": f"linkedWorkItems:{work_item_id}",
                    "fields[workitems]": WI_LIST_FIELDS,
                    "page[size]": DEFAULT_PAGE_SIZE,
                    "page[number]": back_page,
                },
            )

            if back_total is None:
                back_total = extract_total_count(back_response)

            page_summaries = parse_work_item_summaries(
                back_response.get("data", []),
            )
            if not page_summaries:
                break

            back_items.extend(
                LinkedWorkItemSummary(
                    id=summary.id,
                    title=summary.title,
                    role="backlink",
                    suspect=False,
                    direction="back",
                )
                for summary in page_summaries
            )

            if back_total and len(back_items) >= back_total:
                break
            # Prefer links.next as authoritative stop signal;
            # fall back to partial-page heuristic when absent.
            if has_links_next(back_response):
                back_page += 1
                continue
            if len(page_summaries) < DEFAULT_PAGE_SIZE:
                break

            back_page += 1
    except PolarionError as exc:
        raise RuntimeError(
            f"Backlink query failed for work item '{work_item_id}': {exc.message}"
        ) from exc

    all_items = forward_items + back_items
    return LinkedWorkItemsList(
        items=all_items,
        forward_count=len(forward_items),
        back_count=len(back_items),
        total_count=len(all_items),
    )
