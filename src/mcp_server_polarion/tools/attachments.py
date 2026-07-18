"""Document attachment tools — list attachments of a document."""

from __future__ import annotations

from fastmcp import Context
from pydantic import Field

from mcp_server_polarion.core.exceptions import (
    PolarionAuthError,
    PolarionError,
    PolarionNotFoundError,
)
from mcp_server_polarion.models import Attachment, PaginatedResult
from mcp_server_polarion.server import mcp
from mcp_server_polarion.tools._shared.fields import DOCUMENT_ATTACHMENT_LIST_FIELDS
from mcp_server_polarion.tools._shared.helpers import encode_path_segment, get_client
from mcp_server_polarion.tools._shared.pagination import DEFAULT_PAGE_SIZE
from mcp_server_polarion.tools._shared.parse import parse_attachments_page


@mcp.tool(
    tags={"read"},
    timeout=60.0,
    annotations={"readOnlyHint": True},
)
async def list_document_attachments(  # noqa: PLR0913
    ctx: Context,
    project_id: str = Field(description="Polarion project ID."),
    space_id: str = Field(description="Space ID ('_default' = default space)."),
    document_name: str = Field(description="Document name within space_id."),
    page_size: int = Field(default=DEFAULT_PAGE_SIZE, ge=1, le=100),
    page_number: int = Field(default=1, ge=1),
) -> PaginatedResult[Attachment]:
    """List a document's attachments as a paginated page.

    Document attachments only, not work item attachments. Returned id is the
    exact token a body references as attachment:{id}; Polarion never
    validates that reference, so a body may point at a missing file. Order
    is server-defined and not requestable. Use read_document for body
    context, list_documents for valid space/document ids.
    """
    client = get_client(ctx)
    path = (
        f"/projects/{encode_path_segment(project_id)}"
        f"/spaces/{encode_path_segment(space_id)}"
        f"/documents/{encode_path_segment(document_name)}"
        "/attachments"
    )
    try:
        response = await client.get(
            path,
            params={
                "fields[document_attachments]": DOCUMENT_ATTACHMENT_LIST_FIELDS,
                "include": "author",
                "fields[users]": "name",
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
            "Cannot access document attachments -- check your POLARION_TOKEN"
            " permissions."
        ) from exc
    except PolarionError as exc:
        raise RuntimeError(
            f"Failed to list attachments for '{space_id}/{document_name}': "
            f"{exc.message}"
        ) from exc

    return parse_attachments_page(response, page_number, page_size)
