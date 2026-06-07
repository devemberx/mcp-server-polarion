"""Document comment models — comment views, create specs, and write results."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Literal

from pydantic import BaseModel, Field


class DocumentComment(BaseModel):
    """A single document comment returned by ``list_document_comments``.

    Comments form a tree: top-level comments have ``parent_comment_id=None``;
    replies link back via ``parent_comment_id`` and expose their own replies
    via ``child_comment_ids``. The endpoint returns a flat page — rebuild the
    thread client-side. ``text`` is verbatim (HTML is NOT sanitized, so it
    round-trips losslessly); ``text_format`` says whether it is HTML or plain.
    """

    id: str
    created: str = Field(description="ISO-8601 timestamp.")
    resolved: bool = False
    text: str = ""
    text_format: Literal["text/html", "text/plain"] = "text/html"
    author_id: str | None = None
    parent_comment_id: str | None = Field(
        default=None, description="None on top-level."
    )
    child_comment_ids: list[str] = Field(
        default_factory=list, description="Direct reply IDs in declaration order."
    )


class DocumentCommentSpec(BaseModel):
    """One comment to create via ``create_document_comments``.

    ``parent_comment_id`` is the short id from ``list_document_comments``
    (omit for top-level). Omit ``author_id`` to default to the token's user;
    omit ``resolved`` to let Polarion default to False.
    """

    text: str = Field(min_length=1)
    text_format: Literal["text/html", "text/plain"] = "text/plain"
    resolved: bool | None = None
    author_id: str | None = Field(default=None, description="Defaults to token user.")
    parent_comment_id: str | None = Field(
        default=None, description="Short id for replies; omit for top-level."
    )


class DocumentCommentsCreateResult(BaseModel):
    """Result of a ``create_document_comments`` operation."""

    created: bool
    dry_run: bool
    comment_ids: list[str] = Field(
        description="Short IDs in Polarion's return order; empty on dry-run."
    )
    payload_preview: Mapping[str, object] | None


class DocumentCommentUpdateResult(BaseModel):
    """Result of an ``update_document_comment`` operation."""

    updated: bool
    dry_run: bool
    comment_id: str | None = Field(
        description="Short comment id patched; None on dry-run."
    )
    resolved: bool = Field(description="The resolved value sent (or that would be).")
    payload_preview: Mapping[str, object] | None
