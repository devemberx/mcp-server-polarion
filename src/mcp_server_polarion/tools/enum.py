"""Enum option tools — list valid option ids via Polarion getAvailableOptions.

One module for both resources: getAvailableOptions apply to work items and
documents alike, mirror the resource-parameterized guard layer.
"""

from __future__ import annotations

from typing import Literal

from fastmcp import Context
from pydantic import Field

from mcp_server_polarion.core.exceptions import (
    PolarionAuthError,
    PolarionError,
    PolarionNotFoundError,
)
from mcp_server_polarion.models import EnumOption, PaginatedResult
from mcp_server_polarion.server import mcp
from mcp_server_polarion.tools._shared.cache import store_cached_field_options
from mcp_server_polarion.tools._shared.helpers import (
    encode_path_segment,
    get_client,
)
from mcp_server_polarion.tools._shared.pagination import (
    DEFAULT_PAGE_SIZE,
    make_page,
)
from mcp_server_polarion.tools._shared.parse import parse_enum_option


async def _list_enum_options(  # noqa: PLR0913
    ctx: Context,
    *,
    resource: Literal["workitems", "documents"],
    project_id: str,
    field_id: str,
    type_id: str,
    type_label: str,
    page_size: int,
    page_number: int,
) -> PaginatedResult[EnumOption]:
    client = get_client(ctx)
    path = (
        f"/projects/{encode_path_segment(project_id)}"
        f"/{resource}/fields/{encode_path_segment(field_id)}"
        "/actions/getAvailableOptions"
    )
    params: dict[str, str | int] = {
        "type": type_id,
        "page[size]": page_size,
        "page[number]": page_number,
    }
    try:
        response = await client.get(path, params=params)
    except PolarionNotFoundError as exc:
        # Same fact write guards cache on own 404 probe -- store so later
        # writes skip one request.
        store_cached_field_options(
            project_id, resource, field_id, type_id, {}, not_found=True
        )
        raise ValueError(
            f"No enum options for field '{field_id}' on {type_label} type "
            f"'{type_id}' in project '{project_id}' -- field unknown or not "
            f"enum-typed."
        ) from exc
    except PolarionAuthError as exc:
        raise PermissionError(
            f"Cannot list {type_label} enum options"
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
                items.append(parse_enum_option(entry))

    page = make_page(items, response, page_number, page_size)
    # Complete option set only: partial page prime = valid ids rejected until
    # expiry. Never invalidate here -- discovery typically run right before
    # write, so dropping entry cost that write one fetch.
    if page_number == 1 and not page.has_more:
        store_cached_field_options(
            project_id,
            resource,
            field_id,
            type_id,
            {option.id: option.name for option in items},
        )
    return page


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
            "e.g. 'status', 'type', 'severity', 'priority', or a custom field id."
        ),
    ),
    work_item_type: str = Field(
        description="e.g. 'task', 'requirement'; '~' = type-agnostic.",
    ),
    page_size: int = Field(default=DEFAULT_PAGE_SIZE, ge=1, le=100),
    page_number: int = Field(default=1, ge=1),
) -> PaginatedResult[EnumOption]:
    """List valid enum option ids for a work item field of a given type.

    Call this before create_work_items / update_work_items — invalid enum ids
    are rejected on write. An unknown work_item_type silently falls back to
    '~', so verify the type id first.
    """
    return await _list_enum_options(
        ctx,
        resource="workitems",
        project_id=project_id,
        field_id=field_id,
        type_id=work_item_type,
        type_label="work item",
        page_size=page_size,
        page_number=page_number,
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
        description="e.g. 'status', 'type', or a custom field id.",
    ),
    document_type: str = Field(
        description="e.g. 'systemReqSpecification'; '~' = type-agnostic.",
    ),
    page_size: int = Field(default=DEFAULT_PAGE_SIZE, ge=1, le=100),
    page_number: int = Field(default=1, ge=1),
) -> PaginatedResult[EnumOption]:
    """List valid enum option ids for a document field of a given type.

    Call this before create_document / update_document — invalid enum ids are
    rejected on write. An unknown document_type silently falls back to '~',
    so verify the type id first.
    """
    return await _list_enum_options(
        ctx,
        resource="documents",
        project_id=project_id,
        field_id=field_id,
        type_id=document_type,
        type_label="document",
        page_size=page_size,
        page_number=page_number,
    )
