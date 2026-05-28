"""Pydantic models for MCP tool inputs and outputs.

Every tool accepts and returns Pydantic models — never raw ``dict``.
Fields where the name alone is unambiguous (e.g. ``items``, ``page``)
omit ``Field(description=...)``; the rest carry a description that the
JSON Schema surfaces to the LLM.

Models are organised into three categories:

* **Read models** — returned by read tools (summaries, details, paginated
  results).
* **Write-result models** — returned by write tools (create/update
  confirmations with ``dry_run`` support).
* **Generic wrappers** — ``PaginatedResult[T]`` used by all list tools.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Literal

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


class PaginatedResult[T](BaseModel):
    """Paginated response wrapper used by all list tools.

    Provides the current page of items together with pagination metadata
    so the LLM can decide whether to request additional pages.
    """

    items: list[T]
    total_count: int
    page: int
    page_size: int
    has_more: bool = Field(default=False, description="True if more pages follow.")


class ProjectSummary(BaseModel):
    """Summary of a Polarion project returned by ``list_projects``."""

    id: str
    name: str
    active: bool = Field(
        default=True,
        description="False means archived.",
    )


class EnumOption(BaseModel):
    """Single enum option returned by ``list_document_enum_options``.

    Surfaces only the attributes an LLM needs to pick a value before a
    write call. Other Polarion option fields (color, iconURL, columnWidth,
    createDefect, limited, minValue, oppositeName, parent,
    requiresSignatureForTestCaseExecution, templateWorkItem) are not
    exposed.
    """

    id: str = Field(description="Option id; pass verbatim to write tools.")
    name: str = Field(description="Human-readable display name.")
    description: str = Field(
        default="",
        description="Option description; empty when Polarion has none.",
    )
    default: bool = Field(
        default=False,
        description="True when this option is the project default.",
    )
    hidden: bool = Field(
        default=False,
        description="True when the option is hidden in the UI; avoid selecting.",
    )
    terminal: bool = Field(
        default=False,
        description="For status fields, True for workflow end-states.",
    )


class DocumentSummary(BaseModel):
    """Summary of a Polarion document returned by ``list_documents``."""

    space_id: str = Field(
        description="Space containing the document (e.g. '_default').",
    )
    document_name: str = Field(
        description="Document name within the space.",
    )


class DocumentDetail(BaseModel):
    """Full details of a Polarion document returned by ``get_document``.

    ``content_html`` is the round-trip pair for
    ``update_document(home_page_content_html=...)`` — populated only when
    ``include_homepage_content_html=True``. It is the inline prose only;
    heading text and embedded work-item bodies live in separate work items,
    so ``read_document`` is the assembled-body view.

    ``custom_fields`` mirrors the keys configured on the document type:
    rich-text values stay as ``{'type': 'text/html', 'value': '<...>'}``
    dicts so the shape round-trips back unchanged.
    """

    title: str = Field(description="Document title.")
    type: str = Field(
        default="",
        description="Document type (e.g. 'req_specification').",
    )
    status: str = Field(
        default="",
        description="Document workflow status (e.g. 'draft', 'approved').",
    )
    content_html: str = Field(
        default="",
        description="Raw Polarion HTML body; empty unless the read flag was True.",
    )
    custom_fields: dict[str, object] = Field(
        default_factory=dict,
        description="Project-defined custom fields keyed by field ID.",
    )


class DocumentPart(BaseModel):
    """A single part (heading or work item) within a Polarion document.

    Field population varies by ``type``:

    * ``heading`` — text in ``title``, depth in ``level``, ``work_item_*``
      fields point at the heading work item.
    * ``workitem`` — body in ``description`` (Markdown), metadata on
      ``work_item_*``, ``content`` empty.
    * ``normal`` / ``wikiblock`` — body in ``content`` (Markdown).
    * ``toc`` / ``tof`` / ``page_break`` — widget placeholders, all body
      fields empty. ``tof`` and ``page_break`` are inferred from the part
      ID prefix because Polarion reports both as plain ``normal``.

    Use ``id`` as ``previous_part_id`` / ``next_part_id`` when calling
    ``move_work_item_to_document``. ``work_item_id`` plugs straight into
    ``get_work_item`` / ``list_work_item_links``.
    """

    id: str = Field(
        description="Short part identifier (e.g. 'workitem_MCPT-042').",
    )
    title: str = Field(description="Part title or heading text.")
    content: str = Field(
        description="Part body in Markdown; empty unless body lives here.",
    )
    type: Literal[
        "heading",
        "workitem",
        "normal",
        "toc",
        "wikiblock",
        "tof",
        "page_break",
    ] = Field(description="Part type; see class docstring for body-field mapping.")
    level: int = Field(
        description="Heading level (1-4) for heading parts; 0 otherwise.",
    )
    description: str = Field(
        default="",
        description="Linked work item body as Markdown; only set on 'workitem' parts.",
    )
    work_item_id: str = Field(
        default="",
        description="Linked Work Item ID; empty unless 'workitem' / 'heading'.",
    )
    work_item_type: str = Field(
        default="",
        description="Linked work item type; empty unless 'workitem' / 'heading'.",
    )
    work_item_status: str = Field(
        default="",
        description="Linked work item status; empty unless 'workitem' / 'heading'.",
    )
    external: bool = Field(
        default=False,
        description="True when the part is re-used from another project (read-only).",
    )
    outline_number: str = Field(
        default="",
        description="Hierarchical position (e.g. '1.2.3'); empty for prose / widgets.",
    )
    next_part_id: str = Field(
        default="",
        description="Short ID of the next part; empty on the last part.",
    )


class DocumentReadResult(BaseModel):
    """Rendered Markdown view of one page of document parts.

    Returned by ``read_document``. Interleaves heading titles, embedded
    work-item descriptions, and inline prose from a single page of
    ``read_document_parts`` into a flowing Markdown stream suitable for
    end-to-end reading by an LLM.

    The output is read-only synthesis: it cannot be fed back to any
    write tool because no update path accepts this shape. For round-trip
    editing of the document body, fetch the raw source via
    ``get_document(include_homepage_content_html=True)`` instead.

    ``part_count`` reflects parts consumed from ``read_document_parts``
    on this page — including widget placeholders that produce no output
    — so use it for pagination accounting, not chunk count.
    """

    content: str = Field(description="Rendered Markdown for this page.")
    part_count: int = Field(description="Number of parts consumed on this page.")
    page: int = Field(description="Current page number (1-based).")
    page_size: int = Field(description="Maximum number of parts per page.")
    total_parts: int = Field(description="Total parts across the entire document.")
    has_more: bool = Field(
        default=False,
        description="True when more pages of parts follow.",
    )


class WorkItemSummary(BaseModel):
    """Compact work-item representation for list and search results.

    ``space_id`` + ``document_name`` together address the containing
    document (both empty when the work item is free-floating); pass them
    to ``get_document`` / ``read_document_parts``.
    """

    id: str = Field(description="Work Item ID (e.g. 'MCPT-001').")
    title: str = Field(description="Work Item title.")
    type: str = Field(description="Work Item type (e.g. 'requirement', 'testCase').")
    status: str = Field(description="Workflow status (e.g. 'draft', 'approved').")
    priority: str = Field(
        default="",
        description="Priority value as a string (e.g. '90.0'); empty when unset.",
    )
    updated: str = Field(
        default="",
        description="ISO-8601 last-modified timestamp; empty when unreported.",
    )
    space_id: str = Field(
        default="",
        description="Containing document's space; empty when free-floating.",
    )
    document_name: str = Field(
        default="",
        description="Containing document name; empty when free-floating.",
    )
    assignee_ids: list[str] = Field(
        default_factory=list,
        description="Short user IDs of assignees; empty list when unassigned.",
    )


class Hyperlink(BaseModel):
    """A single external hyperlink attached to a work item."""

    role: str = Field(description="Hyperlink role id (e.g. 'ref_ext').")
    title: str = Field(
        default="",
        description="Human-readable link title; empty when unset.",
    )
    uri: str = Field(description="Target URI of the hyperlink.")


class WorkItemDetail(WorkItemSummary):
    """Full work-item details returned by ``get_work_item``.

    Extends ``WorkItemSummary`` with the description, project context,
    and detail-only metadata (authorship, resolution, severity, outline
    position, external hyperlinks).

    ``description_html`` is the round-trip pair for
    ``update_work_item(description_html=...)`` and must never pass through
    a Markdown converter or sanitizer — doing so strips Polarion-specific
    spans and breaks the round-trip. ``custom_fields`` keeps rich-text
    values as ``{'type': 'text/html', 'value': '<...>'}`` dicts so the
    shape round-trips back unchanged.
    """

    description_html: str = Field(
        default="",
        description="Raw Polarion HTML body; empty unless the read flag was True.",
    )
    project_id: str = Field(description="Project that contains this work item.")
    author_id: str = Field(
        default="",
        description="Short user ID of the author; empty when unreported.",
    )
    created: str = Field(
        default="",
        description="ISO-8601 creation timestamp; empty when unreported.",
    )
    resolution: str = Field(
        default="",
        description="Resolution outcome (e.g. 'fixed'); empty when unresolved.",
    )
    severity: str = Field(
        default="",
        description="Severity classification (e.g. 'critical'); empty for non-defects.",
    )
    outline_number: str = Field(
        default="",
        description="Hierarchical position (e.g. '1.2.3'); empty outside a document.",
    )
    hyperlinks: list[Hyperlink] = Field(
        default_factory=list,
        description="External hyperlinks attached to this work item.",
    )
    custom_fields: dict[str, object] = Field(
        default_factory=dict,
        description="Project-defined custom fields keyed by field ID.",
    )


class WorkItemRead(WorkItemSummary):
    """LLM-friendly work-item view returned by ``read_work_item``.

    Mirrors ``WorkItemDetail`` but exposes ``description`` as Markdown
    (converted from Polarion HTML) instead of the raw ``description_html``.
    Read-only synthesis: the Markdown body cannot be fed back to
    ``update_work_item`` (the converter collapses Polarion-specific
    markup). For round-trip editing, use
    ``get_work_item(include_description_html=True)`` paired with
    ``update_work_item(description_html=...)``.

    ``custom_fields`` keeps rich-text values as
    ``{'type': 'text/html', 'value': '<...>'}`` dicts so this dict alone
    can be copied back into ``update_work_item(custom_fields=...)``.
    """

    description: str = Field(
        default="",
        description="Body as Markdown; read-only — do NOT feed to update_work_item.",
    )
    project_id: str = Field(description="Project that contains this work item.")
    author_id: str = Field(
        default="",
        description="Short user ID of the author; empty when unreported.",
    )
    created: str = Field(
        default="",
        description="ISO-8601 creation timestamp; empty when unreported.",
    )
    resolution: str = Field(
        default="",
        description="Resolution outcome (e.g. 'fixed'); empty when unresolved.",
    )
    severity: str = Field(
        default="",
        description="Severity classification (e.g. 'critical'); empty for non-defects.",
    )
    outline_number: str = Field(
        default="",
        description="Hierarchical position (e.g. '1.2.3'); empty outside a document.",
    )
    hyperlinks: list[Hyperlink] = Field(
        default_factory=list,
        description="External hyperlinks attached to this work item.",
    )
    custom_fields: dict[str, object] = Field(
        default_factory=dict,
        description="Project-defined custom fields keyed by field ID.",
    )


class WorkItemLink(BaseModel):
    """A work item link with the target's summary metadata.

    ``direction='forward'`` is an outgoing link (this work item links to
    the target); ``'back'`` is an incoming link (the target links to this
    work item). The back-direction Lucene fallback does not expose the
    originating role, so ``role`` is ``None`` for every back-direction
    item. ``suspect`` marks links whose target changed since the link
    was last reviewed; it is only meaningful in the forward direction.
    """

    id: str = Field(description="Linked Work Item ID (e.g. 'MCPT-002').")
    title: str = Field(description="Linked Work Item title.")
    role: str | None = Field(
        default=None,
        description="Link role (e.g. 'parent'); None on back-direction links.",
    )
    direction: Literal["forward", "back"] = Field(
        description="'forward' for outgoing links, 'back' for incoming.",
    )
    suspect: bool = Field(description="True when the link is marked as suspect.")
    type: str = Field(
        default="",
        description="Linked work item type; empty when unreported.",
    )
    status: str = Field(
        default="",
        description="Linked work item workflow status; empty when unreported.",
    )
    space_id: str = Field(
        default="",
        description="Linked item's document space; empty when not module-bound.",
    )
    document_name: str = Field(
        default="",
        description="Linked item's document name; empty when not module-bound.",
    )


class DocumentComment(BaseModel):
    """A single document comment returned by ``list_document_comments``.

    Comments form a tree: top-level comments have ``parent_comment_id=None``
    and replies link back via ``parent_comment_id`` while exposing their own
    replies through ``child_comment_ids``. The list endpoint returns a flat
    page; rebuild the thread on the client side. ``text`` is returned
    verbatim with ``text_format`` indicating whether it is HTML or plain
    text -- HTML is NOT sanitized, so it round-trips losslessly.
    """

    id: str = Field(description="Comment ID (e.g. 'MyCommentId').")
    created: str = Field(description="ISO-8601 creation timestamp.")
    resolved: bool = Field(
        default=False,
        description="True when the comment is marked resolved.",
    )
    text: str = Field(default="", description="Comment body verbatim.")
    text_format: Literal["text/html", "text/plain"] = Field(
        default="text/html",
        description="MIME type of ``text`` as reported by Polarion.",
    )
    author_id: str | None = Field(
        default=None,
        description="User ID of the author; None when unknown.",
    )
    parent_comment_id: str | None = Field(
        default=None,
        description="Parent comment ID for replies; None on top-level.",
    )
    child_comment_ids: list[str] = Field(
        default_factory=list,
        description="Direct reply comment IDs in declaration order.",
    )


class DocumentCommentSpec(BaseModel):
    """One comment to create via ``create_document_comments``.

    ``parent_comment_id`` is the short ID from ``list_document_comments``
    (omit for top-level); the tool composes the full path internally.
    Omit ``author_id`` to default to the authenticated token's user;
    omit ``resolved`` to let Polarion default to False.
    """

    text: str = Field(min_length=1, description="Comment body text.")
    text_format: Literal["text/html", "text/plain"] = Field(
        default="text/plain",
        description="MIME type of ``text``.",
    )
    resolved: bool | None = Field(default=None, description="Initial resolved state.")
    author_id: str | None = Field(default=None, description="Author user ID.")
    parent_comment_id: str | None = Field(
        default=None,
        description="Short comment ID for replies; omit for top-level.",
    )


class WorkItemCreateResult(BaseModel):
    """Result of a ``create_work_item`` operation."""

    created: bool = Field(description="True on a real create; False on dry-run.")
    dry_run: bool = Field(description="Whether this was a dry-run.")
    work_item_id: str | None = Field(
        description="ID of the new work item (e.g. 'MCPT-042'); None on dry-run.",
    )
    payload_preview: Mapping[str, object] | None = Field(
        description="JSON:API payload sent or previewed; None after real ops.",
    )


class WorkItemUpdateResult(BaseModel):
    """Result of an ``update_work_item`` operation."""

    updated: bool = Field(description="True on a real update; False on dry-run.")
    dry_run: bool = Field(description="Whether this was a dry-run.")
    current: WorkItemDetail | None = Field(
        description="Post-PATCH state for verification; None on dry-run.",
    )
    changes: Mapping[str, object] = Field(
        description="Map of field names to their new values in the PATCH.",
    )
    payload_preview: Mapping[str, object] | None = Field(
        description="JSON:API payload sent or previewed; None after real ops.",
    )


class DocumentCommentsCreateResult(BaseModel):
    """Result of a ``create_document_comments`` operation."""

    created: bool = Field(description="True on a real create; False on dry-run.")
    dry_run: bool = Field(description="Whether this was a dry-run.")
    comment_ids: list[str] = Field(
        description="Short IDs in Polarion's return order; empty on dry-run.",
    )
    payload_preview: Mapping[str, object] | None = Field(
        description="JSON:API payload sent or previewed; None after real ops.",
    )


class DocumentCommentUpdateResult(BaseModel):
    """Result of an ``update_document_comment`` operation."""

    updated: bool = Field(description="True on a real PATCH; False on dry-run.")
    dry_run: bool = Field(description="Whether this was a dry-run.")
    comment_id: str | None = Field(
        description="Short comment ID patched (e.g. 'c42'); None on dry-run.",
    )
    resolved: bool = Field(
        description="The resolved value sent (or that would be sent).",
    )
    payload_preview: Mapping[str, object] | None = Field(
        description="JSON:API payload sent or previewed; None after real ops.",
    )


class WorkItemLinkSpec(BaseModel):
    """One link to create under a source work item."""

    role: str = Field(min_length=1, description="Link role id (e.g. 'parent').")
    target_work_item_id: str = Field(min_length=1, description="Target work item ID.")
    target_project_id: str | None = Field(
        default=None,
        description="Target's project; defaults to the source's project.",
    )
    suspect: bool = Field(default=False, description="Mark the link as suspect.")
    revision: str | None = Field(
        default=None,
        description="Optional revision pin; defaults to current HEAD.",
    )


class WorkItemLinkRef(BaseModel):
    """One existing link identified for deletion."""

    role: str = Field(min_length=1, description="Link role id; must match exactly.")
    target_work_item_id: str = Field(min_length=1, description="Target work item ID.")
    target_project_id: str | None = Field(
        default=None,
        description="Target's project; defaults to the source's project.",
    )


class WorkItemLinksCreateResult(BaseModel):
    """Result of a ``create_work_item_links`` operation."""

    created: bool = Field(description="True on a real create; False on dry-run.")
    dry_run: bool = Field(description="Whether this was a dry-run.")
    link_ids: list[str] = Field(
        default_factory=list,
        description="Composite 5-segment link ids in input order; empty on dry-run.",
    )
    payload_preview: Mapping[str, object] | None = Field(
        default=None,
        description="JSON:API payload sent or previewed; None after real ops.",
    )


class WorkItemLinksDeleteResult(BaseModel):
    """Result of a ``delete_work_item_links`` operation.

    ``link_ids`` echoes the REQUEST — Polarion silently ignores body-level
    refs that do not match an existing link, so the echo is not a list of
    what was actually deleted. Cross-check with ``list_work_item_links``
    if exact accounting is required.
    """

    deleted: bool = Field(description="True on a real delete; False on dry-run.")
    dry_run: bool = Field(description="Whether this was a dry-run.")
    link_ids: list[str] = Field(
        default_factory=list,
        description="Composite 5-segment ids reconstructed from the request refs.",
    )
    payload_preview: Mapping[str, object] | None = Field(
        default=None,
        description="JSON:API payload sent or previewed; None after real ops.",
    )


class WorkItemLinkUpdateSpec(BaseModel):
    """One existing link to update with new attribute values.

    ``suspect`` and ``revision`` are tri-state -- the JSON:API PATCH only
    carries fields that are explicitly set on the spec, so ``None`` means
    "leave the existing server-side value unchanged". At least one of the
    two must be set; an all-``None`` spec would yield an empty PATCH body
    that Polarion rejects with HTTP 400.
    """

    role: str = Field(min_length=1, description="Link role id; must match exactly.")
    target_work_item_id: str = Field(min_length=1, description="Target work item ID.")
    target_project_id: str | None = Field(
        default=None,
        description="Target's project; defaults to the source's project.",
    )
    suspect: bool | None = Field(
        default=None,
        description="New suspect flag; None leaves it unchanged.",
    )
    revision: str | None = Field(
        default=None,
        description="New revision pin; None leaves it unchanged.",
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
    """Result of an ``update_work_item_links`` operation."""

    updated: bool = Field(description="True on a real PATCH; False on dry-run.")
    dry_run: bool = Field(description="Whether this was a dry-run.")
    link_id: str = Field(
        description="Composite 5-segment id computed from inputs.",
    )
    payload_preview: Mapping[str, object] | None = Field(
        description="JSON:API PATCH body; None after real ops.",
    )


class WorkItemMoveResult(BaseModel):
    """Result of a ``move_work_item_to_document`` or sibling move-document call."""

    moved: bool = Field(description="True on a real move; False on dry-run.")
    dry_run: bool = Field(description="Whether this was a dry-run.")
    payload_preview: Mapping[str, object] | None = Field(
        description="Request payload sent or previewed; None after real ops.",
    )


class DocumentCreateResult(BaseModel):
    """Result of a ``create_document`` operation."""

    created: bool = Field(description="True on a real create; False on dry-run.")
    dry_run: bool = Field(description="Whether this was a dry-run.")
    document_name: str | None = Field(
        description="Module name of the new document; None on dry-run.",
    )
    payload_preview: Mapping[str, object] | None = Field(
        description="JSON:API payload sent or previewed; None after real ops.",
    )


class DocumentUpdateResult(BaseModel):
    """Result of an ``update_document`` operation."""

    updated: bool = Field(description="True on a real PATCH; False on dry-run.")
    dry_run: bool = Field(description="Whether this was a dry-run.")
    payload_preview: Mapping[str, object] | None = Field(
        description="JSON:API payload sent or previewed; None after real ops.",
    )


__all__: list[str] = [
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
    "WorkItemCreateResult",
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
]
