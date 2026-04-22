"""Read-only MCP tools for querying Polarion ALM.

Seven tools that retrieve projects, documents, work items, and their
relationships.  Every tool returns Pydantic models -- never raw
``dict`` -- and converts HTML descriptions to Markdown via
``html_to_markdown()``.
"""

from __future__ import annotations

import re
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
    part_id = safe_str(item.get("id", ""))

    # Type ------------------------------------------------------------------
    raw_type = safe_str(attrs.get("type", ""))
    part_type: _PartType = cast(
        _PartType,
        raw_type if raw_type in _VALID_PART_TYPES else "normal",
    )

    # Content ---------------------------------------------------------------
    content_html = ""
    content_obj = attrs.get("content")
    if isinstance(content_obj, dict):
        content_html = safe_str(content_obj.get("value", ""))
    elif isinstance(content_obj, str):
        content_html = content_obj

    # Heading level from HTML tag (e.g. <h2 …> → 2) ------------------------
    level = 0
    if part_type == "heading":
        heading_match = re.match(r"<h(\d)", content_html, re.IGNORECASE)
        if heading_match:
            level = int(heading_match.group(1))

    # Relationships ---------------------------------------------------------
    rels = item.get("relationships", {})
    if not isinstance(rels, dict):
        rels = {}

    next_part_id = extract_relationship_id(rels, "nextPart")
    previous_part_id = extract_relationship_id(rels, "previousPart")

    # Resolve title & description from the included work item ---------------
    title = ""
    description_html = ""
    wi_full_id = extract_relationship_id(rels, "workItem")
    if wi_full_id:
        wi = wi_map.get(wi_full_id, {})
        wi_attrs = wi.get("attributes", {})
        if isinstance(wi_attrs, dict):
            title = safe_str(wi_attrs.get("title", ""))
            desc_obj = wi_attrs.get("description")
            if isinstance(desc_obj, dict):
                description_html = safe_str(desc_obj.get("value", ""))
            elif isinstance(desc_obj, str):
                description_html = desc_obj

    return DocumentPart(
        id=part_id,
        title=title,
        content=html_to_markdown(content_html),
        type=part_type,
        level=level,
        description=(
            html_to_markdown(description_html) if part_type == "workitem" else ""
        ),
        next_part_id=next_part_id,
        previous_part_id=previous_part_id,
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


async def _discover_documents(
    client: PolarionClient,
    project_id: str,
) -> set[tuple[str, str]]:
    """Discover all unique (space_id, document_name) pairs via binary search.

    Because heading work items are sorted by ``module``, all headings for the
    same document are contiguous.  This lets us **binary search** for document
    transition boundaries rather than scanning every page linearly.

    Complexity: O(D * log(N / D)) page fetches, where D is the number of
    unique documents and N is the total number of pages.

    Args:
        client: Active ``PolarionClient`` instance.
        project_id: Polarion project ID.

    Returns:
        Set of (space_id, document_name) tuples.
    """
    base_params: dict[str, str | int] = {
        "fields[workitems]": "module",
        "query": "type:heading",
        "sort": "module",
        "page[size]": DEFAULT_PAGE_SIZE,
    }

    async def _fetch_page(page: int) -> list[object]:
        """Fetch a single page and return the ``data`` array."""
        resp = await client.get(
            f"/projects/{project_id}/workitems",
            params={**base_params, "page[number]": page},
        )
        data = resp.get("data", [])
        return data if isinstance(data, list) else []

    # -- Step 1: fetch page 1 to get the total count. --------------------
    first_resp = await client.get(
        f"/projects/{project_id}/workitems",
        params={**base_params, "page[number]": 1},
    )
    first_data = first_resp.get("data", [])
    if not isinstance(first_data, list) or not first_data:
        return set()

    total_count = extract_total_count(first_resp)
    max_page = max(1, -(-total_count // DEFAULT_PAGE_SIZE))  # ceil div

    documents: set[tuple[str, str]] = set()
    for item in first_data:
        _extract_document_pair(item, documents)

    if max_page <= 1:
        return documents

    # -- Step 2: binary search for successive document boundaries. -------
    # ``last_module`` is the module ID of the *last* item on the most
    # recently processed boundary page.  Because results are sorted,
    # all items between the current position and the next transition have
    # the same (or earlier) module.
    last_module = _get_module_id(first_data[-1])
    current_page = 1

    while current_page < max_page:
        # Binary-search for the first page after ``current_page`` where
        # at least one item has a module different from ``last_module``.
        lo, hi = current_page + 1, max_page
        transition_page = 0
        transition_data: list[object] = []

        while lo <= hi:
            mid = (lo + hi) // 2
            mid_data = await _fetch_page(mid)
            if not mid_data:
                hi = mid - 1
                continue

            # Collect documents from every probed page (free wins).
            for item in mid_data:
                _extract_document_pair(item, documents)

            # Check monotonic property: does this page have any item
            # whose module differs from ``last_module``?
            has_new = any(_get_module_id(it) != last_module for it in mid_data)

            if has_new:
                transition_page = mid
                transition_data = mid_data
                hi = mid - 1
            else:
                lo = mid + 1

        if transition_page == 0:
            break  # No more transitions — all documents discovered.

        # Advance to the transition page for the next iteration.
        current_page = transition_page
        last_module = _get_module_id(transition_data[-1])

    return documents


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

    # Binary-search discovery: O(D * log(N/D)) page fetches instead of
    # O(N) linear scanning, where D = unique documents, N = total pages.
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

    filtered: list[tuple[str, str]] = sorted(documents)
    total = len(filtered)

    # Manual pagination over the extracted set.
    start = (page_number - 1) * page_size
    end = start + page_size
    page_slice = filtered[start:end]

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
) -> DocumentDetail:
    """Get a quick overview of a Polarion document.

    Retrieves the title, metadata, and **body content** of a document.
    The ``content`` field contains the home-page HTML converted to
    Markdown.  Note that Polarion stores **heading text in work-item
    titles**, not in the document body, so headings may appear without
    text.  Empty headings are automatically stripped from the output.

    Use this tool for a **fast summary or metadata lookup**.  For the
    full document structure with heading titles and work-item
    descriptions, use ``get_document_parts`` instead.

    Use ``list_documents`` first to discover valid space IDs and
    document names.

    Args:
        ctx: MCP tool context (injected automatically).
        project_id: Polarion project ID.
        space_id: Space ID containing the document.
        document_name: Document name within the space.

    Returns:
        DocumentDetail with:
        - ``id``: Document identifier.
        - ``title``: Document title.
        - ``content``: Document body in Markdown (empty headings stripped).
        - ``space_id``: Containing space.
        - ``project_id``: Containing project.

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

    try:
        response = await client.get(
            path,
            params={"fields[documents]": "title,homePageContent"},
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
        id=safe_str(attrs.get("id", data.get("id", ""))),
        title=safe_str(attrs.get("title", "")),
        content=content_md,
        space_id=space_id,
        project_id=project_id,
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

    Returns the ordered list of part IDs, titles, and types that make
    up a document's body.  Use this tool **only** when you need:

    - Part IDs for positioning with ``create_document_part``
      (``next_part_id`` / ``previous_part_id``).
    - The hierarchical structure (heading levels) of the document.

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
        - ``id``: Full JSON:API part identifier
          (e.g. 'projectId/spaceId/documentName/heading_MCPT-001').
        - ``title``: Part title from the associated work item.
        - ``content``: Part body content in Markdown.
        - ``type``: 'heading', 'workitem', 'normal', 'toc',
          or 'wikiblock'.
        - ``level``: Heading level (1-4) or 0 for non-headings.
        - ``description``: Work item description in Markdown.
        - ``next_part_id``: Full ID of the next part.
        - ``previous_part_id``: Full ID of the previous part.

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

    Returns a paginated list of work items with basic metadata (title,
    type, status).  Pass a Lucene ``query`` to filter results, or omit
    it to list all work items.

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

    # Extract description HTML.
    desc_obj = attrs.get("description", {})
    desc_html = ""
    if isinstance(desc_obj, dict):
        desc_html = safe_str(desc_obj.get("value", ""))

    # Extract work item ID from JSON:API id
    # (format: "projectId/WI-001").
    raw_id = safe_str(data.get("id", ""))
    wi_id = raw_id.split("/", maxsplit=1)[-1] if "/" in raw_id else raw_id

    return WorkItemDetail(
        id=wi_id or work_item_id,
        title=safe_str(attrs.get("title", "")),
        type=safe_str(attrs.get("type", "")),
        status=safe_str(attrs.get("status", "")),
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
