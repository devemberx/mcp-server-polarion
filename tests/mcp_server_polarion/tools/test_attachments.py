"""Attachment tool tests."""

from __future__ import annotations

import inspect
import json
from pathlib import Path
from typing import Annotated, get_type_hints
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastmcp.utilities.types import Image
from pydantic import TypeAdapter, ValidationError

from mcp_server_polarion.core.exceptions import (
    PolarionAuthError,
    PolarionError,
    PolarionNotFoundError,
    PolarionResponseTooLargeError,
)
from mcp_server_polarion.models import (
    Attachment,
    DocumentAttachmentSpec,
    PaginatedResult,
    WorkItemAttachmentSpec,
)
from mcp_server_polarion.tools.attachments import (
    _MAX_TOTAL_UPLOAD_BYTES,
    _build_document_attachments_payload,
    _build_work_item_attachments_payload,
    _read_attachment_files,
    create_document_attachments,
    create_work_item_attachments,
    get_document_attachment_content,
    list_document_attachments,
    list_work_item_attachments,
)


class TestListDocumentAttachments:
    """``list_document_attachments`` tool."""

    async def test_returns_paginated_result(
        self, mock_ctx: MagicMock, mock_client: AsyncMock
    ) -> None:
        mock_client.get.return_value = {
            "data": [
                {
                    "type": "document_attachments",
                    "id": "proj1/Design/SRS/1-screenshot-20260512-142738-1.png",
                    "attributes": {
                        "id": "1-screenshot-20260512-142738-1.png",
                        "fileName": "screenshot.png",
                        "title": "screenshot",
                        "updated": "2026-05-12T14:27:38Z",
                        "length": 2048,
                    },
                    "relationships": {
                        "author": {
                            "data": {"type": "users", "id": "alice"},
                        },
                    },
                },
            ],
            "included": [
                {"type": "users", "id": "alice", "attributes": {"name": "Alice A"}}
            ],
            "meta": {"totalCount": 1},
        }

        result = await list_document_attachments(
            mock_ctx,
            project_id="proj1",
            space_id="Design",
            document_name="SRS",
            page_size=100,
            page_number=1,
        )

        assert isinstance(result, PaginatedResult)
        assert result.total_count == 1
        assert result.page == 1
        assert result.page_size == 100
        assert result.has_more is False
        assert len(result.items) == 1

        attachment = result.items[0]
        assert isinstance(attachment, Attachment)
        assert attachment.id == "1-screenshot-20260512-142738-1.png"
        assert attachment.file_name == "screenshot.png"
        assert attachment.title == "screenshot"
        assert attachment.length == 2048
        assert attachment.updated == "2026-05-12T14:27:38Z"
        assert attachment.author_name == "Alice A"

    async def test_missing_relationships_default_to_empty(
        self, mock_ctx: MagicMock, mock_client: AsyncMock
    ) -> None:
        mock_client.get.return_value = {
            "data": [
                {
                    "type": "document_attachments",
                    "id": "proj1/Design/SRS/1-empty.png",
                    "attributes": {
                        "id": "1-empty.png",
                    },
                },
            ],
            "meta": {"totalCount": 1},
        }

        result = await list_document_attachments(
            mock_ctx,
            project_id="proj1",
            space_id="Design",
            document_name="SRS",
            page_size=100,
            page_number=1,
        )

        attachment = result.items[0]
        assert attachment.id == "1-empty.png"
        assert attachment.file_name == ""
        assert attachment.title == ""
        assert attachment.length == 0
        assert attachment.updated == ""
        assert attachment.author_name == ""

    async def test_signals_has_more_when_total_exceeds_page(
        self, mock_ctx: MagicMock, mock_client: AsyncMock
    ) -> None:
        mock_client.get.return_value = {
            "data": [
                {
                    "type": "document_attachments",
                    "id": f"proj1/Design/SRS/1-file-{i}.png",
                    "attributes": {
                        "id": f"1-file-{i}.png",
                        "fileName": f"file-{i}.png",
                        "title": f"file-{i}",
                        "updated": "2026-05-01T00:00:00Z",
                        "length": 10,
                    },
                    "relationships": {},
                }
                for i in range(2)
            ],
            "meta": {"totalCount": 5},
        }

        result = await list_document_attachments(
            mock_ctx,
            project_id="proj1",
            space_id="Design",
            document_name="SRS",
            page_size=2,
            page_number=1,
        )

        assert result.total_count == 5
        assert result.has_more is True
        assert len(result.items) == 2

    async def test_passes_pagination_and_fieldset_params(
        self, mock_ctx: MagicMock, mock_client: AsyncMock
    ) -> None:
        mock_client.get.return_value = {"data": []}

        await list_document_attachments(
            mock_ctx,
            project_id="proj1",
            space_id="_default",
            document_name="SRS",
            page_size=25,
            page_number=3,
        )

        calls = mock_client.get.call_args_list
        assert len(calls) == 1
        assert calls[0][0][0] == (
            "/projects/proj1/spaces/_default/documents/SRS/attachments"
        )
        params = calls[0][1]["params"]
        assert set(params.keys()) == {
            "fields[document_attachments]",
            "include",
            "fields[users]",
            "page[size]",
            "page[number]",
        }
        assert params["fields[document_attachments]"] == (
            "id,fileName,title,updated,length,author"
        )
        assert params["include"] == "author"
        assert params["fields[users]"] == "name"
        assert params["page[size]"] == 25
        assert params["page[number]"] == 3

    async def test_url_encodes_space_and_document_name(
        self, mock_ctx: MagicMock, mock_client: AsyncMock
    ) -> None:
        mock_client.get.return_value = {"data": []}

        await list_document_attachments(
            mock_ctx,
            project_id="proj1",
            space_id="My Space",
            document_name="A/B Doc",
            page_size=100,
            page_number=1,
        )

        path = mock_client.get.call_args_list[0][0][0]
        assert path == (
            "/projects/proj1/spaces/My%20Space/documents/A%2FB%20Doc/attachments"
        )

    async def test_not_found_raises_value_error(
        self, mock_ctx: MagicMock, mock_client: AsyncMock
    ) -> None:
        mock_client.get.side_effect = PolarionNotFoundError(
            "Not found",
            status_code=404,
        )

        with pytest.raises(ValueError, match="Design/SRS"):
            await list_document_attachments(
                mock_ctx,
                project_id="proj1",
                space_id="Design",
                document_name="SRS",
                page_size=100,
                page_number=1,
            )

    async def test_not_found_points_at_list_documents(
        self, mock_ctx: MagicMock, mock_client: AsyncMock
    ) -> None:
        mock_client.get.side_effect = PolarionNotFoundError(
            "Not found",
            status_code=404,
        )

        with pytest.raises(ValueError, match="list_documents"):
            await list_document_attachments(
                mock_ctx,
                project_id="proj1",
                space_id="Design",
                document_name="SRS",
                page_size=100,
                page_number=1,
            )

    async def test_auth_error_raises_permission_error(
        self, mock_ctx: MagicMock, mock_client: AsyncMock
    ) -> None:
        mock_client.get.side_effect = PolarionAuthError(
            "Forbidden",
            status_code=403,
        )

        with pytest.raises(PermissionError, match="POLARION_TOKEN"):
            await list_document_attachments(
                mock_ctx,
                project_id="proj1",
                space_id="Design",
                document_name="SRS",
                page_size=100,
                page_number=1,
            )

    async def test_polarion_error_raises_runtime_error(
        self, mock_ctx: MagicMock, mock_client: AsyncMock
    ) -> None:
        mock_client.get.side_effect = PolarionError(
            "Boom",
            status_code=500,
        )

        with pytest.raises(RuntimeError, match="Failed to list attachments"):
            await list_document_attachments(
                mock_ctx,
                project_id="proj1",
                space_id="Design",
                document_name="SRS",
                page_size=100,
                page_number=1,
            )


class TestListDocumentAttachmentsFieldValidation:
    """``page_size`` bound — direct calls bypass FastMCP JSON Schema gate;
    rebuild ``TypeAdapter`` per parameter to prove the constraint is wired.
    """

    @staticmethod
    def _adapter_for(param_name: str) -> TypeAdapter[object]:
        hints = get_type_hints(list_document_attachments)
        sig = inspect.signature(list_document_attachments)
        field_info = sig.parameters[param_name].default
        return TypeAdapter(Annotated[hints[param_name], field_info])

    def test_page_size_rejects_zero(self) -> None:
        with pytest.raises(ValidationError):
            self._adapter_for("page_size").validate_python(0)

    def test_page_size_rejects_over_max(self) -> None:
        with pytest.raises(ValidationError):
            self._adapter_for("page_size").validate_python(101)

    def test_page_size_accepts_minimum(self) -> None:
        assert self._adapter_for("page_size").validate_python(1) == 1

    def test_page_size_accepts_maximum(self) -> None:
        assert self._adapter_for("page_size").validate_python(100) == 100


class TestGetDocumentAttachmentContent:
    """``get_document_attachment_content`` tool."""

    async def test_bitmap_happy_path_returns_image(
        self, mock_ctx: MagicMock, mock_client: AsyncMock
    ) -> None:
        raw = b"\x89PNG\r\n\x1a\nfakepngbytes"
        mock_client.get_bytes = AsyncMock(return_value=raw)

        result = await get_document_attachment_content(
            mock_ctx,
            project_id="proj1",
            space_id="Design",
            document_name="SRS",
            attachment_id="1-shot.png",
        )

        assert isinstance(result, Image)
        assert result.data == raw
        assert result.to_image_content().mimeType == "image/png"
        mock_client.get_bytes.assert_awaited_once_with(
            "/projects/proj1/spaces/Design/documents/SRS/attachments/1-shot.png/content",
            max_bytes=5 * 1024 * 1024,
        )

    async def test_uppercase_extension_still_bitmap(
        self, mock_ctx: MagicMock, mock_client: AsyncMock
    ) -> None:
        raw = b"\x89PNG\r\n\x1a\nfakepngbytes"
        mock_client.get_bytes = AsyncMock(return_value=raw)

        result = await get_document_attachment_content(
            mock_ctx,
            project_id="proj1",
            space_id="Design",
            document_name="SRS",
            attachment_id="1-shot.PNG",
        )

        assert isinstance(result, Image)
        assert result.to_image_content().mimeType == "image/png"

    async def test_svg_returns_decoded_string(
        self, mock_ctx: MagicMock, mock_client: AsyncMock
    ) -> None:
        raw = b"<svg xmlns='http://www.w3.org/2000/svg'></svg>"
        mock_client.get_bytes = AsyncMock(return_value=raw)

        result = await get_document_attachment_content(
            mock_ctx,
            project_id="proj1",
            space_id="Design",
            document_name="SRS",
            attachment_id="1-diagram.svg",
        )

        assert isinstance(result, str)
        assert result == raw.decode("utf-8")
        mock_client.get_bytes.assert_awaited_once_with(
            "/projects/proj1/spaces/Design/documents/SRS/attachments"
            "/1-diagram.svg/content",
            max_bytes=64 * 1024,
        )

    async def test_bitmap_magic_overrides_extension(
        self, mock_ctx: MagicMock, mock_client: AsyncMock
    ) -> None:
        """Renamed file: .png name, JPEG bytes — magic decides served mime."""
        mock_client.get_bytes = AsyncMock(return_value=b"\xff\xd8\xffjpegbytes")

        result = await get_document_attachment_content(
            mock_ctx,
            project_id="proj1",
            space_id="Design",
            document_name="SRS",
            attachment_id="1-shot.png",
        )

        assert isinstance(result, Image)
        assert result.to_image_content().mimeType == "image/jpeg"

    @pytest.mark.parametrize(
        "attachment_id",
        ["1-a.png", "1-a.jpg", "1-a.jpeg", "1-a.jpe", "1-a.gif", "1-a.webp"],
    )
    async def test_every_bitmap_extension_routes_pre_request(
        self, mock_ctx: MagicMock, mock_client: AsyncMock, attachment_id: str
    ) -> None:
        """Extension map project-owned — dropped key = silent false reject."""
        mock_client.get_bytes = AsyncMock(return_value=b"\x89PNG\r\n\x1a\npng")

        result = await get_document_attachment_content(
            mock_ctx,
            project_id="proj1",
            space_id="Design",
            document_name="SRS",
            attachment_id=attachment_id,
        )

        assert isinstance(result, Image)
        assert mock_client.get_bytes.await_args.kwargs["max_bytes"] == 5 * 1024 * 1024

    @pytest.mark.parametrize("magic", [b"GIF87a", b"GIF89a"])
    async def test_gif_magic_detected(
        self, mock_ctx: MagicMock, mock_client: AsyncMock, magic: bytes
    ) -> None:
        mock_client.get_bytes = AsyncMock(return_value=magic + b"gifbytes")

        result = await get_document_attachment_content(
            mock_ctx,
            project_id="proj1",
            space_id="Design",
            document_name="SRS",
            attachment_id="1-anim.gif",
        )

        assert isinstance(result, Image)
        assert result.to_image_content().mimeType == "image/gif"

    async def test_webp_riff_magic_detected(
        self, mock_ctx: MagicMock, mock_client: AsyncMock
    ) -> None:
        mock_client.get_bytes = AsyncMock(
            return_value=b"RIFF\x24\x00\x00\x00WEBPVP8 webpbytes"
        )

        result = await get_document_attachment_content(
            mock_ctx,
            project_id="proj1",
            space_id="Design",
            document_name="SRS",
            attachment_id="1-shot.webp",
        )

        assert isinstance(result, Image)
        assert result.to_image_content().mimeType == "image/webp"

    async def test_bitmap_magic_mismatch_raises_value_error(
        self, mock_ctx: MagicMock, mock_client: AsyncMock
    ) -> None:
        """Garbage bytes as image = whole-request vision API 400 — reject."""
        mock_client.get_bytes = AsyncMock(return_value=b"MZ not an image")

        with pytest.raises(
            ValueError,
            match=r"\(gif, jpeg, png, webp\).*list_document_attachments",
        ):
            await get_document_attachment_content(
                mock_ctx,
                project_id="proj1",
                space_id="Design",
                document_name="SRS",
                attachment_id="1-shot.png",
            )

    async def test_svg_bom_and_whitespace_prefix_ok(
        self, mock_ctx: MagicMock, mock_client: AsyncMock
    ) -> None:
        raw = b"\xef\xbb\xbf\n <?xml version='1.0'?><svg></svg>"
        mock_client.get_bytes = AsyncMock(return_value=raw)

        result = await get_document_attachment_content(
            mock_ctx,
            project_id="proj1",
            space_id="Design",
            document_name="SRS",
            attachment_id="1-diagram.svg",
        )

        assert isinstance(result, str)

    async def test_svg_binary_content_raises_value_error(
        self, mock_ctx: MagicMock, mock_client: AsyncMock
    ) -> None:
        """Binary mislabeled .svg = up to 16k tokens of mojibake — reject."""
        mock_client.get_bytes = AsyncMock(return_value=b"\x89PNG\r\n\x1a\npng")

        with pytest.raises(ValueError, match="list_document_attachments"):
            await get_document_attachment_content(
                mock_ctx,
                project_id="proj1",
                space_id="Design",
                document_name="SRS",
                attachment_id="1-diagram.svg",
            )

    async def test_unsupported_extension_raises_value_error_no_client_call(
        self, mock_ctx: MagicMock, mock_client: AsyncMock
    ) -> None:
        mock_client.get_bytes = AsyncMock()

        with pytest.raises(ValueError, match="list_document_attachments"):
            await get_document_attachment_content(
                mock_ctx,
                project_id="proj1",
                space_id="Design",
                document_name="SRS",
                attachment_id="1-report.docx",
            )

        mock_client.get_bytes.assert_not_awaited()

    async def test_extensionless_id_raises_value_error_no_client_call(
        self, mock_ctx: MagicMock, mock_client: AsyncMock
    ) -> None:
        mock_client.get_bytes = AsyncMock()

        with pytest.raises(ValueError, match="list_document_attachments"):
            await get_document_attachment_content(
                mock_ctx,
                project_id="proj1",
                space_id="Design",
                document_name="SRS",
                attachment_id="README",
            )

        mock_client.get_bytes.assert_not_awaited()

    async def test_svgz_rejected_pre_request(
        self, mock_ctx: MagicMock, mock_client: AsyncMock
    ) -> None:
        """.svgz = gzipped SVG — bytes never decode as markup, reject early."""
        mock_client.get_bytes = AsyncMock()

        with pytest.raises(ValueError, match="list_document_attachments"):
            await get_document_attachment_content(
                mock_ctx,
                project_id="proj1",
                space_id="Design",
                document_name="SRS",
                attachment_id="1-diagram.svgz",
            )

        mock_client.get_bytes.assert_not_awaited()

    async def test_gz_double_extension_rejected_pre_request(
        self, mock_ctx: MagicMock, mock_client: AsyncMock
    ) -> None:
        """.png.gz = gzip bytes, not PNG — reject on final suffix."""
        mock_client.get_bytes = AsyncMock()

        with pytest.raises(ValueError, match="list_document_attachments"):
            await get_document_attachment_content(
                mock_ctx,
                project_id="proj1",
                space_id="Design",
                document_name="SRS",
                attachment_id="1-shot.png.gz",
            )

        mock_client.get_bytes.assert_not_awaited()

    async def test_unsupported_extension_message_lists_extensions(
        self, mock_ctx: MagicMock, mock_client: AsyncMock
    ) -> None:
        """Rejection lists extensions, not mime types — LLM match file_name."""
        mock_client.get_bytes = AsyncMock()

        with pytest.raises(
            ValueError, match=r"supported formats: gif, jpeg, png, webp, svg"
        ):
            await get_document_attachment_content(
                mock_ctx,
                project_id="proj1",
                space_id="Design",
                document_name="SRS",
                attachment_id="1-report.docx",
            )

    async def test_response_too_large_raises_value_error_mentioning_cap(
        self, mock_ctx: MagicMock, mock_client: AsyncMock
    ) -> None:
        mock_client.get_bytes = AsyncMock(
            side_effect=PolarionResponseTooLargeError(
                "too big",
                limit=5 * 1024 * 1024,
            )
        )

        with pytest.raises(ValueError, match="5242880"):
            await get_document_attachment_content(
                mock_ctx,
                project_id="proj1",
                space_id="Design",
                document_name="SRS",
                attachment_id="1-shot.png",
            )

    async def test_not_found_raises_value_error(
        self, mock_ctx: MagicMock, mock_client: AsyncMock
    ) -> None:
        mock_client.get_bytes = AsyncMock(
            side_effect=PolarionNotFoundError("Not found", status_code=404)
        )

        with pytest.raises(ValueError, match="list_document_attachments"):
            await get_document_attachment_content(
                mock_ctx,
                project_id="proj1",
                space_id="Design",
                document_name="SRS",
                attachment_id="1-shot.png",
            )

    async def test_auth_error_raises_permission_error(
        self, mock_ctx: MagicMock, mock_client: AsyncMock
    ) -> None:
        mock_client.get_bytes = AsyncMock(
            side_effect=PolarionAuthError("Forbidden", status_code=403)
        )

        with pytest.raises(PermissionError, match="POLARION_TOKEN"):
            await get_document_attachment_content(
                mock_ctx,
                project_id="proj1",
                space_id="Design",
                document_name="SRS",
                attachment_id="1-shot.png",
            )

    async def test_polarion_error_raises_runtime_error(
        self, mock_ctx: MagicMock, mock_client: AsyncMock
    ) -> None:
        mock_client.get_bytes = AsyncMock(
            side_effect=PolarionError("Boom", status_code=500)
        )

        with pytest.raises(RuntimeError):
            await get_document_attachment_content(
                mock_ctx,
                project_id="proj1",
                space_id="Design",
                document_name="SRS",
                attachment_id="1-shot.png",
            )

    async def test_url_encodes_path_segments(
        self, mock_ctx: MagicMock, mock_client: AsyncMock
    ) -> None:
        mock_client.get_bytes = AsyncMock(return_value=b"\x89PNG\r\n\x1a\nfakepngbytes")

        await get_document_attachment_content(
            mock_ctx,
            project_id="proj1",
            space_id="My Space",
            document_name="A/B Doc",
            attachment_id="1 shot.png",
        )

        path = mock_client.get_bytes.call_args_list[0][0][0]
        assert path == (
            "/projects/proj1/spaces/My%20Space/documents/A%2FB%20Doc"
            "/attachments/1%20shot.png/content"
        )


class TestListWorkItemAttachments:
    """``list_work_item_attachments`` tool."""

    async def test_returns_paginated_result(
        self, mock_ctx: MagicMock, mock_client: AsyncMock
    ) -> None:
        mock_client.get.return_value = {
            "data": [
                {
                    "type": "workitem_attachments",
                    "id": f"proj1/MCPT-556/1-file-{i}.png",
                    "attributes": {
                        "id": f"1-file-{i}.png",
                        "fileName": f"file-{i}.png",
                        "title": f"file-{i}",
                        "updated": "2026-07-19T14:27:38Z",
                        "length": 1024 * (i + 1),
                    },
                    "relationships": {
                        "author": {
                            "data": {"type": "users", "id": "alice"},
                        },
                    },
                }
                for i in range(3)
            ],
            "included": [
                {"type": "users", "id": "alice", "attributes": {"name": "Alice A"}}
            ],
            "meta": {"totalCount": 3},
        }

        result = await list_work_item_attachments(
            mock_ctx,
            project_id="proj1",
            work_item_id="MCPT-556",
            page_size=100,
            page_number=1,
        )

        assert isinstance(result, PaginatedResult)
        assert result.total_count == 3
        assert result.page == 1
        assert result.page_size == 100
        assert result.has_more is False
        assert len(result.items) == 3

        attachment = result.items[0]
        assert isinstance(attachment, Attachment)
        assert attachment.id == "1-file-0.png"
        assert attachment.file_name == "file-0.png"
        assert attachment.title == "file-0"
        assert attachment.length == 1024
        assert attachment.updated == "2026-07-19T14:27:38Z"
        assert attachment.author_name == "Alice A"
        assert all(item.author_name == "Alice A" for item in result.items)

    async def test_returns_empty_page(
        self, mock_ctx: MagicMock, mock_client: AsyncMock
    ) -> None:
        mock_client.get.return_value = {"data": []}

        result = await list_work_item_attachments(
            mock_ctx,
            project_id="proj1",
            work_item_id="MCPT-999",
            page_size=100,
            page_number=1,
        )

        assert result.total_count == 0
        assert result.has_more is False
        assert result.items == []

    async def test_signals_has_more_when_total_exceeds_page(
        self, mock_ctx: MagicMock, mock_client: AsyncMock
    ) -> None:
        mock_client.get.return_value = {
            "data": [
                {
                    "type": "workitem_attachments",
                    "id": f"proj1/MCPT-556/1-file-{i}.png",
                    "attributes": {
                        "id": f"1-file-{i}.png",
                        "fileName": f"file-{i}.png",
                        "title": f"file-{i}",
                        "updated": "2026-07-19T00:00:00Z",
                        "length": 10,
                    },
                    "relationships": {},
                }
                for i in range(2)
            ],
            # Live rule: work item attachments meta.totalCount present page
            # 1 already (diverges document attachments overshoot-only rule);
            # compute_has_more reuse means tool logic never branch on it.
            "meta": {"totalCount": 5},
        }

        result = await list_work_item_attachments(
            mock_ctx,
            project_id="proj1",
            work_item_id="MCPT-556",
            page_size=2,
            page_number=1,
        )

        assert result.total_count == 5
        assert result.has_more is True
        assert len(result.items) == 2

    async def test_passes_pagination_and_fieldset_params(
        self, mock_ctx: MagicMock, mock_client: AsyncMock
    ) -> None:
        mock_client.get.return_value = {"data": []}

        await list_work_item_attachments(
            mock_ctx,
            project_id="proj1",
            work_item_id="MCPT-556",
            page_size=25,
            page_number=3,
        )

        calls = mock_client.get.call_args_list
        assert len(calls) == 1
        assert calls[0][0][0] == "/projects/proj1/workitems/MCPT-556/attachments"
        params = calls[0][1]["params"]
        assert set(params.keys()) == {
            "fields[workitem_attachments]",
            "include",
            "fields[users]",
            "page[size]",
            "page[number]",
        }
        assert params["fields[workitem_attachments]"] == (
            "id,fileName,title,updated,length,author"
        )
        assert params["include"] == "author"
        assert params["fields[users]"] == "name"
        assert params["page[size]"] == 25
        assert params["page[number]"] == 3

    async def test_url_encodes_project_and_work_item_id(
        self, mock_ctx: MagicMock, mock_client: AsyncMock
    ) -> None:
        mock_client.get.return_value = {"data": []}

        await list_work_item_attachments(
            mock_ctx,
            project_id="My Proj",
            work_item_id="MCPT/556",
            page_size=100,
            page_number=1,
        )

        path = mock_client.get.call_args_list[0][0][0]
        assert path == "/projects/My%20Proj/workitems/MCPT%2F556/attachments"

    async def test_not_found_raises_value_error(
        self, mock_ctx: MagicMock, mock_client: AsyncMock
    ) -> None:
        mock_client.get.side_effect = PolarionNotFoundError(
            "Not found",
            status_code=404,
        )

        with pytest.raises(ValueError, match="MCPT-556"):
            await list_work_item_attachments(
                mock_ctx,
                project_id="proj1",
                work_item_id="MCPT-556",
                page_size=100,
                page_number=1,
            )

    async def test_not_found_points_at_list_work_items(
        self, mock_ctx: MagicMock, mock_client: AsyncMock
    ) -> None:
        mock_client.get.side_effect = PolarionNotFoundError(
            "Not found",
            status_code=404,
        )

        with pytest.raises(ValueError, match="list_work_items"):
            await list_work_item_attachments(
                mock_ctx,
                project_id="proj1",
                work_item_id="MCPT-556",
                page_size=100,
                page_number=1,
            )

    async def test_auth_error_raises_permission_error(
        self, mock_ctx: MagicMock, mock_client: AsyncMock
    ) -> None:
        mock_client.get.side_effect = PolarionAuthError(
            "Forbidden",
            status_code=403,
        )

        with pytest.raises(PermissionError, match="POLARION_TOKEN"):
            await list_work_item_attachments(
                mock_ctx,
                project_id="proj1",
                work_item_id="MCPT-556",
                page_size=100,
                page_number=1,
            )

    async def test_polarion_error_raises_runtime_error(
        self, mock_ctx: MagicMock, mock_client: AsyncMock
    ) -> None:
        mock_client.get.side_effect = PolarionError(
            "Boom",
            status_code=500,
        )

        with pytest.raises(RuntimeError, match="Failed to list attachments"):
            await list_work_item_attachments(
                mock_ctx,
                project_id="proj1",
                work_item_id="MCPT-556",
                page_size=100,
                page_number=1,
            )


class TestListWorkItemAttachmentsFieldValidation:
    """``page_size``/``page_number`` bounds -- direct calls bypass FastMCP
    JSON Schema gate; rebuild ``TypeAdapter`` per parameter to prove the
    constraint is wired.
    """

    @staticmethod
    def _adapter_for(param_name: str) -> TypeAdapter[object]:
        hints = get_type_hints(list_work_item_attachments)
        sig = inspect.signature(list_work_item_attachments)
        field_info = sig.parameters[param_name].default
        return TypeAdapter(Annotated[hints[param_name], field_info])

    def test_page_size_rejects_zero(self) -> None:
        with pytest.raises(ValidationError):
            self._adapter_for("page_size").validate_python(0)

    def test_page_size_rejects_over_max(self) -> None:
        with pytest.raises(ValidationError):
            self._adapter_for("page_size").validate_python(101)

    def test_page_size_accepts_minimum(self) -> None:
        assert self._adapter_for("page_size").validate_python(1) == 1

    def test_page_size_accepts_maximum(self) -> None:
        assert self._adapter_for("page_size").validate_python(100) == 100

    def test_page_number_rejects_zero(self) -> None:
        with pytest.raises(ValidationError):
            self._adapter_for("page_number").validate_python(0)

    def test_page_number_accepts_minimum(self) -> None:
        assert self._adapter_for("page_number").validate_python(1) == 1


class TestBuildDocumentAttachmentsPayload:
    """``_build_document_attachments_payload`` -- pure JSON:API body builder."""

    def test_file_name_defaults_to_basename(self) -> None:
        payload = _build_document_attachments_payload(
            [DocumentAttachmentSpec(file_path="/data/foo/bar.png")]
        )
        attrs = payload["data"][0]["attributes"]  # type: ignore[index]
        assert attrs["fileName"] == "bar.png"  # type: ignore[index]

    def test_file_name_override_wins(self) -> None:
        payload = _build_document_attachments_payload(
            [
                DocumentAttachmentSpec(
                    file_path="/data/foo/bar.png", file_name="renamed.png"
                )
            ]
        )
        attrs = payload["data"][0]["attributes"]  # type: ignore[index]
        assert attrs["fileName"] == "renamed.png"  # type: ignore[index]

    def test_title_omitted_when_none(self) -> None:
        payload = _build_document_attachments_payload(
            [DocumentAttachmentSpec(file_path="/data/bar.png")]
        )
        attrs = payload["data"][0]["attributes"]  # type: ignore[index]
        assert "title" not in attrs  # type: ignore[operator]

    def test_title_included_when_set(self) -> None:
        payload = _build_document_attachments_payload(
            [DocumentAttachmentSpec(file_path="/data/bar.png", title="A Chart")]
        )
        attrs = payload["data"][0]["attributes"]  # type: ignore[index]
        assert attrs["title"] == "A Chart"  # type: ignore[index]

    def test_type_is_document_attachments(self) -> None:
        payload = _build_document_attachments_payload(
            [DocumentAttachmentSpec(file_path="/data/bar.png")]
        )
        assert payload["data"][0]["type"] == "document_attachments"  # type: ignore[index]

    def test_multiple_specs_preserve_order(self) -> None:
        payload = _build_document_attachments_payload(
            [
                DocumentAttachmentSpec(file_path="/data/a.png"),
                DocumentAttachmentSpec(file_path="/data/b.png"),
            ]
        )
        names = [
            item["attributes"]["fileName"]  # type: ignore[index]
            for item in payload["data"]  # type: ignore[union-attr]
        ]
        assert names == ["a.png", "b.png"]


class TestReadAttachmentFiles:
    """``_read_attachment_files`` -- local read + validation seam."""

    def test_returns_ordered_name_bytes_pairs(self, tmp_path: Path) -> None:
        first = tmp_path / "a.png"
        second = tmp_path / "b.png"
        first.write_bytes(b"aaa")
        second.write_bytes(b"bbbb")

        result = _read_attachment_files(
            [
                DocumentAttachmentSpec(file_path=str(first)),
                DocumentAttachmentSpec(file_path=str(second)),
            ]
        )

        assert result == [("a.png", b"aaa"), ("b.png", b"bbbb")]

    def test_file_name_override_used_as_key(self, tmp_path: Path) -> None:
        path = tmp_path / "a.png"
        path.write_bytes(b"data")

        result = _read_attachment_files(
            [DocumentAttachmentSpec(file_path=str(path), file_name="renamed.png")]
        )

        assert result == [("renamed.png", b"data")]

    def test_missing_file_raises_value_error(self, tmp_path: Path) -> None:
        missing = tmp_path / "missing.png"

        with pytest.raises(ValueError, match=str(missing)):
            _read_attachment_files([DocumentAttachmentSpec(file_path=str(missing))])

    def test_directory_path_raises_value_error(self, tmp_path: Path) -> None:
        directory = tmp_path / "adir"
        directory.mkdir()

        with pytest.raises(ValueError, match="directory"):
            _read_attachment_files([DocumentAttachmentSpec(file_path=str(directory))])

    def test_duplicate_effective_file_name_now_allowed(self, tmp_path: Path) -> None:
        # Dup-reject moved to caller (doc-only semantics) -- helper itself
        # stays permissive so work item callers can pass duplicates through.
        first = tmp_path / "a.png"
        second = tmp_path / "b.png"
        first.write_bytes(b"1")
        second.write_bytes(b"2")

        result = _read_attachment_files(
            [
                DocumentAttachmentSpec(file_path=str(first), file_name="dup.png"),
                DocumentAttachmentSpec(file_path=str(second), file_name="dup.png"),
            ]
        )

        assert result == [("dup.png", b"1"), ("dup.png", b"2")]

    def test_duplicate_effective_file_name_allowed_for_work_item_spec(
        self, tmp_path: Path
    ) -> None:
        first = tmp_path / "a.png"
        second = tmp_path / "b.png"
        first.write_bytes(b"1")
        second.write_bytes(b"2")

        result = _read_attachment_files(
            [
                WorkItemAttachmentSpec(file_path=str(first), file_name="dup.png"),
                WorkItemAttachmentSpec(file_path=str(second), file_name="dup.png"),
            ]
        )

        assert result == [("dup.png", b"1"), ("dup.png", b"2")]

    def test_file_name_with_slash_raises_value_error(self, tmp_path: Path) -> None:
        # Separator shift id path segments (server unverified) -- fail closed.
        path = tmp_path / "a.png"
        path.write_bytes(b"1")

        with pytest.raises(ValueError, match=r"path separator"):
            _read_attachment_files(
                [DocumentAttachmentSpec(file_path=str(path), file_name="sub/a.png")]
            )

    def test_windows_file_path_basename_raises_separator_error(self) -> None:
        # Windows path on POSIX: basename = unsplit whole string with '\\' --
        # separator error name real cause, not "does not exist".
        with pytest.raises(ValueError, match=r"path separator"):
            _read_attachment_files(
                [DocumentAttachmentSpec(file_path="C:\\Users\\x\\shot.png")]
            )

    def test_single_file_over_cap_raises_value_error(self, tmp_path: Path) -> None:
        big = tmp_path / "big.bin"
        # Sparse file: logical size over cap, real disk usage near-zero --
        # cap check reads size via stat, never loads oversized content.
        with big.open("wb") as handle:
            handle.seek(_MAX_TOTAL_UPLOAD_BYTES)
            handle.write(b"\0")

        with pytest.raises(ValueError, match=r"big\.bin"):
            _read_attachment_files([DocumentAttachmentSpec(file_path=str(big))])

    def test_total_over_cap_lists_every_file(self, tmp_path: Path) -> None:
        first = tmp_path / "first.bin"
        second = tmp_path / "second.bin"
        half_cap_over = _MAX_TOTAL_UPLOAD_BYTES // 2 + 1
        for path in (first, second):
            with path.open("wb") as handle:
                handle.seek(half_cap_over)
                handle.write(b"\0")

        with pytest.raises(ValueError, match=r"first\.bin.*second\.bin"):
            _read_attachment_files(
                [
                    DocumentAttachmentSpec(file_path=str(first)),
                    DocumentAttachmentSpec(file_path=str(second)),
                ]
            )

    def test_read_failure_after_validation_raises_value_error(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Stat pass, read fail (deleted/perms mid-call) = ValueError, not OSError.
        target = tmp_path / "vanish.png"
        target.write_bytes(b"png")

        def explode(self: Path) -> bytes:
            raise OSError("gone")

        monkeypatch.setattr(Path, "read_bytes", explode)
        with pytest.raises(ValueError, match=r"Cannot read .*vanish\.png"):
            _read_attachment_files([DocumentAttachmentSpec(file_path=str(target))])

    def test_file_grown_past_cap_between_stat_and_read_raises(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Stat under cap, file grow before read = post-read total re-check
        # catch it -- stat-time cap alone = TOCTOU bypass.
        target = tmp_path / "grow.bin"
        target.write_bytes(b"small")

        def grown(self: Path) -> bytes:
            return b"\0" * (_MAX_TOTAL_UPLOAD_BYTES + 1)

        monkeypatch.setattr(Path, "read_bytes", grown)
        with pytest.raises(ValueError, match=r"grew"):
            _read_attachment_files([DocumentAttachmentSpec(file_path=str(target))])


class TestCreateDocumentAttachmentsDryRun:
    """dry_run return preview without calling Polarion; files still validated."""

    async def test_dry_run_no_post_multipart_call(
        self, mock_ctx: MagicMock, mock_client: AsyncMock, tmp_path: Path
    ) -> None:
        file_path = tmp_path / "shot.png"
        file_path.write_bytes(b"pngdata")
        mock_client.post_multipart = AsyncMock()

        result = await create_document_attachments(
            mock_ctx,
            project_id="P",
            space_id="S",
            document_name="D",
            attachments=[DocumentAttachmentSpec(file_path=str(file_path))],
            dry_run=True,
        )

        mock_client.post_multipart.assert_not_called()
        assert result.created is False
        assert result.dry_run is True
        assert result.attachment_ids == []

    async def test_dry_run_payload_preview_shape(
        self, mock_ctx: MagicMock, mock_client: AsyncMock, tmp_path: Path
    ) -> None:
        file_path = tmp_path / "shot.png"
        file_path.write_bytes(b"pngdata")

        result = await create_document_attachments(
            mock_ctx,
            project_id="P",
            space_id="S",
            document_name="D",
            attachments=[
                DocumentAttachmentSpec(file_path=str(file_path), title="Shot")
            ],
            dry_run=True,
        )

        assert result.payload_preview is not None
        data = result.payload_preview["data"]
        assert isinstance(data, list)
        assert data[0]["attributes"]["fileName"] == "shot.png"  # type: ignore[index]
        assert data[0]["attributes"]["title"] == "Shot"  # type: ignore[index]
        assert result.payload_preview["files"] == [
            {"file_name": "shot.png", "size_bytes": 7}
        ]

    async def test_dry_run_still_validates_files(
        self, mock_ctx: MagicMock, mock_client: AsyncMock, tmp_path: Path
    ) -> None:
        missing = tmp_path / "missing.png"

        with pytest.raises(ValueError, match=str(missing)):
            await create_document_attachments(
                mock_ctx,
                project_id="P",
                space_id="S",
                document_name="D",
                attachments=[DocumentAttachmentSpec(file_path=str(missing))],
                dry_run=True,
            )


class TestCreateDocumentAttachmentsHappyPath:
    """Successful upload extracts ordered bare attachment ids."""

    async def test_returns_ordered_attachment_ids(
        self, mock_ctx: MagicMock, mock_client: AsyncMock, tmp_path: Path
    ) -> None:
        first = tmp_path / "a.png"
        second = tmp_path / "b.png"
        first.write_bytes(b"aaa")
        second.write_bytes(b"bbbb")
        mock_client.post_multipart = AsyncMock(
            return_value={
                "data": [
                    {"type": "document_attachments", "id": "P/S/D/a.png"},
                    {"type": "document_attachments", "id": "P/S/D/b.png"},
                ]
            }
        )

        result = await create_document_attachments(
            mock_ctx,
            project_id="P",
            space_id="S",
            document_name="D",
            attachments=[
                DocumentAttachmentSpec(file_path=str(first)),
                DocumentAttachmentSpec(file_path=str(second)),
            ],
            dry_run=False,
        )

        assert result.created is True
        assert result.dry_run is False
        assert result.attachment_ids == ["a.png", "b.png"]
        assert result.payload_preview is None

    async def test_post_multipart_called_with_ordered_parts(
        self, mock_ctx: MagicMock, mock_client: AsyncMock, tmp_path: Path
    ) -> None:
        first = tmp_path / "a.png"
        second = tmp_path / "b.png"
        first.write_bytes(b"aaa")
        second.write_bytes(b"bbbb")
        mock_client.post_multipart = AsyncMock(
            return_value={
                "data": [
                    {"type": "document_attachments", "id": "P/S/D/a.png"},
                    {"type": "document_attachments", "id": "P/S/D/b.png"},
                ]
            }
        )

        await create_document_attachments(
            mock_ctx,
            project_id="P",
            space_id="S",
            document_name="D",
            attachments=[
                DocumentAttachmentSpec(file_path=str(first)),
                DocumentAttachmentSpec(file_path=str(second)),
            ],
            dry_run=False,
        )

        call = mock_client.post_multipart.call_args
        assert call[0][0] == "/projects/P/spaces/S/documents/D/attachments"
        resource = json.loads(call.kwargs["data"]["resource"])
        assert [item["attributes"]["fileName"] for item in resource["data"]] == [
            "a.png",
            "b.png",
        ]
        assert call.kwargs["files"] == [
            ("files", ("a.png", b"aaa", "application/octet-stream")),
            ("files", ("b.png", b"bbbb", "application/octet-stream")),
        ]

    async def test_no_attachment_ids_raises_runtime_error(
        self, mock_ctx: MagicMock, mock_client: AsyncMock, tmp_path: Path
    ) -> None:
        path = tmp_path / "a.png"
        path.write_bytes(b"a")
        mock_client.post_multipart = AsyncMock(return_value={"data": []})

        with pytest.raises(RuntimeError, match="list_document_attachments"):
            await create_document_attachments(
                mock_ctx,
                project_id="P",
                space_id="S",
                document_name="D",
                attachments=[DocumentAttachmentSpec(file_path=str(path))],
                dry_run=False,
            )


class TestCreateDocumentAttachmentsValidationBeforeCall:
    """Local pre-request validation blocks the POST entirely."""

    async def test_missing_file_raises_value_error_no_post(
        self, mock_ctx: MagicMock, mock_client: AsyncMock, tmp_path: Path
    ) -> None:
        missing = tmp_path / "missing.png"
        mock_client.post_multipart = AsyncMock()

        with pytest.raises(ValueError, match=str(missing)):
            await create_document_attachments(
                mock_ctx,
                project_id="P",
                space_id="S",
                document_name="D",
                attachments=[DocumentAttachmentSpec(file_path=str(missing))],
                dry_run=False,
            )
        mock_client.post_multipart.assert_not_called()

    async def test_directory_path_raises_value_error_no_post(
        self, mock_ctx: MagicMock, mock_client: AsyncMock, tmp_path: Path
    ) -> None:
        directory = tmp_path / "adir"
        directory.mkdir()
        mock_client.post_multipart = AsyncMock()

        with pytest.raises(ValueError, match="directory"):
            await create_document_attachments(
                mock_ctx,
                project_id="P",
                space_id="S",
                document_name="D",
                attachments=[DocumentAttachmentSpec(file_path=str(directory))],
                dry_run=False,
            )
        mock_client.post_multipart.assert_not_called()

    async def test_duplicate_file_name_raises_value_error_no_post(
        self, mock_ctx: MagicMock, mock_client: AsyncMock, tmp_path: Path
    ) -> None:
        first = tmp_path / "a.png"
        second = tmp_path / "b.png"
        first.write_bytes(b"1")
        second.write_bytes(b"2")
        mock_client.post_multipart = AsyncMock()

        with pytest.raises(ValueError, match=r"dup\.png"):
            await create_document_attachments(
                mock_ctx,
                project_id="P",
                space_id="S",
                document_name="D",
                attachments=[
                    DocumentAttachmentSpec(file_path=str(first), file_name="dup.png"),
                    DocumentAttachmentSpec(file_path=str(second), file_name="dup.png"),
                ],
                dry_run=False,
            )
        mock_client.post_multipart.assert_not_called()

    async def test_single_file_over_cap_raises_value_error_no_post(
        self, mock_ctx: MagicMock, mock_client: AsyncMock, tmp_path: Path
    ) -> None:
        big = tmp_path / "big.bin"
        with big.open("wb") as handle:
            handle.seek(_MAX_TOTAL_UPLOAD_BYTES)
            handle.write(b"\0")
        mock_client.post_multipart = AsyncMock()

        with pytest.raises(ValueError, match=r"big\.bin"):
            await create_document_attachments(
                mock_ctx,
                project_id="P",
                space_id="S",
                document_name="D",
                attachments=[DocumentAttachmentSpec(file_path=str(big))],
                dry_run=False,
            )
        mock_client.post_multipart.assert_not_called()

    async def test_total_over_cap_raises_value_error_no_post(
        self, mock_ctx: MagicMock, mock_client: AsyncMock, tmp_path: Path
    ) -> None:
        first = tmp_path / "first.bin"
        second = tmp_path / "second.bin"
        half_cap_over = _MAX_TOTAL_UPLOAD_BYTES // 2 + 1
        for path in (first, second):
            with path.open("wb") as handle:
                handle.seek(half_cap_over)
                handle.write(b"\0")
        mock_client.post_multipart = AsyncMock()

        with pytest.raises(ValueError, match=r"first\.bin.*second\.bin"):
            await create_document_attachments(
                mock_ctx,
                project_id="P",
                space_id="S",
                document_name="D",
                attachments=[
                    DocumentAttachmentSpec(file_path=str(first)),
                    DocumentAttachmentSpec(file_path=str(second)),
                ],
                dry_run=False,
            )
        mock_client.post_multipart.assert_not_called()


class TestCreateDocumentAttachmentsErrorMapping:
    """Error mapping per spec: 409 duplicate fileName, 404, auth, other."""

    async def test_conflict_raises_value_error_mentioning_list(
        self, mock_ctx: MagicMock, mock_client: AsyncMock, tmp_path: Path
    ) -> None:
        path = tmp_path / "a.png"
        path.write_bytes(b"a")
        mock_client.post_multipart = AsyncMock(
            side_effect=PolarionError("dup", status_code=409)
        )

        with pytest.raises(ValueError, match="list_document_attachments"):
            await create_document_attachments(
                mock_ctx,
                project_id="P",
                space_id="S",
                document_name="D",
                attachments=[DocumentAttachmentSpec(file_path=str(path))],
                dry_run=False,
            )

    async def test_not_found_raises_value_error(
        self, mock_ctx: MagicMock, mock_client: AsyncMock, tmp_path: Path
    ) -> None:
        path = tmp_path / "a.png"
        path.write_bytes(b"a")
        mock_client.post_multipart = AsyncMock(
            side_effect=PolarionNotFoundError("missing", status_code=404)
        )

        with pytest.raises(ValueError, match="list_documents"):
            await create_document_attachments(
                mock_ctx,
                project_id="P",
                space_id="S",
                document_name="D",
                attachments=[DocumentAttachmentSpec(file_path=str(path))],
                dry_run=False,
            )

    async def test_auth_error_raises_permission_error(
        self, mock_ctx: MagicMock, mock_client: AsyncMock, tmp_path: Path
    ) -> None:
        path = tmp_path / "a.png"
        path.write_bytes(b"a")
        mock_client.post_multipart = AsyncMock(
            side_effect=PolarionAuthError("forbidden", status_code=403)
        )

        with pytest.raises(PermissionError, match="POLARION_TOKEN"):
            await create_document_attachments(
                mock_ctx,
                project_id="P",
                space_id="S",
                document_name="D",
                attachments=[DocumentAttachmentSpec(file_path=str(path))],
                dry_run=False,
            )

    async def test_payload_too_large_raises_runtime_error_with_remedy(
        self, mock_ctx: MagicMock, mock_client: AsyncMock, tmp_path: Path
    ) -> None:
        path = tmp_path / "a.png"
        path.write_bytes(b"a")
        mock_client.post_multipart = AsyncMock(
            side_effect=PolarionError("too large", status_code=413)
        )

        with pytest.raises(RuntimeError, match=r"reduce.*size|split"):
            await create_document_attachments(
                mock_ctx,
                project_id="P",
                space_id="S",
                document_name="D",
                attachments=[DocumentAttachmentSpec(file_path=str(path))],
                dry_run=False,
            )

    async def test_other_polarion_error_raises_runtime_error(
        self, mock_ctx: MagicMock, mock_client: AsyncMock, tmp_path: Path
    ) -> None:
        path = tmp_path / "a.png"
        path.write_bytes(b"a")
        mock_client.post_multipart = AsyncMock(
            side_effect=PolarionError("boom", status_code=400)
        )

        with pytest.raises(RuntimeError, match="Failed to create document attachments"):
            await create_document_attachments(
                mock_ctx,
                project_id="P",
                space_id="S",
                document_name="D",
                attachments=[DocumentAttachmentSpec(file_path=str(path))],
                dry_run=False,
            )


class TestCreateDocumentAttachmentsFieldValidation:
    """``attachments`` 1..10 list bounds -- ``TypeAdapter`` rebuild (Field
    constraints bypass FastMCP JSON Schema gate on direct call).
    """

    @staticmethod
    def _adapter_for(param_name: str) -> TypeAdapter[object]:
        hints = get_type_hints(create_document_attachments)
        sig = inspect.signature(create_document_attachments)
        field_info = sig.parameters[param_name].default
        return TypeAdapter(Annotated[hints[param_name], field_info])

    def test_empty_list_rejected(self) -> None:
        with pytest.raises(ValidationError):
            self._adapter_for("attachments").validate_python([])

    def test_over_max_rejected(self) -> None:
        specs = [{"file_path": f"/data/{i}.png"} for i in range(11)]
        with pytest.raises(ValidationError):
            self._adapter_for("attachments").validate_python(specs)

    def test_at_max_accepted(self) -> None:
        specs = [{"file_path": f"/data/{i}.png"} for i in range(10)]
        validated = self._adapter_for("attachments").validate_python(specs)
        assert isinstance(validated, list)
        assert len(validated) == 10

    def test_at_minimum_accepted(self) -> None:
        validated = self._adapter_for("attachments").validate_python(
            [{"file_path": "/data/a.png"}]
        )
        assert len(validated) == 1


class TestBuildWorkItemAttachmentsPayload:
    """``_build_work_item_attachments_payload`` -- pure JSON:API body builder."""

    def test_file_name_defaults_to_basename(self) -> None:
        payload = _build_work_item_attachments_payload(
            [WorkItemAttachmentSpec(file_path="/data/foo/bar.png")]
        )
        attrs = payload["data"][0]["attributes"]  # type: ignore[index]
        assert attrs["fileName"] == "bar.png"  # type: ignore[index]

    def test_file_name_override_wins(self) -> None:
        payload = _build_work_item_attachments_payload(
            [
                WorkItemAttachmentSpec(
                    file_path="/data/foo/bar.png", file_name="renamed.png"
                )
            ]
        )
        attrs = payload["data"][0]["attributes"]  # type: ignore[index]
        assert attrs["fileName"] == "renamed.png"  # type: ignore[index]

    def test_title_omitted_when_none(self) -> None:
        payload = _build_work_item_attachments_payload(
            [WorkItemAttachmentSpec(file_path="/data/bar.png")]
        )
        attrs = payload["data"][0]["attributes"]  # type: ignore[index]
        assert "title" not in attrs  # type: ignore[operator]

    def test_title_included_when_set(self) -> None:
        payload = _build_work_item_attachments_payload(
            [WorkItemAttachmentSpec(file_path="/data/bar.png", title="A Chart")]
        )
        attrs = payload["data"][0]["attributes"]  # type: ignore[index]
        assert attrs["title"] == "A Chart"  # type: ignore[index]

    def test_type_is_workitem_attachments(self) -> None:
        payload = _build_work_item_attachments_payload(
            [WorkItemAttachmentSpec(file_path="/data/bar.png")]
        )
        assert payload["data"][0]["type"] == "workitem_attachments"  # type: ignore[index]

    def test_multiple_specs_preserve_order(self) -> None:
        payload = _build_work_item_attachments_payload(
            [
                WorkItemAttachmentSpec(file_path="/data/a.png"),
                WorkItemAttachmentSpec(file_path="/data/b.png"),
            ]
        )
        names = [
            item["attributes"]["fileName"]  # type: ignore[index]
            for item in payload["data"]  # type: ignore[union-attr]
        ]
        assert names == ["a.png", "b.png"]

    def test_duplicate_file_names_both_present(self) -> None:
        # Payload builder itself never dedups -- server assigns counter ids.
        payload = _build_work_item_attachments_payload(
            [
                WorkItemAttachmentSpec(file_path="/data/a.png", file_name="dup.png"),
                WorkItemAttachmentSpec(file_path="/data/b.png", file_name="dup.png"),
            ]
        )
        names = [
            item["attributes"]["fileName"]  # type: ignore[index]
            for item in payload["data"]  # type: ignore[union-attr]
        ]
        assert names == ["dup.png", "dup.png"]


class TestCreateWorkItemAttachmentsDryRun:
    """dry_run return preview without calling Polarion; files still validated."""

    async def test_dry_run_no_post_multipart_call(
        self, mock_ctx: MagicMock, mock_client: AsyncMock, tmp_path: Path
    ) -> None:
        file_path = tmp_path / "shot.png"
        file_path.write_bytes(b"pngdata")
        mock_client.post_multipart = AsyncMock()

        result = await create_work_item_attachments(
            mock_ctx,
            project_id="P",
            work_item_id="WI-1",
            attachments=[WorkItemAttachmentSpec(file_path=str(file_path))],
            dry_run=True,
        )

        mock_client.post_multipart.assert_not_called()
        assert result.created is False
        assert result.dry_run is True
        assert result.attachment_ids == []

    async def test_dry_run_payload_preview_shape(
        self, mock_ctx: MagicMock, mock_client: AsyncMock, tmp_path: Path
    ) -> None:
        file_path = tmp_path / "shot.png"
        file_path.write_bytes(b"pngdata")

        result = await create_work_item_attachments(
            mock_ctx,
            project_id="P",
            work_item_id="WI-1",
            attachments=[
                WorkItemAttachmentSpec(file_path=str(file_path), title="Shot")
            ],
            dry_run=True,
        )

        assert result.payload_preview is not None
        data = result.payload_preview["data"]
        assert isinstance(data, list)
        assert data[0]["attributes"]["fileName"] == "shot.png"  # type: ignore[index]
        assert data[0]["attributes"]["title"] == "Shot"  # type: ignore[index]
        assert result.payload_preview["files"] == [
            {"file_name": "shot.png", "size_bytes": 7}
        ]

    async def test_dry_run_still_validates_files(
        self, mock_ctx: MagicMock, mock_client: AsyncMock, tmp_path: Path
    ) -> None:
        missing = tmp_path / "missing.png"

        with pytest.raises(ValueError, match=str(missing)):
            await create_work_item_attachments(
                mock_ctx,
                project_id="P",
                work_item_id="WI-1",
                attachments=[WorkItemAttachmentSpec(file_path=str(missing))],
                dry_run=True,
            )


class TestCreateWorkItemAttachmentsHappyPath:
    """Successful upload extracts ordered counter-prefixed attachment ids."""

    async def test_returns_ordered_counter_prefixed_ids(
        self, mock_ctx: MagicMock, mock_client: AsyncMock, tmp_path: Path
    ) -> None:
        first = tmp_path / "a.txt"
        second = tmp_path / "b.txt"
        first.write_bytes(b"aaa")
        second.write_bytes(b"bbbb")
        mock_client.post_multipart = AsyncMock(
            return_value={
                "data": [
                    {
                        "type": "workitem_attachments",
                        "id": "PROJ/WI-1/1-a.txt",
                        "links": {"self": "..."},
                    },
                    {
                        "type": "workitem_attachments",
                        "id": "PROJ/WI-1/2-b.txt",
                        "links": {"self": "..."},
                    },
                ]
            }
        )

        result = await create_work_item_attachments(
            mock_ctx,
            project_id="PROJ",
            work_item_id="WI-1",
            attachments=[
                WorkItemAttachmentSpec(file_path=str(first)),
                WorkItemAttachmentSpec(file_path=str(second)),
            ],
            dry_run=False,
        )

        assert result.created is True
        assert result.dry_run is False
        assert result.attachment_ids == ["1-a.txt", "2-b.txt"]
        assert result.payload_preview is None

    async def test_post_multipart_called_with_ordered_parts(
        self, mock_ctx: MagicMock, mock_client: AsyncMock, tmp_path: Path
    ) -> None:
        first = tmp_path / "a.txt"
        second = tmp_path / "b.txt"
        first.write_bytes(b"aaa")
        second.write_bytes(b"bbbb")
        mock_client.post_multipart = AsyncMock(
            return_value={
                "data": [
                    {"type": "workitem_attachments", "id": "PROJ/WI-1/1-a.txt"},
                    {"type": "workitem_attachments", "id": "PROJ/WI-1/2-b.txt"},
                ]
            }
        )

        await create_work_item_attachments(
            mock_ctx,
            project_id="PROJ",
            work_item_id="WI-1",
            attachments=[
                WorkItemAttachmentSpec(file_path=str(first)),
                WorkItemAttachmentSpec(file_path=str(second)),
            ],
            dry_run=False,
        )

        call = mock_client.post_multipart.call_args
        assert call[0][0] == "/projects/PROJ/workitems/WI-1/attachments"
        resource = json.loads(call.kwargs["data"]["resource"])
        assert [item["attributes"]["fileName"] for item in resource["data"]] == [
            "a.txt",
            "b.txt",
        ]
        assert resource["data"][0]["type"] == "workitem_attachments"
        assert call.kwargs["files"] == [
            ("files", ("a.txt", b"aaa", "application/octet-stream")),
            ("files", ("b.txt", b"bbbb", "application/octet-stream")),
        ]

    async def test_no_attachment_ids_raises_runtime_error(
        self, mock_ctx: MagicMock, mock_client: AsyncMock, tmp_path: Path
    ) -> None:
        path = tmp_path / "a.txt"
        path.write_bytes(b"a")
        mock_client.post_multipart = AsyncMock(return_value={"data": []})

        with pytest.raises(RuntimeError, match="list_work_item_attachments"):
            await create_work_item_attachments(
                mock_ctx,
                project_id="P",
                work_item_id="WI-1",
                attachments=[WorkItemAttachmentSpec(file_path=str(path))],
                dry_run=False,
            )


class TestCreateWorkItemAttachmentsValidationBeforeCall:
    """Local pre-request validation blocks the POST; duplicate file_name
    is the one divergence -- it reaches the client call, no reject.
    """

    async def test_missing_file_raises_value_error_no_post(
        self, mock_ctx: MagicMock, mock_client: AsyncMock, tmp_path: Path
    ) -> None:
        missing = tmp_path / "missing.png"
        mock_client.post_multipart = AsyncMock()

        with pytest.raises(ValueError, match=str(missing)):
            await create_work_item_attachments(
                mock_ctx,
                project_id="P",
                work_item_id="WI-1",
                attachments=[WorkItemAttachmentSpec(file_path=str(missing))],
                dry_run=False,
            )
        mock_client.post_multipart.assert_not_called()

    async def test_directory_path_raises_value_error_no_post(
        self, mock_ctx: MagicMock, mock_client: AsyncMock, tmp_path: Path
    ) -> None:
        directory = tmp_path / "adir"
        directory.mkdir()
        mock_client.post_multipart = AsyncMock()

        with pytest.raises(ValueError, match="directory"):
            await create_work_item_attachments(
                mock_ctx,
                project_id="P",
                work_item_id="WI-1",
                attachments=[WorkItemAttachmentSpec(file_path=str(directory))],
                dry_run=False,
            )
        mock_client.post_multipart.assert_not_called()

    async def test_separator_file_name_raises_value_error_no_post(
        self, mock_ctx: MagicMock, mock_client: AsyncMock, tmp_path: Path
    ) -> None:
        path = tmp_path / "a.png"
        path.write_bytes(b"1")
        mock_client.post_multipart = AsyncMock()

        with pytest.raises(ValueError, match="path separator"):
            await create_work_item_attachments(
                mock_ctx,
                project_id="P",
                work_item_id="WI-1",
                attachments=[
                    WorkItemAttachmentSpec(file_path=str(path), file_name="sub/a.png")
                ],
                dry_run=False,
            )
        mock_client.post_multipart.assert_not_called()

    async def test_single_file_over_cap_raises_value_error_no_post(
        self, mock_ctx: MagicMock, mock_client: AsyncMock, tmp_path: Path
    ) -> None:
        big = tmp_path / "big.bin"
        with big.open("wb") as handle:
            handle.seek(_MAX_TOTAL_UPLOAD_BYTES)
            handle.write(b"\0")
        mock_client.post_multipart = AsyncMock()

        with pytest.raises(ValueError, match=r"big\.bin"):
            await create_work_item_attachments(
                mock_ctx,
                project_id="P",
                work_item_id="WI-1",
                attachments=[WorkItemAttachmentSpec(file_path=str(big))],
                dry_run=False,
            )
        mock_client.post_multipart.assert_not_called()

    async def test_total_over_cap_raises_value_error_no_post(
        self, mock_ctx: MagicMock, mock_client: AsyncMock, tmp_path: Path
    ) -> None:
        first = tmp_path / "first.bin"
        second = tmp_path / "second.bin"
        half_cap_over = _MAX_TOTAL_UPLOAD_BYTES // 2 + 1
        for path in (first, second):
            with path.open("wb") as handle:
                handle.seek(half_cap_over)
                handle.write(b"\0")
        mock_client.post_multipart = AsyncMock()

        with pytest.raises(ValueError, match=r"first\.bin.*second\.bin"):
            await create_work_item_attachments(
                mock_ctx,
                project_id="P",
                work_item_id="WI-1",
                attachments=[
                    WorkItemAttachmentSpec(file_path=str(first)),
                    WorkItemAttachmentSpec(file_path=str(second)),
                ],
                dry_run=False,
            )
        mock_client.post_multipart.assert_not_called()

    async def test_duplicate_file_name_allowed_reaches_client_call(
        self, mock_ctx: MagicMock, mock_client: AsyncMock, tmp_path: Path
    ) -> None:
        # Divergence from doc sibling: server assigns counter ids, in-call
        # dups accumulate legally -- no client-side reject.
        first = tmp_path / "a.png"
        second = tmp_path / "b.png"
        first.write_bytes(b"1")
        second.write_bytes(b"2")
        mock_client.post_multipart = AsyncMock(
            return_value={
                "data": [
                    {"type": "workitem_attachments", "id": "P/WI-1/1-dup.png"},
                    {"type": "workitem_attachments", "id": "P/WI-1/2-dup.png"},
                ]
            }
        )

        result = await create_work_item_attachments(
            mock_ctx,
            project_id="P",
            work_item_id="WI-1",
            attachments=[
                WorkItemAttachmentSpec(file_path=str(first), file_name="dup.png"),
                WorkItemAttachmentSpec(file_path=str(second), file_name="dup.png"),
            ],
            dry_run=False,
        )

        mock_client.post_multipart.assert_called_once()
        assert result.attachment_ids == ["1-dup.png", "2-dup.png"]


class TestCreateWorkItemAttachmentsErrorMapping:
    """Error mapping per spec: 404, auth, 413, generic -- NO 409 branch."""

    async def test_not_found_raises_value_error(
        self, mock_ctx: MagicMock, mock_client: AsyncMock, tmp_path: Path
    ) -> None:
        path = tmp_path / "a.png"
        path.write_bytes(b"a")
        mock_client.post_multipart = AsyncMock(
            side_effect=PolarionNotFoundError("missing", status_code=404)
        )

        with pytest.raises(ValueError, match="list_work_items"):
            await create_work_item_attachments(
                mock_ctx,
                project_id="P",
                work_item_id="WI-1",
                attachments=[WorkItemAttachmentSpec(file_path=str(path))],
                dry_run=False,
            )

    async def test_auth_error_raises_permission_error(
        self, mock_ctx: MagicMock, mock_client: AsyncMock, tmp_path: Path
    ) -> None:
        path = tmp_path / "a.png"
        path.write_bytes(b"a")
        mock_client.post_multipart = AsyncMock(
            side_effect=PolarionAuthError("forbidden", status_code=403)
        )

        with pytest.raises(PermissionError, match="POLARION_TOKEN"):
            await create_work_item_attachments(
                mock_ctx,
                project_id="P",
                work_item_id="WI-1",
                attachments=[WorkItemAttachmentSpec(file_path=str(path))],
                dry_run=False,
            )

    async def test_payload_too_large_raises_runtime_error_with_remedy(
        self, mock_ctx: MagicMock, mock_client: AsyncMock, tmp_path: Path
    ) -> None:
        path = tmp_path / "a.png"
        path.write_bytes(b"a")
        mock_client.post_multipart = AsyncMock(
            side_effect=PolarionError("too large", status_code=413)
        )

        with pytest.raises(RuntimeError, match=r"reduce.*size|split"):
            await create_work_item_attachments(
                mock_ctx,
                project_id="P",
                work_item_id="WI-1",
                attachments=[WorkItemAttachmentSpec(file_path=str(path))],
                dry_run=False,
            )

    async def test_other_polarion_error_raises_runtime_error(
        self, mock_ctx: MagicMock, mock_client: AsyncMock, tmp_path: Path
    ) -> None:
        path = tmp_path / "a.png"
        path.write_bytes(b"a")
        mock_client.post_multipart = AsyncMock(
            side_effect=PolarionError("boom", status_code=400)
        )

        with pytest.raises(
            RuntimeError, match="Failed to create work item attachments"
        ):
            await create_work_item_attachments(
                mock_ctx,
                project_id="P",
                work_item_id="WI-1",
                attachments=[WorkItemAttachmentSpec(file_path=str(path))],
                dry_run=False,
            )

    async def test_conflict_falls_through_to_generic_runtime_error(
        self, mock_ctx: MagicMock, mock_client: AsyncMock, tmp_path: Path
    ) -> None:
        # No 409 special-case on this endpoint -- server never conflicts on
        # dup fileName, so any 409 (however unexpected) maps to generic.
        path = tmp_path / "a.png"
        path.write_bytes(b"a")
        mock_client.post_multipart = AsyncMock(
            side_effect=PolarionError("dup", status_code=409)
        )

        with pytest.raises(
            RuntimeError, match="Failed to create work item attachments"
        ):
            await create_work_item_attachments(
                mock_ctx,
                project_id="P",
                work_item_id="WI-1",
                attachments=[WorkItemAttachmentSpec(file_path=str(path))],
                dry_run=False,
            )

    async def test_empty_echo_raises_runtime_error(
        self, mock_ctx: MagicMock, mock_client: AsyncMock, tmp_path: Path
    ) -> None:
        path = tmp_path / "a.png"
        path.write_bytes(b"a")
        mock_client.post_multipart = AsyncMock(return_value={"data": []})

        with pytest.raises(RuntimeError, match="list_work_item_attachments"):
            await create_work_item_attachments(
                mock_ctx,
                project_id="P",
                work_item_id="WI-1",
                attachments=[WorkItemAttachmentSpec(file_path=str(path))],
                dry_run=False,
            )


class TestCreateWorkItemAttachmentsFieldValidation:
    """``attachments`` 1..10 list bounds -- ``TypeAdapter`` rebuild (Field
    constraints bypass FastMCP JSON Schema gate on direct call).
    """

    @staticmethod
    def _adapter_for(param_name: str) -> TypeAdapter[object]:
        hints = get_type_hints(create_work_item_attachments)
        sig = inspect.signature(create_work_item_attachments)
        field_info = sig.parameters[param_name].default
        return TypeAdapter(Annotated[hints[param_name], field_info])

    def test_empty_list_rejected(self) -> None:
        with pytest.raises(ValidationError):
            self._adapter_for("attachments").validate_python([])

    def test_over_max_rejected(self) -> None:
        specs = [{"file_path": f"/data/{i}.png"} for i in range(11)]
        with pytest.raises(ValidationError):
            self._adapter_for("attachments").validate_python(specs)

    def test_at_max_accepted(self) -> None:
        specs = [{"file_path": f"/data/{i}.png"} for i in range(10)]
        validated = self._adapter_for("attachments").validate_python(specs)
        assert isinstance(validated, list)
        assert len(validated) == 10

    def test_at_minimum_accepted(self) -> None:
        validated = self._adapter_for("attachments").validate_python(
            [{"file_path": "/data/a.png"}]
        )
        assert len(validated) == 1

    def test_project_id_rejects_empty_string(self) -> None:
        with pytest.raises(ValidationError):
            self._adapter_for("project_id").validate_python("")

    def test_work_item_id_rejects_empty_string(self) -> None:
        with pytest.raises(ValidationError):
            self._adapter_for("work_item_id").validate_python("")
