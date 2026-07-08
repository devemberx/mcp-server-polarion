"""Comment models — shared view, create specs, update results."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class Comment(BaseModel):
    """Single comment from comment list tools."""

    id: str
    created: str
    resolved: bool = False
    title: str = ""
    text: str = ""
    text_format: Literal["text/html", "text/plain"] = "text/html"
    author_name: str = ""
    parent_comment_id: str | None = None
    child_comment_ids: list[str] = Field(default_factory=list)


class CommentSpec(BaseModel):
    """Common create fields; base for type-specific specs."""

    # LLM input model: reject typo keys, not silent-drop.
    model_config = ConfigDict(extra="forbid")

    text: str = Field(min_length=1)
    text_format: Literal["text/html", "text/plain"] = "text/plain"
    resolved: bool | None = None
    parent_comment_id: str | None = None


class WorkItemCommentSpec(CommentSpec):
    """Work item comment to create; adds ``title`` (document comments have none)."""

    title: str | None = Field(default=None, description="Comment heading.")


class CommentsCreateResult(BaseModel):
    """Comment-create result."""

    created: bool
    dry_run: bool
    comment_ids: list[str]
    payload_preview: Mapping[str, object] | None


class CommentUpdateResult(BaseModel):
    """Comment-resolve update result, shared across comment types."""

    updated: bool
    dry_run: bool
    comment_id: str | None
    resolved: bool
    payload_preview: Mapping[str, object] | None
