"""Attachment model — shared view for document and work item attachments."""

from __future__ import annotations

from collections.abc import Mapping

from pydantic import BaseModel, ConfigDict


class Attachment(BaseModel):
    """Single attachment from attachment list tools."""

    id: str
    file_name: str = ""
    title: str = ""
    length: int = 0
    updated: str = ""
    author_name: str = ""


class DocumentAttachmentSpec(BaseModel):
    """One file to upload as a document attachment."""

    # LLM input model: reject typo keys, not silent-drop.
    model_config = ConfigDict(extra="forbid")

    file_path: str
    file_name: str | None = None
    title: str | None = None


class WorkItemAttachmentSpec(BaseModel):
    """One file to upload as a work item attachment; file_name is not the id."""

    # LLM input model: reject typo keys, not silent-drop.
    model_config = ConfigDict(extra="forbid")

    file_path: str
    file_name: str | None = None
    title: str | None = None


class AttachmentsCreateResult(BaseModel):
    """Attachment-create result, shared by document and work item upload
    tools.
    """

    created: bool
    dry_run: bool
    attachment_ids: list[str]
    payload_preview: Mapping[str, object] | None
