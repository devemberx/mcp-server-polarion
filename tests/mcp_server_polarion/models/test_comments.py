"""Tests for comment models in ``mcp_server_polarion.models.comments``."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from mcp_server_polarion.models import (
    Comment,
    CommentsCreateResult,
    CommentSpec,
    CommentUpdateResult,
    WorkItemCommentSpec,
)


class TestComment:
    def test_top_level_defaults(self):
        c = Comment(id="c1", created="2026-01-01T00:00:00Z")
        assert c.resolved is False
        assert c.title == ""
        assert c.text == ""
        assert c.text_format == "text/html"
        assert c.parent_comment_id is None
        assert c.child_comment_ids == []

    def test_reply_with_children(self):
        c = Comment(
            id="c2",
            created="2026-01-01T00:00:00Z",
            parent_comment_id="c1",
            child_comment_ids=["c3", "c4"],
        )
        assert c.parent_comment_id == "c1"
        assert c.child_comment_ids == ["c3", "c4"]


class TestCommentSpec:
    def test_minimal(self):
        spec = CommentSpec(text="hello")
        assert spec.text == "hello"
        assert spec.text_format == "text/plain"
        assert spec.resolved is None
        assert spec.parent_comment_id is None

    def test_no_title_field(self):
        """Base spec (document comments) has no title field at all."""
        assert not hasattr(CommentSpec(text="hello"), "title")

    def test_empty_text_rejected(self):
        with pytest.raises(ValidationError):
            CommentSpec(text="")


class TestWorkItemCommentSpec:
    def test_inherits_base_fields(self):
        spec = WorkItemCommentSpec(text="hello")
        assert spec.text == "hello"
        assert spec.text_format == "text/plain"
        assert spec.title is None

    def test_title(self):
        spec = WorkItemCommentSpec(text="hello", title="Heads up")
        assert spec.title == "Heads up"

    def test_empty_text_rejected(self):
        with pytest.raises(ValidationError):
            WorkItemCommentSpec(text="")


class TestCommentsCreateResult:
    def test_dry_run(self):
        result = CommentsCreateResult(
            created=False,
            dry_run=True,
            comment_ids=[],
            payload_preview={"data": [{"type": "workitem_comments"}]},
        )
        assert result.dry_run is True
        assert result.comment_ids == []
        assert result.payload_preview is not None


class TestCommentUpdateResult:
    def test_resolve(self):
        result = CommentUpdateResult(
            updated=True,
            dry_run=False,
            comment_id="proj/space/doc/c1",
            resolved=True,
            payload_preview=None,
        )
        assert result.comment_id == "proj/space/doc/c1"
        assert result.resolved is True

    def test_resolve_work_item_three_segment_id(self):
        result = CommentUpdateResult(
            updated=True,
            dry_run=False,
            comment_id="c1",
            resolved=False,
            payload_preview=None,
        )
        assert result.comment_id == "c1"
        assert result.resolved is False
