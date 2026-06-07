"""Document models — summaries, details, parts, and write results."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Literal

from pydantic import BaseModel, Field


class DocumentSummary(BaseModel):
    """Summary of a Polarion document returned by ``list_documents``."""

    space_id: str
    document_name: str


class DocumentDetail(BaseModel):
    """Full details of a Polarion document returned by ``get_document``.

    ``content_html`` is the round-trip pair for
    ``update_document(home_page_content_html=...)`` — populated only when
    ``include_homepage_content_html=True``, and the inline prose only
    (heading text and embedded work-item bodies live in separate work items,
    so ``read_document`` is the assembled-body view). ``custom_fields`` keeps
    rich-text values as ``{'type': 'text/html', 'value': '<...>'}`` so the
    shape round-trips back unchanged.
    """

    title: str
    type: str = ""
    status: str = ""
    content_html: str = Field(
        default="", description="Raw HTML body; empty unless the read flag was True."
    )
    custom_fields: dict[str, object] = Field(default_factory=dict)


class DocumentPart(BaseModel):
    """A single part (heading or work item) within a Polarion document.

    Field population varies by ``type``:

    * ``heading`` — text in ``title``, depth in ``level``, ``work_item_*``
      point at the heading work item.
    * ``workitem`` — body in ``description`` (Markdown), metadata on
      ``work_item_*``, ``content`` empty.
    * ``normal`` / ``wikiblock`` — body in ``content`` (Markdown).
    * ``toc`` / ``tof`` / ``page_break`` — widget placeholders, body fields
      empty (``tof`` / ``page_break`` inferred from the part-ID prefix).

    Use ``id`` as ``previous_part_id`` / ``next_part_id`` for
    ``move_work_item_to_document``; ``work_item_id`` plugs into
    ``get_work_item`` / ``list_work_item_links``.
    """

    id: str = Field(description="Short part id (e.g. 'workitem_MCPT-042').")
    title: str
    content: str = Field(description="Markdown body; empty unless body lives here.")
    type: Literal[
        "heading",
        "workitem",
        "normal",
        "toc",
        "wikiblock",
        "tof",
        "page_break",
    ] = Field(description="Part type; see class docstring for body-field mapping.")
    level: int = Field(description="Heading level 1-4 for headings; 0 otherwise.")
    description: str = Field(
        default="", description="Work item body as Markdown; only on 'workitem' parts."
    )
    work_item_id: str = Field(default="", description="Empty unless workitem/heading.")
    work_item_type: str = ""
    work_item_status: str = ""
    external: bool = Field(
        default=False, description="Re-used from another project (read-only)."
    )
    outline_number: str = Field(
        default="", description="e.g. '1.2.3'; empty for prose/widgets."
    )
    next_part_id: str = ""


class DocumentReadResult(BaseModel):
    """Rendered Markdown view of one page of document parts (``read_document``).

    Read-only synthesis: it cannot be fed back to any write tool. For
    round-trip body editing, fetch raw source via
    ``get_document(include_homepage_content_html=True)``. ``part_count``
    counts parts consumed (including widget placeholders that emit nothing),
    so use it for pagination accounting, not chunk count.
    """

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
    document_name: str | None = Field(
        description="Module name of the new document; None on dry-run."
    )
    payload_preview: Mapping[str, object] | None


class DocumentUpdateResult(BaseModel):
    """Result of an ``update_document`` operation."""

    updated: bool
    dry_run: bool
    payload_preview: Mapping[str, object] | None
