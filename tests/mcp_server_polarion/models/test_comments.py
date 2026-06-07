"""Tests for document comment models in ``mcp_server_polarion.models.comments``."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from mcp_server_polarion.models import (
    DocumentComment,
    DocumentCommentsCreateResult,
    DocumentCommentSpec,
    DocumentCommentUpdateResult,
)


class TestDocumentComment:
    def test_top_level_defaults(self):
        c = DocumentComment(id="c1", created="2026-01-01T00:00:00Z")
        assert c.resolved is False
        assert c.text == ""
        assert c.text_format == "text/html"
        assert c.parent_comment_id is None
        assert c.child_comment_ids == []

    def test_reply_with_children(self):
        c = DocumentComment(
            id="c2",
            created="2026-01-01T00:00:00Z",
            parent_comment_id="c1",
            child_comment_ids=["c3", "c4"],
        )
        assert c.parent_comment_id == "c1"
        assert c.child_comment_ids == ["c3", "c4"]


class TestDocumentCommentSpec:
    def test_minimal(self):
        spec = DocumentCommentSpec(text="hello")
        assert spec.text == "hello"
        assert spec.text_format == "text/plain"
        assert spec.resolved is None
        assert spec.parent_comment_id is None

    def test_empty_text_rejected(self):
        with pytest.raises(ValidationError):
            DocumentCommentSpec(text="")


class TestDocumentCommentsCreateResult:
    def test_dry_run(self):
        result = DocumentCommentsCreateResult(
            created=False,
            dry_run=True,
            comment_ids=[],
            payload_preview={"data": [{"type": "document_comments"}]},
        )
        assert result.dry_run is True
        assert result.comment_ids == []
        assert result.payload_preview is not None


class TestDocumentCommentUpdateResult:
    def test_resolve(self):
        result = DocumentCommentUpdateResult(
            updated=True,
            dry_run=False,
            comment_id="proj/space/doc/c1",
            resolved=True,
            payload_preview=None,
        )
        assert result.comment_id == "proj/space/doc/c1"
        assert result.resolved is True
