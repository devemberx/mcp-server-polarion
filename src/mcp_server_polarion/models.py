"""Pydantic models for MCP tool inputs and outputs.

Every tool accepts and returns Pydantic models — never raw ``dict``.
Class docstrings and ``Field(description=...)`` ship in the JSON Schema, so
keep them tight: omit a description when the field name + type say everything
(e.g. ``items``, ``page``, ``id``), and keep one only for non-obvious semantics
(units, empty-conditions, round-trip / read-only contracts).

Models are organised into three categories:

* **Read models** — returned by read tools (summaries, details, paginated
  results).
* **Write-result models** — returned by write tools (create/update
  confirmations with ``dry_run`` support).
* **Generic wrappers** — ``PaginatedResult[T]`` used by all list tools.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Final, Literal

from pydantic import BaseModel, Field, model_validator

# Recursive JSON-safe alias for internal payload builders. Result-model
# fields below intentionally surface payload previews as
# `Mapping[str, object]` instead of `dict[str, JsonValue]`: the recursive
# alias emits a `$defs/JsonValue` self-reference that the FastMCP client's
# `json_schema_to_type` cannot rebuild, producing an unresolved
# `ForwardRef('Root')` TypeAdapter and noisy errors on every write call.
type JsonValue = (
    str | int | float | bool | None | list[JsonValue] | dict[str, JsonValue]
)

# Caps a single body payload so a prompt-injected caller cannot ship a
# multi-megabyte blob to Polarion. Observed real document bodies stay under
# ~30 KB, so 2 MiB leaves ~70x headroom. This is a per-item bound; a bulk
# ``create_work_items`` batch can carry it once per item, so the aggregate
# request is bounded by item count, not by this constant alone. Single source
# of truth; ``tools.write`` imports this.
MAX_BODY_HTML_LEN: Final[int] = 2_000_000


class PaginatedResult[T](BaseModel):
    """Paginated response wrapper used by all list tools."""

    items: list[T]
    total_count: int
    page: int
    page_size: int
    has_more: bool = False


class ProjectSummary(BaseModel):
    """Summary of a Polarion project returned by ``list_projects``."""

    id: str
    name: str
    active: bool = Field(default=True, description="False means archived.")


class EnumOption(BaseModel):
    """Single enum option returned by ``list_*_enum_options``.

    Surfaces only what an LLM needs to pick a value before a write.
    """

    id: str = Field(description="Option id; pass verbatim to write tools.")
    name: str
    description: str = Field(default="", description="Empty when Polarion has none.")
    default: bool = False
    hidden: bool = Field(
        default=False, description="Hidden in the UI; avoid selecting."
    )
    terminal: bool = Field(
        default=False, description="For status fields, a workflow end-state."
    )


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


class WorkItemSummary(BaseModel):
    """Compact work-item representation for list and search results.

    ``space_id`` + ``document_name`` address the containing document (both
    empty when free-floating); pass them to
    ``get_document`` / ``read_document_parts``.
    """

    id: str
    title: str
    type: str
    status: str
    priority: str = Field(default="", description="e.g. '90.0'; empty when unset.")
    updated: str = Field(default="", description="ISO-8601; empty when unreported.")
    space_id: str = ""
    document_name: str = ""
    assignee_ids: list[str] = Field(
        default_factory=list, description="Short user IDs; empty when unassigned."
    )


class Hyperlink(BaseModel):
    """A single external hyperlink attached to a work item."""

    role: str = Field(description="Hyperlink role id (e.g. 'ref_ext').")
    title: str = ""
    uri: str


class WorkItemDetail(WorkItemSummary):
    """Full work-item details returned by ``get_work_item``.

    Extends ``WorkItemSummary`` with the body, project context, and
    detail-only metadata. ``description_html`` is the round-trip pair for
    ``update_work_item(description_html=...)`` and must never pass through a
    Markdown converter or sanitizer (it strips Polarion-specific spans and
    breaks the round-trip). ``custom_fields`` keeps rich-text values as
    ``{'type': 'text/html', 'value': '<...>'}`` so the shape round-trips back.
    """

    description_html: str = Field(
        default="", description="Raw HTML body; empty unless the read flag was True."
    )
    project_id: str
    author_id: str = Field(default="", description="Short user ID; empty when unknown.")
    created: str = Field(default="", description="ISO-8601; empty when unreported.")
    resolution: str = Field(
        default="", description="e.g. 'fixed'; empty when unresolved."
    )
    severity: str = Field(
        default="", description="e.g. 'critical'; empty for non-defects."
    )
    outline_number: str = Field(
        default="", description="e.g. '1.2.3'; empty outside a document."
    )
    hyperlinks: list[Hyperlink] = Field(default_factory=list)
    custom_fields: dict[str, object] = Field(default_factory=dict)


class WorkItemRead(WorkItemSummary):
    """LLM-friendly work-item view returned by ``read_work_item``.

    Mirrors ``WorkItemDetail`` but exposes ``description`` as Markdown
    (converted from HTML). Read-only synthesis — the Markdown body cannot be
    fed back to ``update_work_item``; for round-trip editing use
    ``get_work_item(include_description_html=True)`` with
    ``update_work_item(description_html=...)``. ``custom_fields`` keeps
    rich-text values as ``{'type': 'text/html', 'value': '<...>'}`` so this
    dict alone copies back into ``update_work_item(custom_fields=...)``.
    """

    description: str = Field(
        default="", description="Markdown body; read-only — do NOT feed to writes."
    )
    project_id: str
    author_id: str = Field(default="", description="Short user ID; empty when unknown.")
    created: str = Field(default="", description="ISO-8601; empty when unreported.")
    resolution: str = Field(
        default="", description="e.g. 'fixed'; empty when unresolved."
    )
    severity: str = Field(
        default="", description="e.g. 'critical'; empty for non-defects."
    )
    outline_number: str = Field(
        default="", description="e.g. '1.2.3'; empty outside a document."
    )
    hyperlinks: list[Hyperlink] = Field(default_factory=list)
    custom_fields: dict[str, object] = Field(default_factory=dict)


class WorkItemLink(BaseModel):
    """A work item link with the target's summary metadata.

    ``direction='forward'`` is outgoing (this item links to the target);
    ``'back'`` is incoming. The back-direction Lucene fallback drops the
    originating role, so ``role`` is ``None`` on every back link. ``suspect``
    (forward only) marks links whose target changed since last review.
    """

    id: str
    title: str
    role: str | None = Field(
        default=None, description="e.g. 'parent'; None on back-direction links."
    )
    direction: Literal["forward", "back"]
    suspect: bool
    type: str = ""
    status: str = ""
    space_id: str = Field(default="", description="Empty when not module-bound.")
    document_name: str = Field(default="", description="Empty when not module-bound.")


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


class WorkItemsCreateResult(BaseModel):
    """Result of a ``create_work_items`` operation."""

    created: bool
    dry_run: bool
    work_item_ids: list[str] = Field(
        default_factory=list, description="Short IDs in input order; empty on dry-run."
    )
    payload_preview: Mapping[str, object] | None = None


class WorkItemUpdateResult(BaseModel):
    """Result of an ``update_work_item`` operation."""

    updated: bool
    dry_run: bool
    current: WorkItemDetail | None = Field(
        description="Post-PATCH state for verification; None on dry-run."
    )
    changes: Mapping[str, object]
    payload_preview: Mapping[str, object] | None


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


class WorkItemCreateSpec(BaseModel):
    """One work item to create via ``create_work_items``."""

    title: str = Field(min_length=1)
    type: str = Field(
        min_length=1, description="e.g. 'requirement', 'task', 'testCase'."
    )
    description: str | None = Field(
        default=None,
        max_length=MAX_BODY_HTML_LEN,
        description="Markdown body; converted to sanitized HTML on write.",
    )
    status: str | None = Field(
        default=None, description="Initial workflow status; project default if omitted."
    )
    priority: str | None = Field(default=None, description="e.g. '50.0'.")
    severity: str | None = Field(default=None, description="e.g. 'major', 'critical'.")
    assignee_ids: list[str] | None = Field(
        default=None, description="Short user IDs, e.g. ['alice', 'bob']."
    )
    due_date: str | None = Field(default=None, description="'YYYY-MM-DD'.")
    initial_estimate: str | None = Field(
        default=None, description="Polarion duration, e.g. '5 1/2d', '1w 2d', '4h'."
    )
    hyperlinks: list[Hyperlink] | None = Field(
        default=None, description="Each must have ``role`` and ``uri``."
    )
    custom_fields: dict[str, object] | None = Field(
        default=None,
        description=(
            "Keyed by Polarion field ID. Keys are defined per project+type, so "
            "take them from an existing work item of this ``type`` via "
            "get_work_item to avoid ghost keys; rich-text values must be "
            "``{'type':'text/html','value':...}``."
        ),
    )


class WorkItemLinkSpec(BaseModel):
    """One link to create under a source work item."""

    role: str = Field(min_length=1, description="Link role id (e.g. 'parent').")
    target_work_item_id: str = Field(min_length=1)
    target_project_id: str | None = Field(
        default=None, description="Defaults to the source's project."
    )
    suspect: bool = False
    revision: str | None = Field(
        default=None, description="Revision pin; defaults to current HEAD."
    )


class WorkItemLinkRef(BaseModel):
    """One existing link identified for deletion."""

    role: str = Field(min_length=1, description="Link role id; must match exactly.")
    target_work_item_id: str = Field(min_length=1)
    target_project_id: str | None = Field(
        default=None, description="Defaults to the source's project."
    )


class WorkItemLinksCreateResult(BaseModel):
    """Result of a ``create_work_item_links`` operation."""

    created: bool
    dry_run: bool
    link_ids: list[str] = Field(
        default_factory=list,
        description="Composite 5-segment ids in input order; empty on dry-run.",
    )
    payload_preview: Mapping[str, object] | None = None


class WorkItemLinksDeleteResult(BaseModel):
    """Result of a ``delete_work_item_links`` operation.

    ``link_ids`` echoes the request (all submitted composite ids, in order).
    The tool pre-reads the source's existing outgoing links and splits the
    request into ``deleted_link_ids`` (matched) and ``not_found_link_ids``
    (silent no-ops Polarion ignores); both populate whenever the op returns.
    An unreachable pre-read fails closed before any delete.
    """

    deleted: bool
    dry_run: bool
    link_ids: list[str] = Field(
        default_factory=list,
        description="Composite 5-segment ids reconstructed from the request refs.",
    )
    deleted_link_ids: list[str] = Field(default_factory=list)
    not_found_link_ids: list[str] = Field(
        default_factory=list, description="Requested ids with no match (silent no-ops)."
    )
    payload_preview: Mapping[str, object] | None = None


class WorkItemLinkUpdateSpec(BaseModel):
    """One existing link to update with new attribute values.

    ``suspect`` and ``revision`` are tri-state: the PATCH carries only fields
    explicitly set, so ``None`` leaves the server-side value unchanged. At
    least one must be set — an all-``None`` spec yields an empty PATCH body
    that Polarion rejects with HTTP 400.
    """

    role: str = Field(min_length=1, description="Link role id; must match exactly.")
    target_work_item_id: str = Field(min_length=1)
    target_project_id: str | None = Field(
        default=None, description="Defaults to the source's project."
    )
    suspect: bool | None = Field(
        default=None, description="New suspect flag; None leaves it unchanged."
    )
    revision: str | None = Field(
        default=None, description="New revision pin; None leaves it unchanged."
    )

    @model_validator(mode="after")
    def _at_least_one_attribute(self) -> WorkItemLinkUpdateSpec:
        if self.suspect is None and self.revision is None:
            msg = (
                "WorkItemLinkUpdateSpec requires at least one of"
                " `suspect` / `revision` to be set."
            )
            raise ValueError(msg)
        return self


class WorkItemLinkUpdateResult(BaseModel):
    """Result of an ``update_work_item_link`` operation."""

    updated: bool
    dry_run: bool
    link_id: str = Field(description="Composite 5-segment id computed from inputs.")
    payload_preview: Mapping[str, object] | None


class WorkItemMoveResult(BaseModel):
    """Result of a ``move_work_item_to_document`` or sibling move-document call."""

    moved: bool
    dry_run: bool
    payload_preview: Mapping[str, object] | None


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


__all__: list[str] = [
    "MAX_BODY_HTML_LEN",
    "DocumentComment",
    "DocumentCommentSpec",
    "DocumentCommentUpdateResult",
    "DocumentCommentsCreateResult",
    "DocumentCreateResult",
    "DocumentDetail",
    "DocumentPart",
    "DocumentReadResult",
    "DocumentSummary",
    "DocumentUpdateResult",
    "EnumOption",
    "Hyperlink",
    "JsonValue",
    "PaginatedResult",
    "ProjectSummary",
    "WorkItemCreateSpec",
    "WorkItemDetail",
    "WorkItemLink",
    "WorkItemLinkRef",
    "WorkItemLinkSpec",
    "WorkItemLinkUpdateResult",
    "WorkItemLinkUpdateSpec",
    "WorkItemLinksCreateResult",
    "WorkItemLinksDeleteResult",
    "WorkItemMoveResult",
    "WorkItemRead",
    "WorkItemSummary",
    "WorkItemUpdateResult",
    "WorkItemsCreateResult",
]
