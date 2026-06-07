"""Document comment models — comment views, create specs, and write results."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Literal

from pydantic import BaseModel, Field


class DocumentComment(BaseModel):
    """A single document comment returned by ``list_document_comments``."""

    id: str
    created: str
    resolved: bool = False
    text: str = ""
    text_format: Literal["text/html", "text/plain"] = "text/html"
    author_id: str | None = None
    parent_comment_id: str | None = None
    child_comment_ids: list[str] = Field(default_factory=list)


class DocumentCommentSpec(BaseModel):
    """One comment to create via ``create_document_comments``."""

    text: str = Field(min_length=1)
    text_format: Literal["text/html", "text/plain"] = "text/plain"
    resolved: bool | None = None
    author_id: str | None = None
    parent_comment_id: str | None = None


class DocumentCommentsCreateResult(BaseModel):
    """Result of a ``create_document_comments`` operation."""

    created: bool
    dry_run: bool
    comment_ids: list[str]
    payload_preview: Mapping[str, object] | None


class DocumentCommentUpdateResult(BaseModel):
    """Result of an ``update_document_comment`` operation."""

    updated: bool
    dry_run: bool
    comment_id: str | None
    resolved: bool
    payload_preview: Mapping[str, object] | None
