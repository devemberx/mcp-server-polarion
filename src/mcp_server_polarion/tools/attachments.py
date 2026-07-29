"""Attachment tools — list attachments of a document or work item; fetch one
attachment's content for viewing; upload document or work item attachments.
"""

from __future__ import annotations

import json
from collections import Counter
from collections.abc import Sequence
from pathlib import Path, PurePosixPath
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
from mcp_server_polarion.models import (
    Attachment,
    AttachmentsCreateResult,
    DocumentAttachmentSpec,
    JsonValue,
    PaginatedResult,
    TestRecordAttachmentSpec,
    WorkItemAttachmentSpec,
)
from mcp_server_polarion.server import mcp
from mcp_server_polarion.tools._shared.fields import ATTACHMENT_LIST_FIELDS
from mcp_server_polarion.tools._shared.helpers import (
    encode_path_segment,
    get_client,
    split_test_case_id,
    test_record_path,
)
from mcp_server_polarion.tools._shared.pagination import DEFAULT_PAGE_SIZE
from mcp_server_polarion.tools._shared.parse import (
    extract_created_short_ids,
    parse_attachments_page,
)

# Whole batch; fail closed before any request.
_MAX_TOTAL_UPLOAD_BYTES: Final[int] = 25 * 1024 * 1024
_MAX_ATTACHMENTS_PER_CALL: Final[int] = 10

_HTTP_CONFLICT: Final[int] = 409
_HTTP_PAYLOAD_TOO_LARGE: Final[int] = 413

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


async def _fetch_attachment_content(  # noqa: PLR0913
    ctx: Context,
    path: str,
    attachment_id: str,
    list_tool: str,
    not_found_location: str,
    resource_noun: str,
) -> Image | str:
    """Shared gate+fetch+sniff body for both attachment content tools.

    ``not_found_location`` = preposition + address for 404 message;
    ``list_tool`` = discovery tool named in every guidance error.
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
            f"Check file_name via {list_tool}."
        )

    try:
        raw = await get_client(ctx).get_bytes(path, max_bytes=max_bytes)
    except PolarionResponseTooLargeError as exc:
        raise ValueError(
            f"Attachment '{attachment_id}' exceeds the {max_bytes} byte "
            f"fetch cap. Check length via {list_tool} before fetching."
        ) from exc
    except PolarionNotFoundError as exc:
        raise ValueError(
            f"Attachment '{attachment_id}' not found {not_found_location}. "
            f"Use {list_tool} to discover valid ids."
        ) from exc
    except PolarionAuthError as exc:
        raise PermissionError(
            f"Cannot access {resource_noun} attachment content -- check your"
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
                f"Attachment '{attachment_id}' content is not SVG markup \u2014 "
                "its file_name extension may be wrong. Verify via "
                f"{list_tool}."
            )
        return text
    sniffed_format = _sniff_bitmap_format(raw)
    if sniffed_format is None:
        supported = ", ".join(sorted(set(_BITMAP_EXTENSION_TO_FORMAT.values())))
        raise ValueError(
            f"Attachment '{attachment_id}' content matches no supported "
            f"image format ({supported}) \u2014 its file_name extension may be "
            f"wrong. Verify via {list_tool}."
        )
    return Image(data=raw, format=sniffed_format)


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
    request. Use get_work_item_attachment_content for work item
    attachments. Use list_document_attachments to discover attachment ids,
    file names, and sizes.
    """
    return await _fetch_attachment_content(
        ctx,
        path=(
            f"/projects/{encode_path_segment(project_id)}"
            f"/spaces/{encode_path_segment(space_id)}"
            f"/documents/{encode_path_segment(document_name)}"
            f"/attachments/{encode_path_segment(attachment_id)}/content"
        ),
        attachment_id=attachment_id,
        list_tool="list_document_attachments",
        not_found_location=(
            f"in document '{space_id}/{document_name}' (project '{project_id}')"
        ),
        resource_noun="document",
    )


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


@mcp.tool(
    tags={"read"},
    timeout=60.0,
    annotations={"readOnlyHint": True},
)
async def list_test_record_attachments(  # noqa: PLR0913
    ctx: Context,
    project_id: str = Field(description="Polarion project ID."),
    test_run_id: str = Field(description="Test run ID (e.g. 'TR-2026-01')."),
    test_case_id: str = Field(
        description=(
            "Full test case work item ID 'project/WI-id' as returned by "
            "list_test_records."
        )
    ),
    iteration: int = Field(
        default=0, ge=0, description="Record iteration number (0-based)."
    ),
    page_size: int = Field(default=DEFAULT_PAGE_SIZE, ge=1, le=100),
    page_number: int = Field(default=1, ge=1),
) -> PaginatedResult[Attachment]:
    """List a test record's attachments as a paginated page.

    Test record attachments only -- use list_work_item_attachments for work
    item files, list_document_attachments for document files. test_case_id
    is the full 'project/WI-id' form from list_test_records, not the short
    work item ID. Order is server-defined and not requestable. An empty
    result means the record has no attachments; verify the
    run/test-case/iteration coordinates via list_test_records if unsure.
    """
    client = get_client(ctx)
    path = (
        test_record_path(project_id, test_run_id, test_case_id, iteration)
        + "/attachments"
    )
    try:
        response = await client.get(
            path,
            params={
                "fields[testrecord_attachments]": ATTACHMENT_LIST_FIELDS,
                "include": "author",
                "fields[users]": "name",
                "page[size]": page_size,
                "page[number]": page_number,
            },
        )
    except PolarionNotFoundError as exc:
        raise ValueError(
            f"Test record for case '{test_case_id}' iteration {iteration} not "
            f"found in test run '{test_run_id}' (project '{project_id}'). "
            "Use `list_test_records` to discover valid coordinates."
        ) from exc
    except PolarionAuthError as exc:
        raise PermissionError(
            "Cannot access test record attachments -- check your"
            " POLARION_TOKEN permissions."
        ) from exc
    except PolarionError as exc:
        raise RuntimeError(
            f"Failed to list attachments for test record: {exc.message}"
        ) from exc

    return parse_attachments_page(response, page_number, page_size)


@mcp.tool(
    tags={"read"},
    timeout=60.0,
    annotations={"readOnlyHint": True},
)
async def get_work_item_attachment_content(
    ctx: Context,
    project_id: str = Field(description="Polarion project ID."),
    work_item_id: str = Field(description="Work item ID within project_id."),
    attachment_id: str = Field(
        description="Attachment id (bare filename token) from"
        " list_work_item_attachments."
    ),
) -> Image | str:
    """Fetch a work item attachment's content for viewing.

    PNG, JPEG, GIF, and WebP return as a viewable image; SVG returns its
    source markup as text. Any other extension is rejected before any
    request. Use get_document_attachment_content for document attachments.
    Use list_work_item_attachments to discover attachment ids, file names,
    and sizes.
    """
    return await _fetch_attachment_content(
        ctx,
        path=(
            f"/projects/{encode_path_segment(project_id)}"
            f"/workitems/{encode_path_segment(work_item_id)}"
            f"/attachments/{encode_path_segment(attachment_id)}/content"
        ),
        attachment_id=attachment_id,
        list_tool="list_work_item_attachments",
        not_found_location=(f"on work item '{work_item_id}' (project '{project_id}')"),
        resource_noun="work item",
    )


@mcp.tool(
    tags={"read"},
    timeout=60.0,
    annotations={"readOnlyHint": True},
)
async def get_test_record_attachment_content(  # noqa: PLR0913
    ctx: Context,
    project_id: str = Field(description="Polarion project ID."),
    test_run_id: str = Field(description="Test run ID (e.g. 'TR-2026-01')."),
    test_case_id: str = Field(
        description="Full test case work item ID 'project/WI-id' as returned"
        " by list_test_records."
    ),
    attachment_id: str = Field(
        description="Attachment id ({testCaseId}_{fileName} token) from"
        " list_test_record_attachments."
    ),
    iteration: int = Field(
        default=0, ge=0, description="Record iteration number (0-based)."
    ),
) -> Image | str:
    """Fetch a test record attachment's content for viewing.

    PNG, JPEG, GIF, and WebP return as a viewable image; SVG returns its
    source markup as text. Any other extension is rejected before any
    request. Use get_document_attachment_content or
    get_work_item_attachment_content for the other domains. Use
    list_test_record_attachments to discover attachment ids, file names,
    and sizes.
    """
    base = test_record_path(project_id, test_run_id, test_case_id, iteration)

    return await _fetch_attachment_content(
        ctx,
        path=f"{base}/attachments/{encode_path_segment(attachment_id)}/content",
        attachment_id=attachment_id,
        list_tool="list_test_record_attachments",
        not_found_location=(
            f"on test record for case '{test_case_id}' iteration {iteration}"
            f" (test run '{test_run_id}', project '{project_id}')"
        ),
        resource_noun="test record",
    )


def _effective_file_name(
    spec: DocumentAttachmentSpec | WorkItemAttachmentSpec | TestRecordAttachmentSpec,
) -> str:
    """Effective fileName attribute across domains -- doc spec also reuse it
    as the attachment id (dup-checked by caller), work item spec get a
    server counter prefix, test record spec get {testCaseId}_{fileName}
    server rewrite instead.
    """
    return spec.file_name if spec.file_name else Path(spec.file_path).name


def _build_attachments_payload(
    specs: Sequence[
        DocumentAttachmentSpec | WorkItemAttachmentSpec | TestRecordAttachmentSpec
    ],
    resource_type: str,
) -> dict[str, JsonValue]:
    """POST .../attachments body shared by doc/WI/testrecord resource types;
    title skip when unset (skip-None rule). Pure -- no disk access.
    """
    items: list[JsonValue] = []
    for spec in specs:
        attributes: dict[str, JsonValue] = {"fileName": _effective_file_name(spec)}
        if spec.title:
            attributes["title"] = spec.title
        items.append({"type": resource_type, "attributes": attributes})
    return {"data": items}


def _build_document_attachments_payload(
    specs: Sequence[DocumentAttachmentSpec],
) -> dict[str, JsonValue]:
    """POST .../documents/{d}/attachments body."""
    return _build_attachments_payload(specs, "document_attachments")


def _build_work_item_attachments_payload(
    specs: Sequence[WorkItemAttachmentSpec],
) -> dict[str, JsonValue]:
    """POST .../workitems/{wi}/attachments body."""
    return _build_attachments_payload(specs, "workitem_attachments")


def _build_test_record_attachments_payload(
    specs: Sequence[TestRecordAttachmentSpec],
) -> dict[str, JsonValue]:
    """POST .../testruns/{r}/testrecords/{tcProj}/{tcId}/{iter}/attachments body."""
    return _build_attachments_payload(specs, "testrecord_attachments")


def _reject_separator_file_names(file_names: Sequence[str]) -> None:
    """fileName become attachment id; '/' or '\\' inside shift id path
    segments (server behavior unverified) -- fail closed. Windows file_path
    on POSIX land here too: basename = unsplit whole string.
    """
    invalid = sorted({name for name in file_names if "/" in name or "\\" in name})
    if invalid:
        raise ValueError(
            f"file_name(s) {invalid} contain a path separator -- file_name"
            " becomes the attachment id and must be a bare file name;"
            " directories belong in file_path (set an explicit file_name"
            " override for non-POSIX paths)."
        )


def _reject_duplicate_file_names(file_names: Sequence[str]) -> None:
    """In-call collision has no merge semantics (unlike link-batch dup) -- reject."""
    duplicates = sorted(
        name for name, count in Counter(file_names).items() if count > 1
    )
    if duplicates:
        raise ValueError(
            f"Duplicate file_name(s) {duplicates} in one call -- file_name"
            " becomes the attachment id and must be unique; rename one of"
            " the conflicting files (or set an explicit file_name override)."
        )


def _read_attachment_files(
    specs: Sequence[
        DocumentAttachmentSpec | WorkItemAttachmentSpec | TestRecordAttachmentSpec
    ],
) -> list[tuple[str, bytes]]:
    """Read spec files, order preserved; every reject raise ValueError before
    any Polarion call. Stat cap check pre-read keep oversized file out of
    memory. Dup-name reject NOT here -- doc-only semantics, caller applies
    _reject_duplicate_file_names itself before calling this.
    """
    file_names = [_effective_file_name(spec) for spec in specs]
    _reject_separator_file_names(file_names)

    sizes: list[tuple[str, int]] = []
    for spec, file_name in zip(specs, file_names, strict=True):
        path = Path(spec.file_path)
        if path.is_dir():
            raise ValueError(
                f"'{spec.file_path}' is a directory -- provide a path to a"
                " readable file."
            )
        if not path.is_file():
            raise ValueError(
                f"'{spec.file_path}' does not exist or is not a readable file"
                " -- provide an absolute path to a readable file."
            )

        size = path.stat().st_size
        if size > _MAX_TOTAL_UPLOAD_BYTES:
            raise ValueError(
                f"'{spec.file_path}' is {size} bytes, over the"
                f" {_MAX_TOTAL_UPLOAD_BYTES} byte per-call cap -- compress it"
                " or upload it via the Polarion portal; a single file cannot"
                " be split across calls."
            )
        sizes.append((file_name, size))

    total = sum(size for _, size in sizes)
    if total > _MAX_TOTAL_UPLOAD_BYTES:
        listing = ", ".join(f"{name} ({size} bytes)" for name, size in sizes)
        raise ValueError(
            f"Total upload size {total} bytes exceeds the"
            f" {_MAX_TOTAL_UPLOAD_BYTES} byte per-call cap ({listing}) --"
            " split the files across multiple calls."
        )

    results: list[tuple[str, bytes]] = []
    for spec, file_name in zip(specs, file_names, strict=True):
        try:
            content = Path(spec.file_path).read_bytes()
        except OSError as exc:
            raise ValueError(f"Cannot read '{spec.file_path}': {exc}.") from exc
        results.append((file_name, content))

    # Stat-time cap alone = TOCTOU: file can grow between stat + read.
    total_read = sum(len(content) for _, content in results)
    if total_read > _MAX_TOTAL_UPLOAD_BYTES:
        raise ValueError(
            f"Total bytes read ({total_read}) exceeds the"
            f" {_MAX_TOTAL_UPLOAD_BYTES} byte per-call cap -- a file grew"
            " between size check and read; retry, or split the files across"
            " multiple calls."
        )
    return results


@mcp.tool(
    tags={"write"},
    timeout=60.0,
    annotations={
        # Non-idempotent: server reject retry dup. Open world: files from local disk.
        "readOnlyHint": False,
        "destructiveHint": False,
        "idempotentHint": False,
        "openWorldHint": True,
    },
)
async def create_document_attachments(  # noqa: PLR0913
    ctx: Context,
    project_id: str = Field(min_length=1, description="Polarion project ID."),
    space_id: str = Field(
        min_length=1,
        description="Space ID ('_default' = default space).",
    ),
    document_name: str = Field(
        min_length=1,
        description="Document name within space_id.",
    ),
    attachments: list[DocumentAttachmentSpec] = Field(  # noqa: B008
        min_length=1,
        max_length=_MAX_ATTACHMENTS_PER_CALL,
        description="Files to upload in one request.",
    ),
    dry_run: bool = Field(
        default=False,
        description="Preview payload without calling Polarion.",
    ),
) -> AttachmentsCreateResult:
    """Upload 1-10 local files as document attachments in one request.

    file_path is read from local disk by the server process -- use absolute
    paths to readable files. file_name (default: file_path's basename)
    becomes the attachment id; reference it in a document body as
    attachment:{id} for update_document. Total upload size per call is
    capped at 25 MiB: compress or use the Polarion portal for one oversized
    file, split oversized batches across calls. Pure create -- nothing is
    replaced. Uploads cannot be deleted through this API, so verify
    file_path and file_name first. A file_name colliding with another item
    in the same call, or with an existing attachment on the document,
    rejects the whole batch -- check list_document_attachments first or
    pick a new file_name. NOT idempotent -- retrying a success is rejected
    as a duplicate, not silently merged.
    """
    _reject_duplicate_file_names([_effective_file_name(spec) for spec in attachments])
    files = _read_attachment_files(attachments)
    payload = _build_document_attachments_payload(attachments)

    if dry_run:
        preview: dict[str, JsonValue] = {
            **payload,
            "files": [
                {"file_name": file_name, "size_bytes": len(content)}
                for file_name, content in files
            ],
        }
        return AttachmentsCreateResult(
            created=False,
            dry_run=True,
            attachment_ids=[],
            payload_preview=preview,
        )

    client = get_client(ctx)
    path = (
        f"/projects/{encode_path_segment(project_id)}"
        f"/spaces/{encode_path_segment(space_id)}"
        f"/documents/{encode_path_segment(document_name)}"
        "/attachments"
    )
    parts = [
        ("files", (file_name, content, "application/octet-stream"))
        for file_name, content in files
    ]
    try:
        response = await client.post_multipart(
            path,
            data={"resource": json.dumps(payload)},
            files=parts,
        )
    except PolarionAuthError as exc:
        raise PermissionError(
            "Cannot create document attachments -- check your POLARION_TOKEN"
            " permissions."
        ) from exc
    except PolarionNotFoundError as exc:
        raise ValueError(
            f"Document '{document_name}' (space '{space_id}',"
            f" project '{project_id}') not found."
            " Use `list_documents` to discover valid IDs."
        ) from exc
    except PolarionError as exc:
        if exc.status_code == _HTTP_CONFLICT:
            raise ValueError(
                "One or more file_name values already exist as attachments"
                f" on '{document_name}' -- the whole batch was rejected."
                " Check `list_document_attachments` first or pick new"
                " file_name values."
            ) from exc
        if exc.status_code == _HTTP_PAYLOAD_TOO_LARGE:
            raise RuntimeError(
                "Server upload cap is below the 25 MiB client cap --"
                " reduce file size or split the batch across calls."
            ) from exc
        raise RuntimeError(
            f"Failed to create document attachments: {exc.message}"
        ) from exc

    attachment_ids = extract_created_short_ids(
        response,
        expected_count=len(attachments),
        list_tool="list_document_attachments",
    )

    return AttachmentsCreateResult(
        created=True,
        dry_run=False,
        attachment_ids=attachment_ids,
        payload_preview=None,
    )


@mcp.tool(
    tags={"write"},
    timeout=60.0,
    annotations={
        # Non-idempotent: retry silently duplicate, server never conflict on
        # dup fileName (unlike document sibling, which 409s).
        "readOnlyHint": False,
        "destructiveHint": False,
        "idempotentHint": False,
        "openWorldHint": True,
    },
)
async def create_work_item_attachments(
    ctx: Context,
    project_id: str = Field(min_length=1, description="Polarion project ID."),
    work_item_id: str = Field(
        min_length=1,
        description="Work item ID within project_id.",
    ),
    attachments: list[WorkItemAttachmentSpec] = Field(  # noqa: B008
        min_length=1,
        max_length=_MAX_ATTACHMENTS_PER_CALL,
        description="Files to upload in one request.",
    ),
    dry_run: bool = Field(
        default=False,
        description="Preview payload without calling Polarion.",
    ),
) -> AttachmentsCreateResult:
    """Upload 1-10 local files as work item attachments in one request.

    For document attachments use create_document_attachments instead.
    file_path is read from local disk by the server process -- use absolute
    paths to readable files. Total upload size per call is capped at 25 MiB:
    compress or use the Polarion portal for one oversized file, split
    oversized batches across calls. attachment_ids in the result are
    server-assigned counter-prefixed ids (e.g. 3-diagram.png) -- not
    predictable from file_name -- and double as the workitemimg:{id}
    reference tokens for the work item description body. Duplicate
    file_name values are allowed, both within one call and against existing
    attachments: each upload creates a new attachment, never a conflict.
    Heading-type work items accept uploads, but the portal hides their
    Attachments section -- reachable only through the API.
    NOT idempotent -- retrying a success
    silently creates a duplicate; after an ambiguous failure verify with
    list_work_item_attachments before retrying.
    """
    files = _read_attachment_files(attachments)
    payload = _build_work_item_attachments_payload(attachments)

    if dry_run:
        preview: dict[str, JsonValue] = {
            **payload,
            "files": [
                {"file_name": file_name, "size_bytes": len(content)}
                for file_name, content in files
            ],
        }
        return AttachmentsCreateResult(
            created=False,
            dry_run=True,
            attachment_ids=[],
            payload_preview=preview,
        )

    client = get_client(ctx)
    path = (
        f"/projects/{encode_path_segment(project_id)}"
        f"/workitems/{encode_path_segment(work_item_id)}"
        "/attachments"
    )
    parts = [
        ("files", (file_name, content, "application/octet-stream"))
        for file_name, content in files
    ]
    try:
        response = await client.post_multipart(
            path,
            data={"resource": json.dumps(payload)},
            files=parts,
        )
    except PolarionAuthError as exc:
        raise PermissionError(
            "Cannot create work item attachments -- check your POLARION_TOKEN"
            " permissions."
        ) from exc
    except PolarionNotFoundError as exc:
        raise ValueError(
            f"Work item '{work_item_id}' not found in project '{project_id}'. "
            "Use `list_work_items` to discover valid IDs."
        ) from exc
    except PolarionError as exc:
        if exc.status_code == _HTTP_PAYLOAD_TOO_LARGE:
            raise RuntimeError(
                "Server upload cap is below the 25 MiB client cap --"
                " reduce file size or split the batch across calls."
            ) from exc
        raise RuntimeError(
            f"Failed to create work item attachments: {exc.message}"
        ) from exc

    attachment_ids = extract_created_short_ids(
        response,
        expected_count=len(attachments),
        list_tool="list_work_item_attachments",
    )

    return AttachmentsCreateResult(
        created=True,
        dry_run=False,
        attachment_ids=attachment_ids,
        payload_preview=None,
    )


@mcp.tool(
    tags={"write"},
    timeout=60.0,
    annotations={
        # Non-idempotent: server reject retry dup. Open world: files from local disk.
        "readOnlyHint": False,
        "destructiveHint": False,
        "idempotentHint": False,
        "openWorldHint": True,
    },
)
async def create_test_record_attachments(  # noqa: PLR0913
    ctx: Context,
    project_id: str = Field(min_length=1, description="Polarion project ID."),
    test_run_id: str = Field(
        min_length=1, description="Test run ID (e.g. 'TR-2026-01')."
    ),
    test_case_id: str = Field(
        description=(
            "Full test case work item ID 'project/WI-id' as returned by "
            "list_test_records."
        )
    ),
    attachments: list[TestRecordAttachmentSpec] = Field(  # noqa: B008
        min_length=1,
        max_length=_MAX_ATTACHMENTS_PER_CALL,
        description="Files to upload in one request.",
    ),
    iteration: int = Field(
        default=0, ge=0, description="Record iteration number (0-based)."
    ),
    dry_run: bool = Field(
        default=False,
        description="Preview payload without calling Polarion.",
    ),
) -> AttachmentsCreateResult:
    """Upload 1-10 local files as test record attachments in one request.

    For document attachments use create_document_attachments, for work item
    attachments use create_work_item_attachments instead. Record
    coordinates (project_id, test_run_id, test_case_id, iteration) match
    get_test_record -- verify via list_test_records first. file_path is
    read from local disk by the server process -- use absolute paths to
    readable files. Total upload size per call is capped at 25 MiB:
    compress or use the Polarion portal for one oversized file, split
    oversized batches across calls. attachment_ids in the result are
    server-assigned ({test_case_id}_{file_name}) and differ from the input
    file_name. A file_name colliding with another item in the same call, or
    with an existing attachment on the record, rejects the whole batch --
    check list_test_record_attachments first or pick a new file_name. NOT
    idempotent -- retrying a success is rejected as a duplicate, not
    silently merged.
    """
    # Fail fast on short-form test_case_id before file reads + dry_run.
    split_test_case_id(test_case_id)

    _reject_duplicate_file_names([_effective_file_name(spec) for spec in attachments])
    files = _read_attachment_files(attachments)
    payload = _build_test_record_attachments_payload(attachments)

    if dry_run:
        preview: dict[str, JsonValue] = {
            **payload,
            "files": [
                {"file_name": file_name, "size_bytes": len(content)}
                for file_name, content in files
            ],
        }
        return AttachmentsCreateResult(
            created=False,
            dry_run=True,
            attachment_ids=[],
            payload_preview=preview,
        )

    client = get_client(ctx)
    path = (
        test_record_path(project_id, test_run_id, test_case_id, iteration)
        + "/attachments"
    )
    parts = [
        ("files", (file_name, content, "application/octet-stream"))
        for file_name, content in files
    ]
    try:
        response = await client.post_multipart(
            path,
            data={"resource": json.dumps(payload)},
            files=parts,
        )
    except PolarionAuthError as exc:
        raise PermissionError(
            "Cannot create test record attachments -- check your"
            " POLARION_TOKEN permissions."
        ) from exc
    except PolarionNotFoundError as exc:
        raise ValueError(
            f"Test record for case '{test_case_id}' iteration {iteration} not "
            f"found in test run '{test_run_id}' (project '{project_id}'). "
            "Use `list_test_records` to discover valid coordinates."
        ) from exc
    except PolarionError as exc:
        if exc.status_code == _HTTP_CONFLICT:
            raise ValueError(
                "One or more file_name values already exist as attachments"
                " on this test record (within this call or a prior one) --"
                " the whole batch was rejected. Pick new file_name values."
            ) from exc
        if exc.status_code == _HTTP_PAYLOAD_TOO_LARGE:
            raise RuntimeError(
                "Server upload cap is below the 25 MiB client cap --"
                " reduce file size or split the batch across calls."
            ) from exc
        raise RuntimeError(
            f"Failed to create test record attachments: {exc.message}"
        ) from exc

    attachment_ids = extract_created_short_ids(
        response,
        expected_count=len(attachments),
        list_tool="list_test_records",
    )

    return AttachmentsCreateResult(
        created=True,
        dry_run=False,
        attachment_ids=attachment_ids,
        payload_preview=None,
    )
