"""Document models — summaries, details, parts, and write results."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Literal

from pydantic import BaseModel, Field


class DocumentSummary(BaseModel):
    """Summary of a Polarion document returned by ``list_documents``."""

    space_id: str
    document_name: str
    type: str = ""
    status: str = ""
    updated: str = ""
    author: str = ""
    last_updated_by: str = ""


class DocumentDetail(BaseModel):
    """Full details of a Polarion document returned by ``get_document``."""

    title: str
    type: str = ""
    status: str = ""
    updated: str = ""
    author: str = ""
    last_updated_by: str = ""
    content_html: str = ""
    custom_fields: dict[str, object] = Field(default_factory=dict)


class DocumentPart(BaseModel):
    """A single part (heading or work item) within a Polarion document."""

    id: str
    title: str
    content: str
    type: Literal[
        "heading",
        "workitem",
        "normal",
        "toc",
        "wikiblock",
        "tof",
        "page_break",
    ]
    level: int
    description: str = ""
    work_item_id: str = ""
    work_item_type: str = ""
    work_item_status: str = ""
    external: bool = False
    outline_number: str = ""
    next_part_id: str = ""


class DocumentReadResult(BaseModel):
    """Rendered Markdown view of one page of document parts (``read_document``)."""

    content: str
    part_count: int
    page: int
    page_size: int
    total_parts: int
    has_more: bool = False


class DocumentCreateResult(BaseModel):
    """Result of a ``create_document`` operation."""

    created: bool
    dry_run: bool
    document_name: str | None
    payload_preview: Mapping[str, object] | None


class DocumentUpdateResult(BaseModel):
    """Result of an ``update_document`` operation."""

    updated: bool
    dry_run: bool
    payload_preview: Mapping[str, object] | None
