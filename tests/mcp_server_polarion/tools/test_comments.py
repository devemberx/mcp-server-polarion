"""Tests for the comment tools."""

from __future__ import annotations

import inspect
from typing import Annotated, get_type_hints
from unittest.mock import AsyncMock, MagicMock

import pytest
from pydantic import TypeAdapter, ValidationError

from mcp_server_polarion.core.exceptions import (
    PolarionAuthError,
    PolarionError,
    PolarionNotFoundError,
)
from mcp_server_polarion.models import (
    Comment,
    CommentSpec,
    CommentUpdateResult,
    PaginatedResult,
    WorkItemCommentSpec,
)
from mcp_server_polarion.tools.comments import (
    _build_document_comment_update_payload,
    _build_document_comments_payload,
    _build_work_item_comment_update_payload,
    _build_work_item_comments_payload,
    create_document_comments,
    create_work_item_comments,
    list_document_comments,
    list_work_item_comments,
    update_document_comment,
    update_work_item_comment,
)


class TestListDocumentComments:
    """Tests for the ``list_document_comments`` tool."""

    async def test_returns_paginated_result(
        self, mock_ctx: MagicMock, mock_client: AsyncMock
    ) -> None:
        mock_client.get.return_value = {
            "data": [
                {
                    "type": "document_comments",
                    "id": "proj1/Design/SRS/cmt-1",
                    "attributes": {
                        "id": "cmt-1",
                        "created": "2026-04-01T12:00:00Z",
                        "resolved": False,
                        "text": {"type": "text/html", "value": "<p>Review me</p>"},
                    },
                    "relationships": {
                        "author": {
                            "data": {"type": "users", "id": "alice"},
                        },
                    },
                },
            ],
            "meta": {"totalCount": 1},
        }

        result = await list_document_comments(
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

        comment = result.items[0]
        assert isinstance(comment, Comment)
        assert comment.id == "cmt-1"
        assert comment.created == "2026-04-01T12:00:00Z"
        assert comment.resolved is False
        assert comment.text == "<p>Review me</p>"
        assert comment.text_format == "text/html"
        assert comment.author_id == "alice"
        assert comment.parent_comment_id is None
        assert comment.child_comment_ids == []

    async def test_extracts_thread_relationships(
        self, mock_ctx: MagicMock, mock_client: AsyncMock
    ) -> None:
        mock_client.get.return_value = {
            "data": [
                {
                    "type": "document_comments",
                    "id": "proj1/Design/SRS/cmt-reply",
                    "attributes": {
                        "id": "cmt-reply",
                        "created": "2026-04-02T09:00:00Z",
                        "resolved": True,
                        "text": {"type": "text/html", "value": "<p>thanks</p>"},
                    },
                    "relationships": {
                        "author": {"data": {"type": "users", "id": "bob"}},
                        "parentComment": {
                            "data": {
                                "type": "document_comments",
                                "id": "proj1/Design/SRS/cmt-root",
                            },
                        },
                        "childComments": {
                            "data": [
                                {
                                    "type": "document_comments",
                                    "id": "proj1/Design/SRS/cmt-grand-1",
                                },
                                {
                                    "type": "document_comments",
                                    "id": "proj1/Design/SRS/cmt-grand-2",
                                },
                            ],
                        },
                    },
                },
            ],
            "meta": {"totalCount": 1},
        }

        result = await list_document_comments(
            mock_ctx,
            project_id="proj1",
            space_id="Design",
            document_name="SRS",
            page_size=100,
            page_number=1,
        )

        comment = result.items[0]
        assert comment.resolved is True
        assert comment.parent_comment_id == "cmt-root"
        assert comment.child_comment_ids == ["cmt-grand-1", "cmt-grand-2"]

    async def test_handles_plain_text_format(
        self, mock_ctx: MagicMock, mock_client: AsyncMock
    ) -> None:
        mock_client.get.return_value = {
            "data": [
                {
                    "type": "document_comments",
                    "id": "proj1/Design/SRS/cmt-plain",
                    "attributes": {
                        "id": "cmt-plain",
                        "created": "2026-04-03T00:00:00Z",
                        "resolved": False,
                        "text": {
                            "type": "text/plain",
                            "value": "raw <not html> text",
                        },
                    },
                    "relationships": {},
                },
            ],
            "meta": {"totalCount": 1},
        }

        result = await list_document_comments(
            mock_ctx,
            project_id="proj1",
            space_id="Design",
            document_name="SRS",
            page_size=100,
            page_number=1,
        )

        comment = result.items[0]
        assert comment.text_format == "text/plain"
        assert comment.text == "raw <not html> text"

    async def test_missing_relationships_default_to_none(
        self, mock_ctx: MagicMock, mock_client: AsyncMock
    ) -> None:
        mock_client.get.return_value = {
            "data": [
                {
                    "type": "document_comments",
                    "id": "proj1/Design/SRS/cmt-empty",
                    "attributes": {
                        "id": "cmt-empty",
                        "created": "2026-04-04T00:00:00Z",
                        "resolved": False,
                    },
                },
            ],
            "meta": {"totalCount": 1},
        }

        result = await list_document_comments(
            mock_ctx,
            project_id="proj1",
            space_id="Design",
            document_name="SRS",
            page_size=100,
            page_number=1,
        )

        comment = result.items[0]
        assert comment.author_id is None
        assert comment.parent_comment_id is None
        assert comment.child_comment_ids == []
        assert comment.text == ""
        assert comment.text_format == "text/html"

    async def test_signals_has_more_when_total_exceeds_page(
        self, mock_ctx: MagicMock, mock_client: AsyncMock
    ) -> None:
        mock_client.get.return_value = {
            "data": [
                {
                    "type": "document_comments",
                    "id": f"proj1/Design/SRS/cmt-{i}",
                    "attributes": {
                        "id": f"cmt-{i}",
                        "created": "2026-04-01T00:00:00Z",
                        "resolved": False,
                        "text": {"type": "text/html", "value": "<p>x</p>"},
                    },
                    "relationships": {},
                }
                for i in range(2)
            ],
            "meta": {"totalCount": 5},
        }

        result = await list_document_comments(
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

        await list_document_comments(
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
            "/projects/proj1/spaces/_default/documents/SRS/comments"
        )
        params = calls[0][1]["params"]
        assert params["fields[document_comments]"] == (
            "created,resolved,text,author,parentComment,childComments"
        )
        assert params["include"] == "childComments"
        assert params["page[size]"] == 25
        assert params["page[number]"] == 3

    async def test_url_encodes_space_and_document_name(
        self, mock_ctx: MagicMock, mock_client: AsyncMock
    ) -> None:
        mock_client.get.return_value = {"data": []}

        await list_document_comments(
            mock_ctx,
            project_id="proj1",
            space_id="My Space",
            document_name="A/B Doc",
            page_size=100,
            page_number=1,
        )

        path = mock_client.get.call_args_list[0][0][0]
        assert path == (
            "/projects/proj1/spaces/My%20Space/documents/A%2FB%20Doc/comments"
        )

    async def test_not_found_raises_value_error(
        self, mock_ctx: MagicMock, mock_client: AsyncMock
    ) -> None:
        mock_client.get.side_effect = PolarionNotFoundError(
            "Not found",
            status_code=404,
        )

        with pytest.raises(ValueError, match="Design/SRS"):
            await list_document_comments(
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
            await list_document_comments(
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

        with pytest.raises(RuntimeError, match="Failed to list comments"):
            await list_document_comments(
                mock_ctx,
                project_id="proj1",
                space_id="Design",
                document_name="SRS",
                page_size=100,
                page_number=1,
            )


class TestListWorkItemComments:
    """Tests for the ``list_work_item_comments`` tool."""

    async def test_returns_paginated_result(
        self, mock_ctx: MagicMock, mock_client: AsyncMock
    ) -> None:
        mock_client.get.return_value = {
            "data": [
                {
                    "type": "workitem_comments",
                    "id": "proj1/MCPT-1/cmt-1",
                    "attributes": {
                        "id": "cmt-1",
                        "created": "2026-04-01T12:00:00Z",
                        "resolved": False,
                        "title": "Needs review",
                        "text": {"type": "text/html", "value": "<p>Review me</p>"},
                    },
                    "relationships": {
                        "author": {
                            "data": {"type": "users", "id": "alice"},
                        },
                    },
                },
            ],
            "meta": {"totalCount": 1},
        }

        result = await list_work_item_comments(
            mock_ctx,
            project_id="proj1",
            work_item_id="MCPT-1",
            page_size=100,
            page_number=1,
        )

        assert isinstance(result, PaginatedResult)
        assert result.total_count == 1
        assert result.page == 1
        assert result.page_size == 100
        assert result.has_more is False
        assert len(result.items) == 1

        comment = result.items[0]
        assert isinstance(comment, Comment)
        assert comment.id == "cmt-1"
        assert comment.created == "2026-04-01T12:00:00Z"
        assert comment.resolved is False
        assert comment.title == "Needs review"
        assert comment.text == "<p>Review me</p>"
        assert comment.text_format == "text/html"
        assert comment.author_id == "alice"
        assert comment.parent_comment_id is None
        assert comment.child_comment_ids == []

    async def test_extracts_thread_relationships(
        self, mock_ctx: MagicMock, mock_client: AsyncMock
    ) -> None:
        mock_client.get.return_value = {
            "data": [
                {
                    "type": "workitem_comments",
                    "id": "proj1/MCPT-1/cmt-reply",
                    "attributes": {
                        "id": "cmt-reply",
                        "created": "2026-04-02T09:00:00Z",
                        "resolved": True,
                        "text": {"type": "text/html", "value": "<p>thanks</p>"},
                    },
                    "relationships": {
                        "author": {"data": {"type": "users", "id": "bob"}},
                        "parentComment": {
                            "data": {
                                "type": "workitem_comments",
                                "id": "proj1/MCPT-1/cmt-root",
                            },
                        },
                        "childComments": {
                            "data": [
                                {
                                    "type": "workitem_comments",
                                    "id": "proj1/MCPT-1/cmt-grand-1",
                                },
                                {
                                    "type": "workitem_comments",
                                    "id": "proj1/MCPT-1/cmt-grand-2",
                                },
                            ],
                        },
                    },
                },
            ],
            "meta": {"totalCount": 1},
        }

        result = await list_work_item_comments(
            mock_ctx,
            project_id="proj1",
            work_item_id="MCPT-1",
            page_size=100,
            page_number=1,
        )

        comment = result.items[0]
        assert comment.resolved is True
        assert comment.parent_comment_id == "cmt-root"
        assert comment.child_comment_ids == ["cmt-grand-1", "cmt-grand-2"]

    async def test_handles_plain_text_format(
        self, mock_ctx: MagicMock, mock_client: AsyncMock
    ) -> None:
        mock_client.get.return_value = {
            "data": [
                {
                    "type": "workitem_comments",
                    "id": "proj1/MCPT-1/cmt-plain",
                    "attributes": {
                        "id": "cmt-plain",
                        "created": "2026-04-03T00:00:00Z",
                        "resolved": False,
                        "text": {
                            "type": "text/plain",
                            "value": "raw <not html> text",
                        },
                    },
                    "relationships": {},
                },
            ],
            "meta": {"totalCount": 1},
        }

        result = await list_work_item_comments(
            mock_ctx,
            project_id="proj1",
            work_item_id="MCPT-1",
            page_size=100,
            page_number=1,
        )

        comment = result.items[0]
        assert comment.text_format == "text/plain"
        assert comment.text == "raw <not html> text"

    async def test_missing_relationships_default_to_none(
        self, mock_ctx: MagicMock, mock_client: AsyncMock
    ) -> None:
        mock_client.get.return_value = {
            "data": [
                {
                    "type": "workitem_comments",
                    "id": "proj1/MCPT-1/cmt-empty",
                    "attributes": {
                        "id": "cmt-empty",
                        "created": "2026-04-04T00:00:00Z",
                        "resolved": False,
                    },
                },
            ],
            "meta": {"totalCount": 1},
        }

        result = await list_work_item_comments(
            mock_ctx,
            project_id="proj1",
            work_item_id="MCPT-1",
            page_size=100,
            page_number=1,
        )

        comment = result.items[0]
        assert comment.author_id is None
        assert comment.parent_comment_id is None
        assert comment.child_comment_ids == []
        assert comment.title == ""
        assert comment.text == ""
        assert comment.text_format == "text/html"

    async def test_signals_has_more_when_total_exceeds_page(
        self, mock_ctx: MagicMock, mock_client: AsyncMock
    ) -> None:
        mock_client.get.return_value = {
            "data": [
                {
                    "type": "workitem_comments",
                    "id": f"proj1/MCPT-1/cmt-{i}",
                    "attributes": {
                        "id": f"cmt-{i}",
                        "created": "2026-04-01T00:00:00Z",
                        "resolved": False,
                        "text": {"type": "text/html", "value": "<p>x</p>"},
                    },
                    "relationships": {},
                }
                for i in range(2)
            ],
            "meta": {"totalCount": 5},
        }

        result = await list_work_item_comments(
            mock_ctx,
            project_id="proj1",
            work_item_id="MCPT-1",
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

        await list_work_item_comments(
            mock_ctx,
            project_id="proj1",
            work_item_id="MCPT-1",
            page_size=25,
            page_number=3,
        )

        calls = mock_client.get.call_args_list
        assert len(calls) == 1
        assert calls[0][0][0] == "/projects/proj1/workitems/MCPT-1/comments"
        params = calls[0][1]["params"]
        assert params["fields[workitem_comments]"] == (
            "created,resolved,title,text,author,parentComment,childComments"
        )
        assert params["include"] == "childComments"
        assert params["page[size]"] == 25
        assert params["page[number]"] == 3

    async def test_url_encodes_work_item_id(
        self, mock_ctx: MagicMock, mock_client: AsyncMock
    ) -> None:
        mock_client.get.return_value = {"data": []}

        await list_work_item_comments(
            mock_ctx,
            project_id="proj1",
            work_item_id="MCPT 1/2",
            page_size=100,
            page_number=1,
        )

        path = mock_client.get.call_args_list[0][0][0]
        assert path == "/projects/proj1/workitems/MCPT%201%2F2/comments"

    async def test_not_found_raises_value_error(
        self, mock_ctx: MagicMock, mock_client: AsyncMock
    ) -> None:
        mock_client.get.side_effect = PolarionNotFoundError(
            "Not found",
            status_code=404,
        )

        with pytest.raises(ValueError, match="MCPT-1"):
            await list_work_item_comments(
                mock_ctx,
                project_id="proj1",
                work_item_id="MCPT-1",
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
            await list_work_item_comments(
                mock_ctx,
                project_id="proj1",
                work_item_id="MCPT-1",
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

        with pytest.raises(RuntimeError, match="Failed to list comments"):
            await list_work_item_comments(
                mock_ctx,
                project_id="proj1",
                work_item_id="MCPT-1",
                page_size=100,
                page_number=1,
            )


class TestBuildDocumentCommentsPayload:
    """Unit tests for the private ``_build_document_comments_payload`` helper."""

    def test_single_plain_text_spec(self) -> None:
        payload = _build_document_comments_payload(
            specs=[CommentSpec(text="hello")],
            project_id="Proj",
            space_id="Space",
            document_name="Doc",
        )
        data = payload["data"]
        assert isinstance(data, list)
        assert len(data) == 1
        item = data[0]
        assert isinstance(item, dict)
        assert item["type"] == "document_comments"
        attrs = item["attributes"]
        assert isinstance(attrs, dict)
        assert attrs["text"] == {"type": "text/plain", "value": "hello"}
        assert "resolved" not in attrs
        assert "relationships" not in item

    def test_multiple_specs_produce_multiple_items(self) -> None:
        payload = _build_document_comments_payload(
            specs=[
                CommentSpec(text="first"),
                CommentSpec(text="second"),
            ],
            project_id="P",
            space_id="S",
            document_name="D",
        )
        assert len(payload["data"]) == 2  # type: ignore[arg-type]

    def test_resolved_true_in_attributes(self) -> None:
        payload = _build_document_comments_payload(
            specs=[CommentSpec(text="t", resolved=True)],
            project_id="P",
            space_id="S",
            document_name="D",
        )
        attrs = payload["data"][0]["attributes"]  # type: ignore[index]
        assert attrs["resolved"] is True  # type: ignore[index]

    def test_resolved_false_in_attributes(self) -> None:
        """Explicit False must be sent, not silently omitted like None."""
        payload = _build_document_comments_payload(
            specs=[CommentSpec(text="t", resolved=False)],
            project_id="P",
            space_id="S",
            document_name="D",
        )
        attrs = payload["data"][0]["attributes"]  # type: ignore[index]
        assert attrs["resolved"] is False  # type: ignore[index]

    def test_resolved_none_omits_key(self) -> None:
        payload = _build_document_comments_payload(
            specs=[CommentSpec(text="t", resolved=None)],
            project_id="P",
            space_id="S",
            document_name="D",
        )
        attrs = payload["data"][0]["attributes"]  # type: ignore[index]
        assert "resolved" not in attrs  # type: ignore[operator]

    def test_author_relationship(self) -> None:
        payload = _build_document_comments_payload(
            specs=[CommentSpec(text="t", author_id="alice")],
            project_id="P",
            space_id="S",
            document_name="D",
        )
        item = payload["data"][0]  # type: ignore[index]
        assert isinstance(item, dict)
        assert item["relationships"]["author"] == {  # type: ignore[index]
            "data": {"id": "alice", "type": "users"}
        }

    def test_parent_comment_full_path_composed(self) -> None:
        """Short parent_comment_id is expanded to the full 4-segment path."""
        payload = _build_document_comments_payload(
            specs=[CommentSpec(text="t", parent_comment_id="c1")],
            project_id="Proj",
            space_id="Space",
            document_name="Doc",
        )
        item = payload["data"][0]  # type: ignore[index]
        assert isinstance(item, dict)
        rel = item["relationships"]["parentComment"]  # type: ignore[index]
        assert rel == {"data": {"id": "Proj/Space/Doc/c1", "type": "document_comments"}}

    def test_both_relationships_present(self) -> None:
        payload = _build_document_comments_payload(
            specs=[CommentSpec(text="t", author_id="bob", parent_comment_id="c5")],
            project_id="P",
            space_id="S",
            document_name="D",
        )
        item = payload["data"][0]  # type: ignore[index]
        assert isinstance(item, dict)
        rels = item["relationships"]
        assert "author" in rels  # type: ignore[operator]
        assert "parentComment" in rels  # type: ignore[operator]

    def test_html_format_preserved(self) -> None:
        payload = _build_document_comments_payload(
            specs=[CommentSpec(text="<b>bold</b>", text_format="text/html")],
            project_id="P",
            space_id="S",
            document_name="D",
        )
        text_field = payload["data"][0]["attributes"]["text"]  # type: ignore[index]
        assert text_field["type"] == "text/html"  # type: ignore[index]

    def test_payload_wrapped_in_array(self) -> None:
        payload = _build_document_comments_payload(
            specs=[CommentSpec(text="t")],
            project_id="P",
            space_id="S",
            document_name="D",
        )
        assert isinstance(payload["data"], list)
        assert len(payload["data"]) == 1  # type: ignore[arg-type]

    def test_no_title_for_documents(self) -> None:
        """Base CommentSpec has no title field, so it never reaches attributes."""
        spec = CommentSpec(text="t")
        assert not hasattr(spec, "title")
        payload = _build_document_comments_payload(
            specs=[spec],
            project_id="P",
            space_id="S",
            document_name="D",
        )
        attrs = payload["data"][0]["attributes"]  # type: ignore[index]
        assert "title" not in attrs  # type: ignore[operator]


class TestCreateDocumentCommentsDryRun:
    """Verify dry_run returns preview without calling Polarion."""

    async def test_dry_run_no_post_call(
        self, mock_ctx: MagicMock, mock_client: AsyncMock
    ) -> None:
        result = await create_document_comments(
            mock_ctx,
            project_id="P",
            space_id="S",
            document_name="D",
            comments=[CommentSpec(text="hello")],
            dry_run=True,
        )
        mock_client.post.assert_not_called()
        assert result.dry_run is True
        assert result.created is False
        assert result.comment_ids == []

    async def test_dry_run_payload_preview_populated(
        self, mock_ctx: MagicMock, mock_client: AsyncMock
    ) -> None:
        result = await create_document_comments(
            mock_ctx,
            project_id="P",
            space_id="S",
            document_name="D",
            comments=[
                CommentSpec(text="first"),
                CommentSpec(text="second"),
            ],
            dry_run=True,
        )
        assert result.payload_preview is not None
        assert isinstance(result.payload_preview["data"], list)
        assert len(result.payload_preview["data"]) == 2  # type: ignore[arg-type]

    async def test_dry_run_with_relationships(
        self, mock_ctx: MagicMock, mock_client: AsyncMock
    ) -> None:
        result = await create_document_comments(
            mock_ctx,
            project_id="P",
            space_id="S",
            document_name="D",
            comments=[
                CommentSpec(text="reply", author_id="bob", parent_comment_id="c5")
            ],
            dry_run=True,
        )
        assert result.payload_preview is not None
        item = result.payload_preview["data"][0]  # type: ignore[index]
        assert isinstance(item, dict)
        assert "author" in item["relationships"]  # type: ignore[index]
        assert "parentComment" in item["relationships"]  # type: ignore[index]


class TestCreateDocumentCommentsHappyPath:
    """Verify successful creation extracts and returns short comment IDs."""

    async def test_returns_short_comment_ids(
        self, mock_ctx: MagicMock, mock_client: AsyncMock
    ) -> None:
        mock_client.post.return_value = {
            "data": [
                {"type": "document_comments", "id": "p/s/d/c42"},
                {"type": "document_comments", "id": "p/s/d/c43"},
            ]
        }
        result = await create_document_comments(
            mock_ctx,
            project_id="p",
            space_id="s",
            document_name="d",
            comments=[
                CommentSpec(text="first"),
                CommentSpec(text="second"),
            ],
            dry_run=False,
        )
        assert result.created is True
        assert result.dry_run is False
        assert result.comment_ids == ["c42", "c43"]
        assert result.payload_preview is None

    async def test_post_called_with_correct_path(
        self, mock_ctx: MagicMock, mock_client: AsyncMock
    ) -> None:
        mock_client.post.return_value = {
            "data": [{"type": "document_comments", "id": "Proj/_default/Doc/c1"}]
        }
        await create_document_comments(
            mock_ctx,
            project_id="Proj",
            space_id="_default",
            document_name="Doc",
            comments=[CommentSpec(text="hello")],
            dry_run=False,
        )
        call_args = mock_client.post.call_args
        expected_path = "/projects/Proj/spaces/_default/documents/Doc/comments"
        assert call_args[0][0] == expected_path

    async def test_path_url_encodes_spaces(
        self, mock_ctx: MagicMock, mock_client: AsyncMock
    ) -> None:
        mock_client.post.return_value = {
            "data": [{"type": "document_comments", "id": "P/My%20Space/My%20Doc/c1"}]
        }
        await create_document_comments(
            mock_ctx,
            project_id="P",
            space_id="My Space",
            document_name="My Doc",
            comments=[CommentSpec(text="hi")],
            dry_run=False,
        )
        call_args = mock_client.post.call_args
        assert "My%20Space" in call_args[0][0]
        assert "My%20Doc" in call_args[0][0]

    async def test_post_body_multiple_items(
        self, mock_ctx: MagicMock, mock_client: AsyncMock
    ) -> None:
        mock_client.post.return_value = {
            "data": [
                {"type": "document_comments", "id": "P/S/D/c1"},
                {"type": "document_comments", "id": "P/S/D/c2"},
            ]
        }
        await create_document_comments(
            mock_ctx,
            project_id="P",
            space_id="S",
            document_name="D",
            comments=[
                CommentSpec(text="one"),
                CommentSpec(text="two"),
            ],
            dry_run=False,
        )
        body = mock_client.post.call_args[1]["json"]  # type: ignore[index]
        assert len(body["data"]) == 2  # type: ignore[index]

    async def test_resolved_true_sent_in_payload(
        self, mock_ctx: MagicMock, mock_client: AsyncMock
    ) -> None:
        mock_client.post.return_value = {
            "data": [{"type": "document_comments", "id": "P/S/D/c1"}]
        }
        await create_document_comments(
            mock_ctx,
            project_id="P",
            space_id="S",
            document_name="D",
            comments=[CommentSpec(text="done", resolved=True)],
            dry_run=False,
        )
        body = mock_client.post.call_args[1]["json"]
        assert body["data"][0]["attributes"]["resolved"] is True


class TestCreateDocumentCommentsErrors:
    """Verify domain exceptions map to the correct public exceptions."""

    async def test_auth_error_raises_permission_error(
        self, mock_ctx: MagicMock, mock_client: AsyncMock
    ) -> None:
        mock_client.post.side_effect = PolarionAuthError("auth", status_code=401)
        with pytest.raises(PermissionError, match="POLARION_TOKEN"):
            await create_document_comments(
                mock_ctx,
                project_id="P",
                space_id="S",
                document_name="D",
                comments=[CommentSpec(text="hi")],
                dry_run=False,
            )

    async def test_not_found_raises_value_error(
        self, mock_ctx: MagicMock, mock_client: AsyncMock
    ) -> None:
        mock_client.post.side_effect = PolarionNotFoundError(
            "not found", status_code=404
        )
        with pytest.raises(ValueError, match="list_documents"):
            await create_document_comments(
                mock_ctx,
                project_id="P",
                space_id="S",
                document_name="D",
                comments=[CommentSpec(text="hi")],
                dry_run=False,
            )

    async def test_other_polarion_error_raises_runtime_error(
        self, mock_ctx: MagicMock, mock_client: AsyncMock
    ) -> None:
        mock_client.post.side_effect = PolarionError("boom", status_code=500)
        with pytest.raises(RuntimeError, match="boom"):
            await create_document_comments(
                mock_ctx,
                project_id="P",
                space_id="S",
                document_name="D",
                comments=[CommentSpec(text="hi")],
                dry_run=False,
            )

    async def test_empty_response_raises_runtime_error(
        self, mock_ctx: MagicMock, mock_client: AsyncMock
    ) -> None:
        """201 with no IDs must raise rather than silently return created=True."""
        mock_client.post.return_value = {}
        with pytest.raises(RuntimeError, match="no comment IDs"):
            await create_document_comments(
                mock_ctx,
                project_id="P",
                space_id="S",
                document_name="D",
                comments=[CommentSpec(text="hi")],
                dry_run=False,
            )


class TestCreateDocumentCommentsFieldValidation:
    """Field constraints on create_document_comments / CommentSpec — direct
    calls bypass FastMCP's JSON Schema gate, so rebuild a ``TypeAdapter`` per
    parameter to prove the constraint is wired.
    """

    @staticmethod
    def _adapter_for(param_name: str) -> TypeAdapter[object]:
        hints = get_type_hints(create_document_comments)
        sig = inspect.signature(create_document_comments)
        field_info = sig.parameters[param_name].default
        return TypeAdapter(Annotated[hints[param_name], field_info])

    def test_space_id_rejects_empty(self) -> None:
        with pytest.raises(ValidationError):
            self._adapter_for("space_id").validate_python("")

    def test_space_id_accepts_non_empty(self) -> None:
        assert self._adapter_for("space_id").validate_python("_default") == "_default"

    def test_document_name_rejects_empty(self) -> None:
        with pytest.raises(ValidationError):
            self._adapter_for("document_name").validate_python("")

    def test_document_name_accepts_non_empty(self) -> None:
        assert self._adapter_for("document_name").validate_python("MySRS") == "MySRS"

    def test_spec_text_rejects_empty(self) -> None:
        with pytest.raises(ValidationError):
            CommentSpec(text="")

    def test_spec_text_accepts_non_empty(self) -> None:
        spec = CommentSpec(text="hello")
        assert spec.text == "hello"

    def test_spec_default_text_format_is_plain(self) -> None:
        spec = CommentSpec(text="hello")
        assert spec.text_format == "text/plain"

    def test_comments_rejects_empty_list(self) -> None:
        with pytest.raises(ValidationError):
            self._adapter_for("comments").validate_python([])

    def test_comments_accepts_non_empty_list(self) -> None:
        specs = [{"text": "hello"}]
        result = self._adapter_for("comments").validate_python(specs)
        assert isinstance(result, list)
        assert len(result) == 1


class TestBuildWorkItemCommentsPayload:
    """Unit tests for the private ``_build_work_item_comments_payload`` helper."""

    def test_single_plain_text_spec(self) -> None:
        payload = _build_work_item_comments_payload(
            specs=[WorkItemCommentSpec(text="hello")],
            project_id="Proj",
            work_item_id="MCPT-1",
        )
        data = payload["data"]
        assert isinstance(data, list)
        assert len(data) == 1
        item = data[0]
        assert isinstance(item, dict)
        assert item["type"] == "workitem_comments"
        attrs = item["attributes"]
        assert isinstance(attrs, dict)
        assert attrs["text"] == {"type": "text/plain", "value": "hello"}
        assert "title" not in attrs
        assert "resolved" not in attrs
        assert "relationships" not in item

    def test_title_in_attributes(self) -> None:
        payload = _build_work_item_comments_payload(
            specs=[WorkItemCommentSpec(text="t", title="Heads up")],
            project_id="P",
            work_item_id="MCPT-1",
        )
        attrs = payload["data"][0]["attributes"]  # type: ignore[index]
        assert attrs["title"] == "Heads up"  # type: ignore[index]

    def test_resolved_false_in_attributes(self) -> None:
        """Explicit False must be sent, not silently omitted like None."""
        payload = _build_work_item_comments_payload(
            specs=[WorkItemCommentSpec(text="t", resolved=False)],
            project_id="P",
            work_item_id="MCPT-1",
        )
        attrs = payload["data"][0]["attributes"]  # type: ignore[index]
        assert attrs["resolved"] is False  # type: ignore[index]

    def test_resolved_none_omits_key(self) -> None:
        payload = _build_work_item_comments_payload(
            specs=[WorkItemCommentSpec(text="t", resolved=None)],
            project_id="P",
            work_item_id="MCPT-1",
        )
        attrs = payload["data"][0]["attributes"]  # type: ignore[index]
        assert "resolved" not in attrs  # type: ignore[operator]

    def test_author_relationship(self) -> None:
        payload = _build_work_item_comments_payload(
            specs=[WorkItemCommentSpec(text="t", author_id="alice")],
            project_id="P",
            work_item_id="MCPT-1",
        )
        item = payload["data"][0]  # type: ignore[index]
        assert isinstance(item, dict)
        assert item["relationships"]["author"] == {  # type: ignore[index]
            "data": {"id": "alice", "type": "users"}
        }

    def test_parent_comment_full_path_composed(self) -> None:
        """Short parent_comment_id is expanded to the full 3-segment path."""
        payload = _build_work_item_comments_payload(
            specs=[WorkItemCommentSpec(text="t", parent_comment_id="c1")],
            project_id="Proj",
            work_item_id="MCPT-1",
        )
        item = payload["data"][0]  # type: ignore[index]
        assert isinstance(item, dict)
        rel = item["relationships"]["parentComment"]  # type: ignore[index]
        assert rel == {"data": {"id": "Proj/MCPT-1/c1", "type": "workitem_comments"}}

    def test_multiple_specs_produce_multiple_items(self) -> None:
        payload = _build_work_item_comments_payload(
            specs=[
                WorkItemCommentSpec(text="first"),
                WorkItemCommentSpec(text="second"),
            ],
            project_id="P",
            work_item_id="MCPT-1",
        )
        assert len(payload["data"]) == 2  # type: ignore[arg-type]


class TestCreateWorkItemCommentsDryRun:
    """Verify dry_run returns preview without calling Polarion."""

    async def test_dry_run_no_post_call(
        self, mock_ctx: MagicMock, mock_client: AsyncMock
    ) -> None:
        result = await create_work_item_comments(
            mock_ctx,
            project_id="P",
            work_item_id="MCPT-1",
            comments=[WorkItemCommentSpec(text="hello")],
            dry_run=True,
        )
        mock_client.post.assert_not_called()
        assert result.dry_run is True
        assert result.created is False
        assert result.comment_ids == []

    async def test_dry_run_payload_preview_populated(
        self, mock_ctx: MagicMock, mock_client: AsyncMock
    ) -> None:
        result = await create_work_item_comments(
            mock_ctx,
            project_id="P",
            work_item_id="MCPT-1",
            comments=[
                WorkItemCommentSpec(text="first", title="T"),
                WorkItemCommentSpec(text="second"),
            ],
            dry_run=True,
        )
        assert result.payload_preview is not None
        assert isinstance(result.payload_preview["data"], list)
        assert len(result.payload_preview["data"]) == 2  # type: ignore[arg-type]


class TestCreateWorkItemCommentsHappyPath:
    """Verify successful creation extracts and returns short comment IDs."""

    async def test_returns_short_comment_ids(
        self, mock_ctx: MagicMock, mock_client: AsyncMock
    ) -> None:
        mock_client.post.return_value = {
            "data": [
                {"type": "workitem_comments", "id": "p/MCPT-1/c42"},
                {"type": "workitem_comments", "id": "p/MCPT-1/c43"},
            ]
        }
        result = await create_work_item_comments(
            mock_ctx,
            project_id="p",
            work_item_id="MCPT-1",
            comments=[
                WorkItemCommentSpec(text="first"),
                WorkItemCommentSpec(text="second"),
            ],
            dry_run=False,
        )
        assert result.created is True
        assert result.dry_run is False
        assert result.comment_ids == ["c42", "c43"]
        assert result.payload_preview is None

    async def test_post_called_with_correct_path(
        self, mock_ctx: MagicMock, mock_client: AsyncMock
    ) -> None:
        mock_client.post.return_value = {
            "data": [{"type": "workitem_comments", "id": "Proj/MCPT-1/c1"}]
        }
        await create_work_item_comments(
            mock_ctx,
            project_id="Proj",
            work_item_id="MCPT-1",
            comments=[WorkItemCommentSpec(text="hello")],
            dry_run=False,
        )
        call_args = mock_client.post.call_args
        assert call_args[0][0] == "/projects/Proj/workitems/MCPT-1/comments"

    async def test_path_url_encodes_special_chars(
        self, mock_ctx: MagicMock, mock_client: AsyncMock
    ) -> None:
        mock_client.post.return_value = {
            "data": [{"type": "workitem_comments", "id": "P/MY%20WI/c1"}]
        }
        await create_work_item_comments(
            mock_ctx,
            project_id="P",
            work_item_id="MY WI",
            comments=[WorkItemCommentSpec(text="hi")],
            dry_run=False,
        )
        assert "MY%20WI" in mock_client.post.call_args[0][0]

    async def test_title_sent_in_payload(
        self, mock_ctx: MagicMock, mock_client: AsyncMock
    ) -> None:
        mock_client.post.return_value = {
            "data": [{"type": "workitem_comments", "id": "P/MCPT-1/c1"}]
        }
        await create_work_item_comments(
            mock_ctx,
            project_id="P",
            work_item_id="MCPT-1",
            comments=[WorkItemCommentSpec(text="t", title="Heads up")],
            dry_run=False,
        )
        body = mock_client.post.call_args[1]["json"]
        assert body["data"][0]["attributes"]["title"] == "Heads up"


class TestCreateWorkItemCommentsErrors:
    """Verify domain exceptions map to the correct public exceptions."""

    async def test_auth_error_raises_permission_error(
        self, mock_ctx: MagicMock, mock_client: AsyncMock
    ) -> None:
        mock_client.post.side_effect = PolarionAuthError("auth", status_code=401)
        with pytest.raises(PermissionError, match="POLARION_TOKEN"):
            await create_work_item_comments(
                mock_ctx,
                project_id="P",
                work_item_id="MCPT-1",
                comments=[WorkItemCommentSpec(text="hi")],
                dry_run=False,
            )

    async def test_not_found_raises_value_error(
        self, mock_ctx: MagicMock, mock_client: AsyncMock
    ) -> None:
        mock_client.post.side_effect = PolarionNotFoundError(
            "not found", status_code=404
        )
        with pytest.raises(ValueError, match="list_work_items"):
            await create_work_item_comments(
                mock_ctx,
                project_id="P",
                work_item_id="MCPT-1",
                comments=[WorkItemCommentSpec(text="hi")],
                dry_run=False,
            )

    async def test_other_polarion_error_raises_runtime_error(
        self, mock_ctx: MagicMock, mock_client: AsyncMock
    ) -> None:
        mock_client.post.side_effect = PolarionError("boom", status_code=500)
        with pytest.raises(RuntimeError, match="boom"):
            await create_work_item_comments(
                mock_ctx,
                project_id="P",
                work_item_id="MCPT-1",
                comments=[WorkItemCommentSpec(text="hi")],
                dry_run=False,
            )

    async def test_empty_response_raises_runtime_error(
        self, mock_ctx: MagicMock, mock_client: AsyncMock
    ) -> None:
        """201 with no IDs must raise rather than silently return created=True."""
        mock_client.post.return_value = {}
        with pytest.raises(RuntimeError, match="no comment IDs"):
            await create_work_item_comments(
                mock_ctx,
                project_id="P",
                work_item_id="MCPT-1",
                comments=[WorkItemCommentSpec(text="hi")],
                dry_run=False,
            )


class TestCreateWorkItemCommentsFieldValidation:
    """Field constraints on create_work_item_comments — direct calls bypass
    FastMCP's JSON Schema gate, so rebuild a ``TypeAdapter`` per parameter to
    prove the constraint is wired.
    """

    @staticmethod
    def _adapter_for(param_name: str) -> TypeAdapter[object]:
        hints = get_type_hints(create_work_item_comments)
        sig = inspect.signature(create_work_item_comments)
        field_info = sig.parameters[param_name].default
        return TypeAdapter(Annotated[hints[param_name], field_info])

    def test_project_id_rejects_empty(self) -> None:
        with pytest.raises(ValidationError):
            self._adapter_for("project_id").validate_python("")

    def test_work_item_id_rejects_empty(self) -> None:
        with pytest.raises(ValidationError):
            self._adapter_for("work_item_id").validate_python("")

    def test_work_item_id_accepts_non_empty(self) -> None:
        assert self._adapter_for("work_item_id").validate_python("MCPT-1") == "MCPT-1"

    def test_comments_rejects_empty_list(self) -> None:
        with pytest.raises(ValidationError):
            self._adapter_for("comments").validate_python([])

    def test_comments_accepts_non_empty_list(self) -> None:
        result = self._adapter_for("comments").validate_python([{"text": "hello"}])
        assert isinstance(result, list)
        assert len(result) == 1


class TestBuildDocumentCommentUpdatePayload:
    """Unit tests for _build_document_comment_update_payload (no I/O)."""

    def _build(
        self,
        *,
        project_id: str = "Proj",
        space_id: str = "Space",
        document_name: str = "Doc",
        comment_id: str = "c42",
        resolved: bool = True,
    ) -> dict:  # type: ignore[type-arg]
        return _build_document_comment_update_payload(
            project_id=project_id,
            space_id=space_id,
            document_name=document_name,
            comment_id=comment_id,
            resolved=resolved,
        )

    def test_payload_is_dict_not_list(self) -> None:
        payload = self._build()
        assert isinstance(payload["data"], dict)
        assert not isinstance(payload["data"], list)

    def test_type_is_document_comments(self) -> None:
        payload = self._build()
        assert payload["data"]["type"] == "document_comments"  # type: ignore[index]

    def test_resolved_true_included(self) -> None:
        payload = self._build(resolved=True)
        assert payload["data"]["attributes"]["resolved"] is True  # type: ignore[index]

    def test_resolved_false_included(self) -> None:
        payload = self._build(resolved=False)
        assert payload["data"]["attributes"]["resolved"] is False  # type: ignore[index]

    def test_full_id_composed_from_four_segments(self) -> None:
        payload = self._build(
            project_id="P",
            space_id="S",
            document_name="D",
            comment_id="c42",
        )
        assert payload["data"]["id"] == "P/S/D/c42"  # type: ignore[index]

    def test_space_default_value_in_id(self) -> None:
        payload = self._build(space_id="_default")
        assert "_default" in str(payload["data"]["id"])  # type: ignore[index]


class TestUpdateDocumentCommentDryRun:
    """Dry-run path must not call client.patch."""

    async def test_dry_run_skips_patch(
        self, mock_ctx: MagicMock, mock_client: AsyncMock
    ) -> None:
        await update_document_comment(
            mock_ctx,
            project_id="Proj",
            space_id="Space",
            document_name="Doc",
            comment_id="c42",
            resolved=True,
            dry_run=True,
        )
        mock_client.patch.assert_not_called()

    async def test_dry_run_result_flags(
        self, mock_ctx: MagicMock, mock_client: AsyncMock
    ) -> None:
        result = await update_document_comment(
            mock_ctx,
            project_id="Proj",
            space_id="Space",
            document_name="Doc",
            comment_id="c42",
            resolved=True,
            dry_run=True,
        )
        assert isinstance(result, CommentUpdateResult)
        assert result.dry_run is True
        assert result.updated is False

    async def test_dry_run_comment_id_is_none(
        self, mock_ctx: MagicMock, mock_client: AsyncMock
    ) -> None:
        result = await update_document_comment(
            mock_ctx,
            project_id="Proj",
            space_id="Space",
            document_name="Doc",
            comment_id="c42",
            resolved=True,
            dry_run=True,
        )
        assert result.comment_id is None

    async def test_dry_run_payload_preview_populated(
        self, mock_ctx: MagicMock, mock_client: AsyncMock
    ) -> None:
        result = await update_document_comment(
            mock_ctx,
            project_id="Proj",
            space_id="Space",
            document_name="Doc",
            comment_id="c42",
            resolved=True,
            dry_run=True,
        )
        assert result.payload_preview is not None
        data = result.payload_preview["data"]
        assert data["type"] == "document_comments"  # type: ignore[index]
        assert data["attributes"]["resolved"] is True  # type: ignore[index]
        assert data["id"] == "Proj/Space/Doc/c42"  # type: ignore[index]

    async def test_dry_run_resolved_echoed_true(
        self, mock_ctx: MagicMock, mock_client: AsyncMock
    ) -> None:
        result = await update_document_comment(
            mock_ctx,
            project_id="Proj",
            space_id="Space",
            document_name="Doc",
            comment_id="c42",
            resolved=True,
            dry_run=True,
        )
        assert result.resolved is True

    async def test_dry_run_resolved_echoed_false(
        self, mock_ctx: MagicMock, mock_client: AsyncMock
    ) -> None:
        result = await update_document_comment(
            mock_ctx,
            project_id="Proj",
            space_id="Space",
            document_name="Doc",
            comment_id="c42",
            resolved=False,
            dry_run=True,
        )
        assert result.resolved is False


class TestUpdateDocumentCommentHappyPath:
    """Successful PATCH path (204 No Content)."""

    async def test_patch_called_with_correct_path(
        self, mock_ctx: MagicMock, mock_client: AsyncMock
    ) -> None:
        mock_client.patch.return_value = {}
        await update_document_comment(
            mock_ctx,
            project_id="Proj",
            space_id="Space",
            document_name="Doc",
            comment_id="c42",
            resolved=True,
            dry_run=False,
        )
        path = mock_client.patch.call_args[0][0]
        assert path == "/projects/Proj/spaces/Space/documents/Doc/comments/c42"

    async def test_patch_body_resolved_true(
        self, mock_ctx: MagicMock, mock_client: AsyncMock
    ) -> None:
        mock_client.patch.return_value = {}
        await update_document_comment(
            mock_ctx,
            project_id="Proj",
            space_id="Space",
            document_name="Doc",
            comment_id="c42",
            resolved=True,
            dry_run=False,
        )
        body = mock_client.patch.call_args[1]["json"]
        assert body["data"]["attributes"]["resolved"] is True

    async def test_patch_body_resolved_false(
        self, mock_ctx: MagicMock, mock_client: AsyncMock
    ) -> None:
        mock_client.patch.return_value = {}
        await update_document_comment(
            mock_ctx,
            project_id="Proj",
            space_id="Space",
            document_name="Doc",
            comment_id="c42",
            resolved=False,
            dry_run=False,
        )
        body = mock_client.patch.call_args[1]["json"]
        assert body["data"]["attributes"]["resolved"] is False

    async def test_returns_updated_true(
        self, mock_ctx: MagicMock, mock_client: AsyncMock
    ) -> None:
        mock_client.patch.return_value = {}
        result = await update_document_comment(
            mock_ctx,
            project_id="Proj",
            space_id="Space",
            document_name="Doc",
            comment_id="c42",
            resolved=True,
            dry_run=False,
        )
        assert isinstance(result, CommentUpdateResult)
        assert result.updated is True
        assert result.dry_run is False
        assert result.comment_id == "c42"
        assert result.resolved is True
        assert result.payload_preview is None

    async def test_path_url_encodes_segments(
        self, mock_ctx: MagicMock, mock_client: AsyncMock
    ) -> None:
        mock_client.patch.return_value = {}
        await update_document_comment(
            mock_ctx,
            project_id="My Proj",
            space_id="My Space",
            document_name="My Doc",
            comment_id="c 1",
            resolved=True,
            dry_run=False,
        )
        path = mock_client.patch.call_args[0][0]
        assert "My%20Proj" in path
        assert "My%20Space" in path
        assert "My%20Doc" in path
        assert "c%201" in path


class TestUpdateDocumentCommentErrors:
    """Exception mapping for PATCH failures."""

    async def test_auth_error_raises_permission_error(
        self, mock_ctx: MagicMock, mock_client: AsyncMock
    ) -> None:
        mock_client.patch.side_effect = PolarionAuthError("auth", status_code=401)
        with pytest.raises(PermissionError, match="POLARION_TOKEN"):
            await update_document_comment(
                mock_ctx,
                project_id="Proj",
                space_id="Space",
                document_name="Doc",
                comment_id="c42",
                resolved=True,
                dry_run=False,
            )

    async def test_not_found_raises_value_error(
        self, mock_ctx: MagicMock, mock_client: AsyncMock
    ) -> None:
        mock_client.patch.side_effect = PolarionNotFoundError(
            "not found", status_code=404
        )
        with pytest.raises(ValueError, match="list_document_comments"):
            await update_document_comment(
                mock_ctx,
                project_id="Proj",
                space_id="Space",
                document_name="Doc",
                comment_id="c42",
                resolved=True,
                dry_run=False,
            )

    async def test_other_error_raises_runtime_error(
        self, mock_ctx: MagicMock, mock_client: AsyncMock
    ) -> None:
        mock_client.patch.side_effect = PolarionError("boom", status_code=500)
        with pytest.raises(RuntimeError, match="boom"):
            await update_document_comment(
                mock_ctx,
                project_id="Proj",
                space_id="Space",
                document_name="Doc",
                comment_id="c42",
                resolved=True,
                dry_run=False,
            )


class TestBuildWorkItemCommentUpdatePayload:
    """Unit tests for _build_work_item_comment_update_payload (no I/O)."""

    def _build(
        self,
        *,
        project_id: str = "Proj",
        work_item_id: str = "MCPT-001",
        comment_id: str = "c42",
        resolved: bool = True,
    ) -> dict:  # type: ignore[type-arg]
        return _build_work_item_comment_update_payload(
            project_id=project_id,
            work_item_id=work_item_id,
            comment_id=comment_id,
            resolved=resolved,
        )

    def test_payload_is_dict_not_list(self) -> None:
        payload = self._build()
        assert isinstance(payload["data"], dict)
        assert not isinstance(payload["data"], list)

    def test_type_is_workitem_comments(self) -> None:
        payload = self._build()
        assert payload["data"]["type"] == "workitem_comments"  # type: ignore[index]

    def test_resolved_true_included(self) -> None:
        payload = self._build(resolved=True)
        assert payload["data"]["attributes"]["resolved"] is True  # type: ignore[index]

    def test_resolved_false_included(self) -> None:
        payload = self._build(resolved=False)
        assert payload["data"]["attributes"]["resolved"] is False  # type: ignore[index]

    def test_full_id_composed_from_three_segments(self) -> None:
        payload = self._build(
            project_id="P",
            work_item_id="W",
            comment_id="c42",
        )
        assert payload["data"]["id"] == "P/W/c42"  # type: ignore[index]


class TestUpdateWorkItemCommentDryRun:
    """Dry-run path must not call client.patch."""

    async def test_dry_run_skips_patch(
        self, mock_ctx: MagicMock, mock_client: AsyncMock
    ) -> None:
        await update_work_item_comment(
            mock_ctx,
            project_id="Proj",
            work_item_id="MCPT-001",
            comment_id="c42",
            resolved=True,
            dry_run=True,
        )
        mock_client.patch.assert_not_called()

    async def test_dry_run_result_flags(
        self, mock_ctx: MagicMock, mock_client: AsyncMock
    ) -> None:
        result = await update_work_item_comment(
            mock_ctx,
            project_id="Proj",
            work_item_id="MCPT-001",
            comment_id="c42",
            resolved=True,
            dry_run=True,
        )
        assert isinstance(result, CommentUpdateResult)
        assert result.dry_run is True
        assert result.updated is False

    async def test_dry_run_comment_id_is_none(
        self, mock_ctx: MagicMock, mock_client: AsyncMock
    ) -> None:
        result = await update_work_item_comment(
            mock_ctx,
            project_id="Proj",
            work_item_id="MCPT-001",
            comment_id="c42",
            resolved=True,
            dry_run=True,
        )
        assert result.comment_id is None

    async def test_dry_run_payload_preview_populated(
        self, mock_ctx: MagicMock, mock_client: AsyncMock
    ) -> None:
        result = await update_work_item_comment(
            mock_ctx,
            project_id="Proj",
            work_item_id="MCPT-001",
            comment_id="c42",
            resolved=True,
            dry_run=True,
        )
        assert result.payload_preview is not None
        data = result.payload_preview["data"]
        assert data["type"] == "workitem_comments"  # type: ignore[index]
        assert data["attributes"]["resolved"] is True  # type: ignore[index]
        assert data["id"] == "Proj/MCPT-001/c42"  # type: ignore[index]


class TestUpdateWorkItemCommentHappyPath:
    """Successful PATCH path (204 No Content)."""

    async def test_patch_called_with_correct_path(
        self, mock_ctx: MagicMock, mock_client: AsyncMock
    ) -> None:
        mock_client.patch.return_value = {}
        await update_work_item_comment(
            mock_ctx,
            project_id="Proj",
            work_item_id="MCPT-001",
            comment_id="c42",
            resolved=True,
            dry_run=False,
        )
        path = mock_client.patch.call_args[0][0]
        assert path == "/projects/Proj/workitems/MCPT-001/comments/c42"

    async def test_patch_body_three_segment_id_and_type(
        self, mock_ctx: MagicMock, mock_client: AsyncMock
    ) -> None:
        mock_client.patch.return_value = {}
        await update_work_item_comment(
            mock_ctx,
            project_id="Proj",
            work_item_id="MCPT-001",
            comment_id="c42",
            resolved=True,
            dry_run=False,
        )
        body = mock_client.patch.call_args[1]["json"]
        assert body["data"]["id"] == "Proj/MCPT-001/c42"
        assert body["data"]["type"] == "workitem_comments"

    async def test_returns_updated_true(
        self, mock_ctx: MagicMock, mock_client: AsyncMock
    ) -> None:
        mock_client.patch.return_value = {}
        result = await update_work_item_comment(
            mock_ctx,
            project_id="Proj",
            work_item_id="MCPT-001",
            comment_id="c42",
            resolved=False,
            dry_run=False,
        )
        assert isinstance(result, CommentUpdateResult)
        assert result.updated is True
        assert result.dry_run is False
        assert result.comment_id == "c42"
        assert result.resolved is False
        assert result.payload_preview is None

    async def test_path_url_encodes_segments(
        self, mock_ctx: MagicMock, mock_client: AsyncMock
    ) -> None:
        mock_client.patch.return_value = {}
        await update_work_item_comment(
            mock_ctx,
            project_id="My Proj",
            work_item_id="WI 1",
            comment_id="c 1",
            resolved=True,
            dry_run=False,
        )
        path = mock_client.patch.call_args[0][0]
        assert "My%20Proj" in path
        assert "WI%201" in path
        assert "c%201" in path


class TestUpdateWorkItemCommentErrors:
    """Exception mapping for PATCH failures."""

    async def test_auth_error_raises_permission_error(
        self, mock_ctx: MagicMock, mock_client: AsyncMock
    ) -> None:
        mock_client.patch.side_effect = PolarionAuthError("auth", status_code=401)
        with pytest.raises(PermissionError, match="POLARION_TOKEN"):
            await update_work_item_comment(
                mock_ctx,
                project_id="Proj",
                work_item_id="MCPT-001",
                comment_id="c42",
                resolved=True,
                dry_run=False,
            )

    async def test_not_found_raises_value_error(
        self, mock_ctx: MagicMock, mock_client: AsyncMock
    ) -> None:
        mock_client.patch.side_effect = PolarionNotFoundError(
            "not found", status_code=404
        )
        with pytest.raises(ValueError, match="list_work_item_comments"):
            await update_work_item_comment(
                mock_ctx,
                project_id="Proj",
                work_item_id="MCPT-001",
                comment_id="c42",
                resolved=True,
                dry_run=False,
            )

    async def test_other_error_raises_runtime_error(
        self, mock_ctx: MagicMock, mock_client: AsyncMock
    ) -> None:
        mock_client.patch.side_effect = PolarionError("boom", status_code=500)
        with pytest.raises(RuntimeError, match="boom"):
            await update_work_item_comment(
                mock_ctx,
                project_id="Proj",
                work_item_id="MCPT-001",
                comment_id="c42",
                resolved=True,
                dry_run=False,
            )
