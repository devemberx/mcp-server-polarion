"""Attachment tool tests."""

from __future__ import annotations

import inspect
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
from mcp_server_polarion.models import Attachment, PaginatedResult
from mcp_server_polarion.tools.attachments import (
    get_document_attachment_content,
    list_document_attachments,
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

        with pytest.raises(ValueError, match="list_document_attachments"):
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
