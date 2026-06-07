"""Cross-tool invariant: every read tool URL-encodes each path segment."""

from __future__ import annotations

from collections.abc import Iterator
from unittest.mock import AsyncMock, MagicMock

import pytest

from mcp_server_polarion.tools._shared import cache as _cache_mod
from mcp_server_polarion.tools.comments import list_document_comments
from mcp_server_polarion.tools.documents import (
    get_document,
    list_documents,
    read_document_parts,
)
from mcp_server_polarion.tools.links import list_work_item_links
from mcp_server_polarion.tools.work_items import (
    get_work_item,
    list_work_items,
)


class TestReadPathEncoding:
    """Every read tool that builds a URL must URL-encode each path segment.

    Without encoding, a project/space/document/work-item id containing a
    space, slash, or other reserved character would either generate a
    malformed URL or — worse — allow path traversal (``../``) into a
    different Polarion resource. These tests pin the encoding behavior so
    a future refactor cannot silently drop ``encode_path_segment()`` from
    one of the read paths.
    """

    @pytest.fixture(autouse=True)
    def _clear_doc_cache(self) -> Iterator[None]:
        _cache_mod._document_list_cache.clear()
        yield
        _cache_mod._document_list_cache.clear()

    async def test_list_documents_encodes_project_id(
        self, mock_ctx: MagicMock, mock_client: AsyncMock
    ) -> None:
        mock_client.get.return_value = {"data": [], "meta": {"totalCount": 0}}

        await list_documents(
            mock_ctx,
            project_id="My Proj",
            page_size=100,
            page_number=1,
        )

        call_path = mock_client.get.call_args[0][0]
        assert call_path == "/projects/My%20Proj/workitems"

    async def test_get_document_encodes_all_segments(
        self, mock_ctx: MagicMock, mock_client: AsyncMock
    ) -> None:
        mock_client.get.return_value = {
            "data": {"attributes": {"title": "T", "id": "D"}}
        }

        await get_document(
            mock_ctx,
            project_id="My Proj",
            space_id="My Space",
            document_name="My Doc",
        )

        call_path = mock_client.get.call_args[0][0]
        assert call_path == ("/projects/My%20Proj/spaces/My%20Space/documents/My%20Doc")

    async def test_read_document_parts_encodes_all_segments(
        self, mock_ctx: MagicMock, mock_client: AsyncMock
    ) -> None:
        mock_client.get.return_value = {"data": [], "meta": {"totalCount": 0}}

        await read_document_parts(
            mock_ctx,
            project_id="My Proj",
            space_id="My Space",
            document_name="My Doc",
            page_size=100,
            page_number=1,
        )

        call_path = mock_client.get.call_args[0][0]
        assert call_path == (
            "/projects/My%20Proj/spaces/My%20Space/documents/My%20Doc/parts"
        )

    async def test_list_work_items_encodes_project_id(
        self, mock_ctx: MagicMock, mock_client: AsyncMock
    ) -> None:
        mock_client.get.return_value = {"data": [], "meta": {"totalCount": 0}}

        await list_work_items(
            mock_ctx,
            project_id="My Proj",
            query=None,
            page_size=100,
            page_number=1,
        )

        call_path = mock_client.get.call_args[0][0]
        assert call_path == "/projects/My%20Proj/workitems"

    async def test_get_work_item_encodes_project_id_and_work_item_id(
        self, mock_ctx: MagicMock, mock_client: AsyncMock
    ) -> None:
        mock_client.get.return_value = {
            "data": {
                "type": "workitems",
                "id": "My Proj/MCPT-1",
                "attributes": {
                    "title": "T",
                    "type": "requirement",
                    "status": "draft",
                },
                "relationships": {},
            }
        }

        await get_work_item(
            mock_ctx,
            project_id="My Proj",
            work_item_id="MCPT 1",
        )

        call_path = mock_client.get.call_args[0][0]
        assert call_path == "/projects/My%20Proj/workitems/MCPT%201"

    async def test_list_work_item_links_forward_encodes_all_segments(
        self, mock_ctx: MagicMock, mock_client: AsyncMock
    ) -> None:
        mock_client.get.return_value = {"data": [], "meta": {"totalCount": 0}}

        await list_work_item_links(
            mock_ctx,
            project_id="My Proj",
            work_item_id="MCPT 1",
            direction="forward",
            page_size=100,
            page_number=1,
        )

        call_path = mock_client.get.call_args[0][0]
        assert call_path == ("/projects/My%20Proj/workitems/MCPT%201/linkedworkitems")

    async def test_list_work_item_links_back_encodes_project_id(
        self, mock_ctx: MagicMock, mock_client: AsyncMock
    ) -> None:
        # work_item_id is rejected by the Lucene allowlist before encoding;
        # use a safe id and verify only project_id is encoded.
        mock_client.get.return_value = {"data": [], "meta": {"totalCount": 0}}

        await list_work_item_links(
            mock_ctx,
            project_id="My Proj",
            work_item_id="MCPT-1",
            direction="back",
            page_size=100,
            page_number=1,
        )

        call_path = mock_client.get.call_args[0][0]
        assert call_path == "/projects/My%20Proj/workitems"

    async def test_list_document_comments_encodes_all_segments(
        self, mock_ctx: MagicMock, mock_client: AsyncMock
    ) -> None:
        mock_client.get.return_value = {"data": [], "meta": {"totalCount": 0}}

        await list_document_comments(
            mock_ctx,
            project_id="My Proj",
            space_id="My Space",
            document_name="My Doc",
            page_size=100,
            page_number=1,
        )

        call_path = mock_client.get.call_args[0][0]
        assert call_path == (
            "/projects/My%20Proj/spaces/My%20Space/documents/My%20Doc/comments"
        )
