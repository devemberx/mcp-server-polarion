"""Comment models — shared comment view, create specs, and update results."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Literal

from pydantic import BaseModel, Field


class Comment(BaseModel):
    """A single comment returned by the comment list tools."""

    id: str
    created: str
    resolved: bool = False
    title: str = ""
    text: str = ""
    text_format: Literal["text/html", "text/plain"] = "text/html"
    author_id: str | None = None
    parent_comment_id: str | None = None
    child_comment_ids: list[str] = Field(default_factory=list)


class CommentSpec(BaseModel):
    """A comment to create via the comment-create tools."""

    text: str = Field(min_length=1)
    text_format: Literal["text/html", "text/plain"] = "text/plain"
    title: str | None = Field(
        default=None,
        description="Comment title; ignored only for document comments.",
    )
    resolved: bool | None = None
    author_id: str | None = None
    parent_comment_id: str | None = None


class CommentsCreateResult(BaseModel):
    """Result of a comment-create operation."""

    created: bool
    dry_run: bool
    comment_ids: list[str]
    payload_preview: Mapping[str, object] | None


class CommentUpdateResult(BaseModel):
    """Shared result of a comment-resolve update, across all comment types."""

    updated: bool
    dry_run: bool
    comment_id: str | None
    resolved: bool
    payload_preview: Mapping[str, object] | None
