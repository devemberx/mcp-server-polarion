"""Attachment tools — list attachments of a document or work item; fetch one
document attachment's content.
"""

from __future__ import annotations

from pathlib import PurePosixPath
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
from mcp_server_polarion.tools._shared.fields import ATTACHMENT_LIST_FIELDS
from mcp_server_polarion.tools._shared.helpers import encode_path_segment, get_client
from mcp_server_polarion.tools._shared.pagination import DEFAULT_PAGE_SIZE
from mcp_server_polarion.tools._shared.parse import parse_attachments_page

# Extension -> Image format arg; bitmap formats major LLM hosts render.
# Static, not mimetypes.guess_type: guess_type read system mime files +
# registry (polluted env remap .png) and strip .svgz to image/svg+xml.
_BITMAP_EXTENSION_TO_FORMAT: Final[dict[str, str]] = {
    ".png": "png",
    ".jpg": "jpeg",
    ".jpeg": "jpeg",
    ".jpe": "jpeg",
    ".gif": "gif",
    ".webp": "webp",
}
_SVG_EXTENSION: Final[str] = ".svg"

# Bitmap tokens scale with pixels (API downscale past 1568px). SVG ride
# as text: 64 KiB ~ 16k tokens; larger SVG near-always base64 raster.
_MAX_BITMAP_BYTES: Final[int] = 5 * 1024 * 1024
_MAX_SVG_BYTES: Final[int] = 64 * 1024

# Magic decide served format (extension only route pre-fetch) — extension
# lie shipped as image = unrecoverable vision API 400.
_BITMAP_MAGIC_TO_FORMAT: Final[tuple[tuple[bytes, str], ...]] = (
    (b"\x89PNG\r\n\x1a\n", "png"),
    (b"\xff\xd8\xff", "jpeg"),
    (b"GIF87a", "gif"),
    (b"GIF89a", "gif"),
)
# Loose on purpose — goal = block binary mislabeled .svg, not validate spec.
_SVG_PREFIXES: Final[tuple[str, ...]] = ("<?xml", "<svg", "<!--", "<!doctype")


def _sniff_bitmap_format(raw: bytes) -> str | None:
    """Image format from magic bytes; ``None`` = no supported signature."""
    for magic, image_format in _BITMAP_MAGIC_TO_FORMAT:
        if raw.startswith(magic):
            return image_format
    if raw[:4] == b"RIFF" and raw[8:12] == b"WEBP":
        return "webp"
    return None


def _looks_like_svg(text: str) -> bool:
    """Markup prefix after BOM/whitespace strip — reject binary payloads."""
    head = text.lstrip("\ufeff \t\r\n")[:10].lower()
    return head.startswith(_SVG_PREFIXES)


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
                "fields[document_attachments]": ATTACHMENT_LIST_FIELDS,
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
    extension = PurePosixPath(attachment_id).suffix.lower()
    is_svg = extension == _SVG_EXTENSION
    if extension in _BITMAP_EXTENSION_TO_FORMAT:
        max_bytes = _MAX_BITMAP_BYTES
    elif is_svg:
        max_bytes = _MAX_SVG_BYTES
    else:
        # Format names double as extensions -- LLM match against file_name.
        supported = ", ".join(
            [*sorted(set(_BITMAP_EXTENSION_TO_FORMAT.values())), "svg"]
        )
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

    if is_svg:
        text = raw.decode("utf-8", errors="replace")
        if not _looks_like_svg(text):
            raise ValueError(
                f"Attachment '{attachment_id}' content is not SVG markup — "
                "its file_name extension may be wrong. Verify via "
                "list_document_attachments."
            )
        return text
    sniffed_format = _sniff_bitmap_format(raw)
    if sniffed_format is None:
        supported = ", ".join(sorted(set(_BITMAP_EXTENSION_TO_FORMAT.values())))
        raise ValueError(
            f"Attachment '{attachment_id}' content matches no supported "
            f"image format ({supported}) — its file_name extension may be "
            "wrong. Verify via list_document_attachments."
        )
    return Image(data=raw, format=sniffed_format)


@mcp.tool(
    tags={"read"},
    timeout=60.0,
    annotations={"readOnlyHint": True},
)
async def list_work_item_attachments(
    ctx: Context,
    project_id: str = Field(description="Polarion project ID."),
    work_item_id: str = Field(description="Work item ID within project_id."),
    page_size: int = Field(default=DEFAULT_PAGE_SIZE, ge=1, le=100),
    page_number: int = Field(default=1, ge=1),
) -> PaginatedResult[Attachment]:
    """List a work item's attachments as a paginated page.

    Work item attachments only -- use list_document_attachments for
    documents. Returned id is the exact token a body references as
    workitemimg:{id}; Polarion never validates that reference, so a body may
    point at a missing file. Order is server-defined and not requestable.
    Use list_work_items to discover valid ids.
    """
    client = get_client(ctx)
    path = (
        f"/projects/{encode_path_segment(project_id)}"
        f"/workitems/{encode_path_segment(work_item_id)}"
        "/attachments"
    )
    try:
        response = await client.get(
            path,
            params={
                "fields[workitem_attachments]": ATTACHMENT_LIST_FIELDS,
                "include": "author",
                "fields[users]": "name",
                "page[size]": page_size,
                "page[number]": page_number,
            },
        )
    except PolarionNotFoundError as exc:
        raise ValueError(
            f"Work item '{work_item_id}' not found in project '{project_id}'. "
            "Use `list_work_items` to discover valid IDs."
        ) from exc
    except PolarionAuthError as exc:
        raise PermissionError(
            "Cannot access work item attachments -- check your POLARION_TOKEN"
            " permissions."
        ) from exc
    except PolarionError as exc:
        raise RuntimeError(
            f"Failed to list attachments for '{work_item_id}': {exc.message}"
        ) from exc

    return parse_attachments_page(response, page_number, page_size)
