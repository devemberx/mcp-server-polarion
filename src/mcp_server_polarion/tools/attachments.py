"""Document attachment tools — list attachments of a document; fetch one
attachment's content.
"""

from __future__ import annotations

import mimetypes
from typing import Final

from fastmcp import Context
from fastmcp.utilities.types import Image
from pydantic import Field

from mcp_server_polarion.core.exceptions import (
    PolarionAuthError,
    PolarionError,
    PolarionNotFoundError,
    PolarionResponseTooLargeError,
)
from mcp_server_polarion.models import Attachment, PaginatedResult
from mcp_server_polarion.server import mcp
from mcp_server_polarion.tools._shared.fields import DOCUMENT_ATTACHMENT_LIST_FIELDS
from mcp_server_polarion.tools._shared.helpers import encode_path_segment, get_client
from mcp_server_polarion.tools._shared.pagination import DEFAULT_PAGE_SIZE
from mcp_server_polarion.tools._shared.parse import parse_attachments_page

# Mime -> Image format arg. Bitmap formats major LLM hosts render.
_BITMAP_MIME_TO_FORMAT: Final[dict[str, str]] = {
    "image/png": "png",
    "image/jpeg": "jpeg",
    "image/gif": "gif",
    "image/webp": "webp",
}
_SVG_MIME: Final[str] = "image/svg+xml"

# Image tokens scale with pixels (API downscale past 1568px); SVG ride as
# text (~bytes/4 tokens) so its cap differs. 64 KiB ~ 16k tokens — larger
# SVG near-always embed base64 raster, token waste as text.
_MAX_BITMAP_BYTES: Final[int] = 5 * 1024 * 1024
_MAX_SVG_BYTES: Final[int] = 64 * 1024


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


@mcp.tool(
    tags={"read"},
    timeout=60.0,
    annotations={"readOnlyHint": True},
)
async def get_document_attachment_content(
    ctx: Context,
    project_id: str = Field(description="Polarion project ID."),
    space_id: str = Field(description="Space ID ('_default' = default space)."),
    document_name: str = Field(description="Document name within space_id."),
    attachment_id: str = Field(
        description="Attachment id (bare filename token) from"
        " list_document_attachments."
    ),
) -> Image | str:
    """Fetch a document attachment's content for viewing.

    PNG, JPEG, GIF, and WebP return as a viewable image; SVG returns its
    source markup as text. Any other extension is rejected before any
    request. Use list_document_attachments to discover attachment ids,
    file names, and sizes.
    """
    mime, _ = mimetypes.guess_type(attachment_id)
    if mime in _BITMAP_MIME_TO_FORMAT:
        max_bytes = _MAX_BITMAP_BYTES
    elif mime == _SVG_MIME:
        max_bytes = _MAX_SVG_BYTES
    else:
        # Extensions, not mime types -- LLM match against file_name.
        supported = ", ".join([*sorted(_BITMAP_MIME_TO_FORMAT.values()), "svg"])
        raise ValueError(
            f"Attachment '{attachment_id}' has an unsupported or "
            f"unrecognized extension; supported formats: {supported}. "
            "Check file_name via list_document_attachments."
        )

    path = (
        f"/projects/{encode_path_segment(project_id)}"
        f"/spaces/{encode_path_segment(space_id)}"
        f"/documents/{encode_path_segment(document_name)}"
        f"/attachments/{encode_path_segment(attachment_id)}/content"
    )
    try:
        raw = await get_client(ctx).get_bytes(path, max_bytes=max_bytes)
    except PolarionResponseTooLargeError as exc:
        raise ValueError(
            f"Attachment '{attachment_id}' exceeds the {max_bytes} byte "
            "fetch cap. Check length via list_document_attachments before "
            "fetching."
        ) from exc
    except PolarionNotFoundError as exc:
        raise ValueError(
            f"Attachment '{attachment_id}' not found in document "
            f"'{space_id}/{document_name}' (project '{project_id}'). Use "
            "list_document_attachments to discover valid ids."
        ) from exc
    except PolarionAuthError as exc:
        raise PermissionError(
            "Cannot access document attachment content -- check your"
            " POLARION_TOKEN permissions."
        ) from exc
    except PolarionError as exc:
        raise RuntimeError(
            f"Failed to fetch attachment '{attachment_id}': {exc.message}"
        ) from exc

    if mime == _SVG_MIME:
        return raw.decode("utf-8", errors="replace")
    return Image(data=raw, format=_BITMAP_MIME_TO_FORMAT[mime])
