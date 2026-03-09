"""Read-only MCP tools for querying Polarion ALM.

Eight tools that retrieve projects, spaces, documents, work items, and
their relationships.  Every tool returns Pydantic models -- never raw
``dict`` -- and converts HTML descriptions to Markdown via
``html_to_markdown()``.
"""

from __future__ import annotations

import logging
from typing import Final
from urllib.parse import quote

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
    LinkedWorkItemsList,
    LinkedWorkItemSummary,
    PaginatedResult,
    ProjectSummary,
    SpaceSummary,
    WorkItemDetail,
    WorkItemSummary,
)
from mcp_server_polarion.server import mcp
from mcp_server_polarion.utils import html_to_markdown

logger: Final = logging.getLogger("mcp_server_polarion.tools.read")

# Default page size -- Polarion caps at 100.
_DEFAULT_PAGE_SIZE: Final[int] = 100

# Sparse fieldset for list/search endpoints.
_WI_LIST_FIELDS: Final[str] = "title,type,status"
_WI_DETAIL_FIELDS: Final[str] = "title,description,type,status"

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _get_client(ctx: Context) -> PolarionClient:
    """Extract ``PolarionClient`` from the lifespan context.

    Args:
        ctx: FastMCP tool context.

    Returns:
        The active ``PolarionClient`` instance.
    """
    lifespan_ctx: dict[str, object] = ctx.request_context.lifespan_context  # type: ignore[union-attr]
    client = lifespan_ctx["polarion_client"]
    if not isinstance(client, PolarionClient):  # pragma: no cover
        msg = "polarion_client is not a PolarionClient instance"
        raise TypeError(msg)
    return client


def _safe_str(value: object) -> str:
    """Convert a value to ``str``, returning ``""`` for ``None``."""
    if value is None:
        return ""
    return str(value)


def _extract_total_count(response: dict[str, object]) -> int:
    """Extract ``meta.totalCount`` from a JSON:API response.

    Args:
        response: Decoded JSON:API response.

    Returns:
        The total count, or 0 if the field is missing.
    """
    meta = response.get("meta")
    if isinstance(meta, dict):
        total = meta.get("totalCount", 0)
        if isinstance(total, int):
            return total
    return 0


def _encode_path_segment(segment: str) -> str:
    """URL-encode a single path segment (e.g. document name with spaces).

    Args:
        segment: Raw path segment string.

    Returns:
        URL-encoded segment safe for use in URL paths.
    """
    return quote(segment, safe="")


# ---------------------------------------------------------------------------
# Read tools
# ---------------------------------------------------------------------------


@mcp.tool()
async def list_projects(
    ctx: Context,
    page_size: int = Field(
        default=_DEFAULT_PAGE_SIZE,
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
    """List all accessible Polarion projects.

    Returns a paginated list of Polarion projects the authenticated user
    can access.  Use this as the starting point to discover valid project
    IDs for other tools.

    Args:
        ctx: MCP tool context (injected automatically).
        page_size: Number of projects per page (1-100, default 100).
        page_number: Page number to retrieve (1-based, default 1).

    Returns:
        PaginatedResult containing ``ProjectSummary`` items with:
        - ``id``: Project identifier.
        - ``name``: Human-readable project name.
        - ``total_count``: Total number of projects.
        - ``page`` / ``page_size``: Current pagination state.

    Raises:
        PermissionError: If the authentication token is invalid or
            lacks permissions.
        RuntimeError: On unexpected Polarion API errors.
    """
    client = _get_client(ctx)
    try:
        response = await client.get(
            "/projects",
            params={
                "page[size]": page_size,
                "page[number]": page_number,
            },
        )
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
            items.append(
                ProjectSummary(
                    id=_safe_str(item.get("id", "")),
                    name=_safe_str(attrs.get("name", "")),
                )
            )

    return PaginatedResult[ProjectSummary](
        items=items,
        total_count=_extract_total_count(response),
        page=page_number,
        page_size=page_size,
    )


@mcp.tool()
async def list_spaces(
    ctx: Context,
    project_id: str = Field(
        description=(
            "Polarion project ID (e.g. 'myproject'). "
            "Use ``list_projects`` to discover valid IDs."
        ),
    ),
    page_size: int = Field(
        default=_DEFAULT_PAGE_SIZE,
        ge=1,
        le=100,
        description="Number of spaces per page (1-100, default 100).",
    ),
    page_number: int = Field(
        default=1,
        ge=1,
        description="Page number to retrieve (1-based, default 1).",
    ),
) -> PaginatedResult[SpaceSummary]:
    """List all spaces (folders) in a Polarion project.

    Spaces are containers for documents.  Use this tool to discover Space
    IDs before calling ``get_document`` or ``get_document_parts``.

    Since ``GET /projects/{projectId}/spaces`` does not exist in the
    target Polarion version, this tool queries work items with
    ``fields[workitems]=module``, parses the ``relationships.module.data.id``
    field (format: ``projectId/spaceId/documentName``) to extract unique
    Space IDs, and returns them as ``SpaceSummary`` items.

    Args:
        ctx: MCP tool context (injected automatically).
        project_id: Polarion project ID.
        page_size: Number of spaces per page (1-100, default 100).
        page_number: Page number to retrieve (1-based, default 1).

    Returns:
        PaginatedResult containing ``SpaceSummary`` items with:
        - ``id``: Space identifier (e.g. '_default', 'Design').
        - ``name``: Defaults to the space ID.
        - ``total_count``: Total number of unique spaces found.

    Raises:
        ValueError: If the project ID is invalid or not found.
        PermissionError: If the token lacks permissions.
        RuntimeError: On unexpected Polarion API errors.
    """
    client = _get_client(ctx)
    try:
        all_items = await client.get_all_pages(
            f"/projects/{project_id}/workitems",
            params={"fields[workitems]": "module"},
        )
    except PolarionNotFoundError as exc:
        raise ValueError(
            f"Project '{project_id}' not found. "
            "Use `list_projects` to discover valid project IDs."
        ) from exc
    except PolarionAuthError as exc:
        raise PermissionError(
            "Cannot list spaces -- check your POLARION_TOKEN permissions."
        ) from exc
    except PolarionError as exc:
        raise RuntimeError(
            f"Failed to list spaces for project '{project_id}': {exc.message}"
        ) from exc

    # Parse module from relationships to extract unique space IDs.
    # The API returns module in relationships.module.data.id with format:
    # "{projectId}/{spaceId}/{documentName}".
    space_ids: set[str] = set()
    for item in all_items:
        rels = item.get("relationships", {})
        if not isinstance(rels, dict):
            continue
        module_rel = rels.get("module", {})
        if not isinstance(module_rel, dict):
            continue
        mod_data = module_rel.get("data")
        if not isinstance(mod_data, dict):
            continue
        mod_id = mod_data.get("id", "")
        if isinstance(mod_id, str) and mod_id:
            parts = mod_id.split("/")
            # Format: "projectId/spaceId/docName" → parts[1] is spaceId
            if len(parts) >= 2:  # noqa: PLR2004
                space_ids.add(parts[1])

    sorted_spaces = sorted(space_ids)
    total = len(sorted_spaces)

    # Manual pagination over the extracted set.
    start = (page_number - 1) * page_size
    end = start + page_size
    page_slice = sorted_spaces[start:end]

    items = [SpaceSummary(id=sid, name=sid) for sid in page_slice]

    return PaginatedResult[SpaceSummary](
        items=items,
        total_count=total,
        page=page_number,
        page_size=page_size,
    )


@mcp.tool()
async def get_document(
    ctx: Context,
    project_id: str = Field(
        description="Polarion project ID.",
    ),
    space_id: str = Field(
        description=(
            "Space ID that contains the document (e.g. '_default'). "
            "Use ``list_spaces`` to discover valid IDs."
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
    """Get full details of a Polarion document.

    Retrieves the title, description, and metadata for a specific
    document in a space.  Use ``list_spaces`` first to discover valid
    space IDs, then call this tool with the space ID and document name.

    The document description is automatically converted from HTML to
    Markdown for easier consumption.

    Args:
        ctx: MCP tool context (injected automatically).
        project_id: Polarion project ID.
        space_id: Space ID containing the document.
        document_name: Document name within the space.

    Returns:
        DocumentDetail with:
        - ``id``: Document identifier.
        - ``title``: Document title.
        - ``description``: Description in Markdown.
        - ``space_id``: Containing space.
        - ``project_id``: Containing project.

    Raises:
        ValueError: If the document, space, or project is not found.
        PermissionError: If the token lacks permissions.
        RuntimeError: On unexpected Polarion API errors.
    """
    client = _get_client(ctx)
    encoded_name = _encode_path_segment(document_name)
    path = (
        f"/projects/{project_id}"
        f"/spaces/{_encode_path_segment(space_id)}"
        f"/documents/{encoded_name}"
    )

    try:
        response = await client.get(path)
    except PolarionNotFoundError as exc:
        raise ValueError(
            f"Document '{document_name}' not found in space "
            f"'{space_id}' of project '{project_id}'. "
            "Use `list_spaces` to verify the space ID."
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

    # Description is always HTML:
    # { "type": "text/html", "value": "<p>...</p>" }
    desc_obj = attrs.get("description", {})
    desc_html = ""
    if isinstance(desc_obj, dict):
        desc_html = _safe_str(desc_obj.get("value", ""))

    return DocumentDetail(
        id=_safe_str(attrs.get("id", data.get("id", ""))),
        title=_safe_str(attrs.get("title", "")),
        description=html_to_markdown(desc_html),
        space_id=space_id,
        project_id=project_id,
    )


@mcp.tool()
async def get_document_parts(  # noqa: PLR0913
    ctx: Context,
    project_id: str = Field(
        description="Polarion project ID.",
    ),
    space_id: str = Field(
        description="Space ID that contains the document.",
    ),
    document_name: str = Field(
        description="Document name within the space.",
    ),
    page_size: int = Field(
        default=_DEFAULT_PAGE_SIZE,
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
    """List the parts (headings and work items) of a Polarion document.

    Returns the ordered list of parts that make up a document's body.
    Each part is either a heading or a work item reference.  Use
    ``get_document`` first to verify the document exists.

    Part content (descriptions) is automatically converted from HTML to
    Markdown.

    Args:
        ctx: MCP tool context (injected automatically).
        project_id: Polarion project ID.
        space_id: Space ID containing the document.
        document_name: Document name within the space.
        page_size: Number of parts per page (1-100, default 100).
        page_number: Page number to retrieve (1-based, default 1).

    Returns:
        PaginatedResult containing ``DocumentPart`` items with:
        - ``id``: Part identifier (e.g. 'heading_MCPT-001').
        - ``title``: Part title or heading text.
        - ``content``: Body content in Markdown.
        - ``type``: 'heading' or 'workitem'.
        - ``level``: Heading level (1-4) or 0 for work items.

    Raises:
        ValueError: If the document, space, or project is not found.
        PermissionError: If the token lacks permissions.
        RuntimeError: On unexpected Polarion API errors.
    """
    client = _get_client(ctx)
    encoded_name = _encode_path_segment(document_name)
    path = (
        f"/projects/{project_id}"
        f"/spaces/{_encode_path_segment(space_id)}"
        f"/documents/{encoded_name}/parts"
    )

    try:
        response = await client.get(
            path,
            params={
                "page[size]": page_size,
                "page[number]": page_number,
            },
        )
    except PolarionNotFoundError as exc:
        raise ValueError(
            f"Document '{document_name}' not found in space "
            f"'{space_id}' of project '{project_id}'. "
            "Use `list_spaces` to discover valid space IDs."
        ) from exc
    except PolarionAuthError as exc:
        raise PermissionError(
            "Cannot access document parts -- check your POLARION_TOKEN permissions."
        ) from exc
    except PolarionError as exc:
        raise RuntimeError(
            f"Failed to get parts for '{document_name}': {exc.message}"
        ) from exc

    data = response.get("data", [])
    items: list[DocumentPart] = []
    if isinstance(data, list):
        for item in data:
            if not isinstance(item, dict):
                continue
            attrs = item.get("attributes", {})
            if not isinstance(attrs, dict):
                attrs = {}
            part_id = _safe_str(item.get("id", ""))

            # Determine type from ID prefix.
            part_type: str = "workitem"
            if part_id.startswith("heading_"):
                part_type = "heading"

            # Extract heading level.
            level = 0
            raw_level = attrs.get("level")
            if isinstance(raw_level, int):
                level = raw_level

            # Content/description is HTML.
            content_obj = attrs.get(
                "content",
                attrs.get("description", {}),
            )
            content_html = ""
            if isinstance(content_obj, dict):
                content_html = _safe_str(
                    content_obj.get("value", ""),
                )
            elif isinstance(content_obj, str):
                content_html = content_obj

            items.append(
                DocumentPart(
                    id=part_id,
                    title=_safe_str(attrs.get("title", "")),
                    content=html_to_markdown(content_html),
                    type=part_type,  # type: ignore[arg-type]
                    level=level,
                )
            )

    return PaginatedResult[DocumentPart](
        items=items,
        total_count=_extract_total_count(response),
        page=page_number,
        page_size=page_size,
    )


@mcp.tool()
async def list_work_items(
    ctx: Context,
    project_id: str = Field(
        description=(
            "Polarion project ID. Use ``list_projects`` to discover valid IDs."
        ),
    ),
    page_size: int = Field(
        default=_DEFAULT_PAGE_SIZE,
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
    """List work items in a Polarion project.

    Returns a paginated list of work items with basic metadata (title,
    type, status).  Use ``search_work_items`` to apply Lucene query
    filters, or ``get_work_item`` for full details including the
    description.

    Args:
        ctx: MCP tool context (injected automatically).
        project_id: Polarion project ID.
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
        RuntimeError: On unexpected Polarion API errors.
    """
    client = _get_client(ctx)
    try:
        response = await client.get(
            f"/projects/{project_id}/workitems",
            params={
                "fields[workitems]": _WI_LIST_FIELDS,
                "page[size]": page_size,
                "page[number]": page_number,
            },
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
    items = _parse_work_item_summaries(data)

    return PaginatedResult[WorkItemSummary](
        items=items,
        total_count=_extract_total_count(response),
        page=page_number,
        page_size=page_size,
    )


@mcp.tool()
async def get_work_item(
    ctx: Context,
    project_id: str = Field(
        description="Polarion project ID.",
    ),
    work_item_id: str = Field(
        description=(
            "Work Item ID (e.g. 'MCPT-001'). "
            "Use ``list_work_items`` or ``search_work_items`` to "
            "discover valid IDs."
        ),
    ),
) -> WorkItemDetail:
    """Get full details of a single Polarion work item.

    Retrieves the complete work item including its description (converted
    to Markdown).  Use ``list_work_items`` or ``search_work_items`` first
    to discover valid work item IDs.

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
    client = _get_client(ctx)
    path = f"/projects/{project_id}/workitems/{work_item_id}"
    try:
        response = await client.get(
            path,
            params={"fields[workitems]": _WI_DETAIL_FIELDS},
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
        desc_html = _safe_str(desc_obj.get("value", ""))

    # Extract work item ID from JSON:API id
    # (format: "projectId/WI-001").
    raw_id = _safe_str(data.get("id", ""))
    wi_id = raw_id.split("/", maxsplit=1)[-1] if "/" in raw_id else raw_id

    return WorkItemDetail(
        id=wi_id or work_item_id,
        title=_safe_str(attrs.get("title", "")),
        type=_safe_str(attrs.get("type", "")),
        status=_safe_str(attrs.get("status", "")),
        description=html_to_markdown(desc_html),
        project_id=project_id,
    )


@mcp.tool()
async def search_work_items(
    ctx: Context,
    project_id: str = Field(
        description="Polarion project ID.",
    ),
    query: str = Field(
        description=(
            "Lucene query string for filtering work items "
            "(e.g. 'type:requirement AND status:approved'). "
            "See Polarion documentation for query syntax."
        ),
    ),
    page_size: int = Field(
        default=_DEFAULT_PAGE_SIZE,
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
    """Search work items using a Lucene query.

    Filters work items in a Polarion project using the Polarion Lucene
    query syntax.  Common query examples:

    - ``type:requirement`` -- all requirements
    - ``status:approved AND type:requirement`` -- approved requirements
    - ``title:Login`` -- work items with "Login" in the title
    - ``type:testCase AND status:draft`` -- draft test cases

    Use ``list_work_items`` without a query to retrieve all work items,
    or ``get_work_item`` for full details of a specific work item.

    Args:
        ctx: MCP tool context (injected automatically).
        project_id: Polarion project ID.
        query: Lucene query string.
        page_size: Number of work items per page (1-100, default 100).
        page_number: Page number to retrieve (1-based, default 1).

    Returns:
        PaginatedResult containing ``WorkItemSummary`` items matching
        the query, with the same fields as ``list_work_items``.

    Raises:
        ValueError: If the project is not found.
        PermissionError: If the token lacks permissions.
        RuntimeError: On unexpected Polarion API errors (including
            invalid Lucene query syntax).
    """
    client = _get_client(ctx)
    try:
        response = await client.get(
            f"/projects/{project_id}/workitems",
            params={
                "fields[workitems]": _WI_LIST_FIELDS,
                "query": query,
                "page[size]": page_size,
                "page[number]": page_number,
            },
        )
    except PolarionNotFoundError as exc:
        raise ValueError(
            f"Project '{project_id}' not found. "
            "Use `list_projects` to discover valid project IDs."
        ) from exc
    except PolarionAuthError as exc:
        raise PermissionError(
            "Cannot search work items -- check your POLARION_TOKEN permissions."
        ) from exc
    except PolarionError as exc:
        raise RuntimeError(f"Failed to search work items: {exc.message}") from exc

    data = response.get("data", [])
    items = _parse_work_item_summaries(data)

    return PaginatedResult[WorkItemSummary](
        items=items,
        total_count=_extract_total_count(response),
        page=page_number,
        page_size=page_size,
    )


@mcp.tool()
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
    """Get all linked work items (forward and back links).

    Retrieves both forward (outgoing) and back (incoming) links for a
    work item and merges them into a single result.  This provides
    complete traceability information.

    Link roles include relationships like ``parent``, ``relates_to``,
    ``verifies``, ``depends_on``, etc.  The ``suspect`` flag indicates
    whether the linked item has changed since the link was last reviewed.

    Use ``list_work_items`` or ``search_work_items`` first to discover
    valid work item IDs.

    Args:
        ctx: MCP tool context (injected automatically).
        project_id: Polarion project ID.
        work_item_id: Work Item ID (e.g. 'MCPT-001').

    Returns:
        LinkedWorkItemsList with:
        - ``items``: All linked work items (both directions).
        - ``forward_count``: Number of forward links.
        - ``back_count``: Number of back links.

    Raises:
        ValueError: If the work item or project is not found.
        PermissionError: If the token lacks permissions.
        RuntimeError: On unexpected Polarion API errors.
    """
    client = _get_client(ctx)
    base_path = f"/projects/{project_id}/workitems/{work_item_id}"

    try:
        forward_response = await client.get(
            f"{base_path}/linkedworkitems",
        )
        back_response = await client.get(
            f"{base_path}/backlinkedworkitems",
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
            f"Failed to get links for '{work_item_id}': {exc.message}"
        ) from exc

    forward_items = _parse_linked_items(
        forward_response,
        direction="forward",
    )
    back_items = _parse_linked_items(
        back_response,
        direction="back",
    )

    all_items = forward_items + back_items

    return LinkedWorkItemsList(
        items=all_items,
        forward_count=len(forward_items),
        back_count=len(back_items),
    )


# ---------------------------------------------------------------------------
# Parsing helpers
# ---------------------------------------------------------------------------


def _parse_work_item_summaries(
    data: object,
) -> list[WorkItemSummary]:
    """Parse a JSON:API ``data`` array into ``WorkItemSummary`` models.

    Args:
        data: The ``data`` field from a JSON:API response.

    Returns:
        List of parsed ``WorkItemSummary`` instances.
    """
    items: list[WorkItemSummary] = []
    if not isinstance(data, list):
        return items

    for item in data:
        if not isinstance(item, dict):
            continue
        attrs = item.get("attributes", {})
        if not isinstance(attrs, dict):
            attrs = {}

        # Extract ID from JSON:API id
        # (format: "projectId/WI-001").
        raw_id = _safe_str(item.get("id", ""))
        wi_id = raw_id.split("/", maxsplit=1)[-1] if "/" in raw_id else raw_id

        items.append(
            WorkItemSummary(
                id=wi_id,
                title=_safe_str(attrs.get("title", "")),
                type=_safe_str(attrs.get("type", "")),
                status=_safe_str(attrs.get("status", "")),
            )
        )
    return items


def _parse_linked_items(
    response: dict[str, object],
    *,
    direction: str,
) -> list[LinkedWorkItemSummary]:
    """Parse linked work items from a JSON:API response.

    Args:
        response: Decoded JSON:API response from the linked items
            endpoint.
        direction: Link direction ('forward' or 'back').

    Returns:
        List of parsed ``LinkedWorkItemSummary`` instances.
    """
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

        # Extract the linked work item ID.
        raw_id = _safe_str(item.get("id", ""))
        wi_id = raw_id.split("/", maxsplit=1)[-1] if "/" in raw_id else raw_id

        # Parse role from attributes.
        role = _safe_str(attrs.get("role", ""))

        # Parse suspect flag.
        suspect = bool(attrs.get("suspect", False))

        items.append(
            LinkedWorkItemSummary(
                id=wi_id,
                title=_safe_str(attrs.get("title", "")),
                role=role,
                direction=direction,  # type: ignore[arg-type]
                suspect=suspect,
            )
        )
    return items
