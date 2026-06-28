"""Test run tools — list and search test runs in a project."""

from __future__ import annotations

from fastmcp import Context
from pydantic import Field

from mcp_server_polarion.core.exceptions import (
    PolarionAuthError,
    PolarionError,
    PolarionNotFoundError,
)
from mcp_server_polarion.models import PaginatedResult, TestRunSummary
from mcp_server_polarion.server import mcp
from mcp_server_polarion.tools._shared.fields import TEST_RUN_LIST_FIELDS
from mcp_server_polarion.tools._shared.helpers import (
    encode_path_segment,
    get_client,
)
from mcp_server_polarion.tools._shared.pagination import (
    DEFAULT_PAGE_SIZE,
    make_page,
)
from mcp_server_polarion.tools._shared.parse import parse_test_run_summaries


@mcp.tool(
    tags={"read"},
    timeout=60.0,
    annotations={"readOnlyHint": True},
)
async def list_test_runs(  # noqa: PLR0913
    ctx: Context,
    project_id: str = Field(description="Polarion project ID."),
    query: str | None = Field(
        default=None,
        description=(
            "Optional Lucene filter (e.g. 'status:open', 'author.id:devemberx') "
            "OR a 'SQL:(...)' prefix for native SQL."
        ),
    ),
    templates: bool = Field(
        default=False,
        description="List template blueprints instead of actual run instances.",
    ),
    page_size: int = Field(default=DEFAULT_PAGE_SIZE, ge=1, le=100),
    page_number: int = Field(default=1, ge=1),
) -> PaginatedResult[TestRunSummary]:
    """List / search test runs in a project.

    Returns actual run instances by default; set templates=True for the reusable
    template blueprints instead. Filter with a Lucene query (status:open,
    type:manual, author.id:<userid>) or omit for all.
    """
    client = get_client(ctx)
    params: dict[str, str | int] = {
        "fields[testruns]": TEST_RUN_LIST_FIELDS,
        "include": "author",
        "fields[users]": "name",
        "page[size]": page_size,
        "page[number]": page_number,
    }
    if query is not None:
        params["query"] = query
    if templates:
        params["templates"] = "true"
    try:
        response = await client.get(
            f"/projects/{encode_path_segment(project_id)}/testruns",
            params=params,
        )
    except PolarionNotFoundError as exc:
        raise ValueError(
            f"Project '{project_id}' not found. "
            "Use `list_projects` to discover valid project IDs."
        ) from exc
    except PolarionAuthError as exc:
        raise PermissionError(
            "Cannot list test runs -- check your POLARION_TOKEN permissions."
        ) from exc
    except PolarionError as exc:
        raise RuntimeError(f"Failed to list test runs: {exc.message}") from exc

    items = parse_test_run_summaries(response)

    return make_page(items, response, page_number, page_size)
