"""Project query tools."""

from __future__ import annotations

import logging

from fastmcp import Context
from pydantic import Field

from mcp_server_polarion.core.exceptions import (
    PolarionAuthError,
    PolarionError,
)
from mcp_server_polarion.models import (
    PaginatedResult,
    ProjectSummary,
)
from mcp_server_polarion.server import mcp
from mcp_server_polarion.tools._shared.helpers import (
    DEFAULT_PAGE_SIZE,
    compute_has_more,
    extract_total_count,
    get_client,
    safe_str,
)

logger = logging.getLogger("mcp_server_polarion.tools.projects")


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
