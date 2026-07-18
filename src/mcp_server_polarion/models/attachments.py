"""Attachment model — shared view for document (and future work item) attachments."""

from __future__ import annotations

from pydantic import BaseModel


class Attachment(BaseModel):
    """Single attachment from attachment list tools."""

    id: str
    file_name: str = ""
    title: str = ""
    length: int = 0
    updated: str = ""
    author_name: str = ""
