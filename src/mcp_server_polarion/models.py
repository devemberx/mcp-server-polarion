"""Pydantic models for MCP tool inputs and outputs.

Every tool in the Polarion MCP server accepts and returns Pydantic models
— never raw ``dict``.  Each field carries a ``Field(description=...)`` so
that FastMCP can auto-generate JSON Schema documentation for the LLM.

Models are organised into three categories:

* **Read models** — returned by read tools (summaries, details, paginated
  results).
* **Write-result models** — returned by write tools (create/update
  confirmations with ``dry_run`` support).
* **Generic wrappers** — ``PaginatedResult[T]`` used by all list tools.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

# Recursive JSON-safe type alias.  Constrains payload previews and change
# maps to values that are guaranteed to round-trip through JSON-RPC.
type JsonValue = (
    str | int | float | bool | None | list[JsonValue] | dict[str, JsonValue]
)

# ---------------------------------------------------------------------------
# Generic pagination wrapper
# ---------------------------------------------------------------------------


class PaginatedResult[T](BaseModel):
    """Paginated response wrapper used by all list tools.

    Provides the current page of items together with pagination metadata
    so the LLM can decide whether to request additional pages.
    """

    items: list[T] = Field(
        description="List of items on the current page.",
    )
    total_count: int = Field(
        description="Total number of items across all pages.",
    )
    page: int = Field(
        description="Current page number (1-based).",
    )
    page_size: int = Field(
        description="Maximum number of items per page.",
    )
    has_more: bool = Field(
        default=False,
        description=(
            "True when there are more pages after this one. "
            "Use this to decide whether to fetch the next page."
        ),
    )


# ---------------------------------------------------------------------------
# Read models — summaries and details
# ---------------------------------------------------------------------------


class ProjectSummary(BaseModel):
    """Summary of a Polarion project returned by ``list_projects``."""

    id: str = Field(
        description="Unique project identifier (e.g. 'myproject').",
    )
    name: str = Field(
        description="Human-readable project name.",
    )
    active: bool = Field(
        default=True,
        description=(
            "Whether the project is active. False indicates an archived "
            "or inactive project that should generally be skipped when "
            "selecting a target project. Defaults to True if the server "
            "does not report the flag."
        ),
    )


class DocumentSummary(BaseModel):
    """Summary of a Polarion document returned by ``list_documents``."""

    space_id: str = Field(
        description=(
            "Space identifier that contains the document (e.g. '_default', 'Design')."
        ),
    )
    document_name: str = Field(
        description=(
            "Document name within the space"
            " (e.g. 'Software Requirement Specification')."
        ),
    )


class DocumentDetail(BaseModel):
    """Full details of a Polarion document returned by ``get_document``."""

    title: str = Field(
        description="Document title.",
    )
    type: str = Field(
        default="",
        description=(
            "Document type (e.g. 'req_specification', 'test_specification'). "
            "Empty string when the server does not report a type."
        ),
    )
    status: str = Field(
        default="",
        description=(
            "Document workflow status (e.g. 'draft', 'approved'). "
            "Empty string when the server does not report a status."
        ),
    )
    content: str = Field(
        default="",
        description=(
            "Document body (homePageContent) converted to Markdown. "
            "Only populated when ``get_document`` is called with "
            "``include_content=True``; otherwise an empty string."
        ),
    )


class DocumentPart(BaseModel):
    """A single part (heading or work item) within a Polarion document."""

    id: str = Field(
        description=(
            "Short part identifier within the document "
            "(e.g. 'heading_MCPT-001', 'workitem_MCPT-042', 'polarion_1'). "
            "Use this as ``next_part_id`` (insert before) or "
            "``previous_part_id`` (insert after) when calling "
            "``move_work_item_to_document``."
        ),
    )
    title: str = Field(
        description="Part title or heading text.",
    )
    content: str = Field(
        description=(
            "Part body in Markdown. Populated for 'normal', 'toc', and "
            "'wikiblock' parts. Empty for 'heading' parts (the heading "
            "text is in ``title`` and the level in ``level``) and for "
            "'workitem' parts (the body is in ``description``)."
        ),
    )
    type: Literal["heading", "workitem", "normal", "toc", "wikiblock"] = Field(
        description=(
            "Part type: 'heading', 'workitem', 'normal' (rich text), "
            "'toc' (table of contents), or 'wikiblock' (wiki macro block)."
        ),
    )
    level: int = Field(
        description=(
            "Heading level (1-4) for heading parts. "
            "0 for work-item parts that have no heading level."
        ),
    )
    description: str = Field(
        default="",
        description=(
            "Work item description converted to Markdown. "
            "Only populated for 'workitem'-type parts. "
            "Empty for headings and other part types."
        ),
    )
    work_item_id: str = Field(
        default="",
        description=(
            "Short Work Item ID of the linked work item "
            "(e.g. 'MCPT-001'). Populated for 'workitem' and 'heading' "
            "parts; empty for other part types. Use this directly with "
            "``get_work_item`` or ``get_linked_work_items``."
        ),
    )
    work_item_type: str = Field(
        default="",
        description=(
            "Type of the linked work item (e.g. 'requirement', "
            "'testCase', 'risk'). Populated for 'workitem' and 'heading' "
            "parts; empty otherwise."
        ),
    )
    work_item_status: str = Field(
        default="",
        description=(
            "Workflow status of the linked work item "
            "(e.g. 'draft', 'approved'). Populated for 'workitem' and "
            "'heading' parts; empty otherwise."
        ),
    )
    external: bool = Field(
        default=False,
        description=(
            "True when this part references a work item from another "
            "project (re-used content). Such parts are typically "
            "read-only — editing must be done in the source project."
        ),
    )
    next_part_id: str = Field(
        default="",
        description=(
            "Short ID of the next part in document order "
            "(e.g. 'workitem_MCPT-002'). "
            "Empty string when this is the last part."
        ),
    )


class WorkItemSummary(BaseModel):
    """Compact work-item representation for list and search results."""

    id: str = Field(
        description="Work Item ID (e.g. 'MCPT-001').",
    )
    title: str = Field(
        description="Work Item title.",
    )
    type: str = Field(
        description=(
            "Work Item type (e.g. 'requirement', 'task', 'testCase', 'defect')."
        ),
    )
    status: str = Field(
        description="Work Item workflow status (e.g. 'draft', 'approved').",
    )
    priority: str = Field(
        default="",
        description=(
            "Polarion priority value as a string (e.g. '90.0'). "
            "Empty when the server does not report a priority."
        ),
    )
    updated: str = Field(
        default="",
        description=(
            "ISO-8601 timestamp of the last modification "
            "(e.g. '2026-04-29T10:23:00Z'). Empty when not reported."
        ),
    )
    space_id: str = Field(
        default="",
        description=(
            "Space that contains the document this work item belongs to. "
            "Empty when the work item is not part of any document."
        ),
    )
    document_name: str = Field(
        default="",
        description=(
            "Name of the document this work item belongs to. "
            "Empty when the work item is not part of any document. "
            "Use with ``space_id`` to call ``get_document`` / "
            "``get_document_parts``."
        ),
    )
    assignee_ids: list[str] = Field(
        default_factory=list,
        description=(
            "Short user IDs of the assignees (e.g. ['alice', 'bob']). "
            "Empty list when the work item has no assignee."
        ),
    )


class Hyperlink(BaseModel):
    """A single external hyperlink attached to a work item."""

    role: str = Field(
        description=("Hyperlink role identifier (e.g. 'ref_ext', 'implementation')."),
    )
    title: str = Field(
        default="",
        description="Human-readable link title. Empty when not provided.",
    )
    uri: str = Field(
        description="Target URI of the hyperlink.",
    )


class WorkItemDetail(WorkItemSummary):
    """Full work-item details returned by ``get_work_item``.

    Extends ``WorkItemSummary`` with the description, project context,
    and detail-only metadata (authorship, resolution, severity,
    outline position, external hyperlinks).
    """

    description: str = Field(
        description=(
            "Work Item description converted to Markdown. "
            "Empty string when the work item has no description."
        ),
    )
    project_id: str = Field(
        description="Project that contains this work item.",
    )
    author_id: str = Field(
        default="",
        description=(
            "Short user ID of the author (e.g. 'alice'). "
            "Empty when the server does not report an author."
        ),
    )
    created: str = Field(
        default="",
        description=(
            "ISO-8601 timestamp of the work item creation "
            "(e.g. '2026-04-29T10:23:00Z'). Empty when not reported."
        ),
    )
    resolution: str = Field(
        default="",
        description=(
            "Resolution outcome for closed/done work items "
            "(e.g. 'fixed', 'wontfix', 'duplicate'). "
            "Empty for unresolved or non-closeable items."
        ),
    )
    severity: str = Field(
        default="",
        description=(
            "Severity classification, primarily used for defects "
            "(e.g. 'blocker', 'critical', 'major'). "
            "Empty for non-defect types."
        ),
    )
    outline_number: str = Field(
        default="",
        description=(
            "Hierarchical position inside the containing document "
            "(e.g. '1.2.3'). Empty when the work item is not part of "
            "a document or has no assigned outline number."
        ),
    )
    hyperlinks: list[Hyperlink] = Field(
        default_factory=list,
        description=(
            "External hyperlinks attached to this work item. "
            "Empty list when none are set."
        ),
    )


class LinkedWorkItemSummary(BaseModel):
    """A single linked work item with its link metadata."""

    id: str = Field(
        description="Linked Work Item ID (e.g. 'MCPT-002').",
    )
    title: str = Field(
        description="Linked Work Item title.",
    )
    role: str | None = Field(
        default=None,
        description=(
            "Link role identifier (e.g. 'parent', 'relates_to', "
            "'verifies'). ``None`` for back-direction links because "
            "Polarion's ``linkedWorkItems:`` query does not expose the "
            "originating link's role on this server version. May be "
            "filled in once the ``backlinkedworkitems`` endpoint becomes "
            "available."
        ),
    )
    direction: Literal["forward", "back"] = Field(
        description=(
            "'forward' for outgoing links (this WI links to the target). "
            "'back' for incoming links (the target links to this WI)."
        )
    )
    suspect: bool = Field(
        description=(
            "Whether the link is marked as suspect. "
            "Suspect links indicate that the linked item has changed "
            "since the link was last reviewed."
        ),
    )
    type: str = Field(
        default="",
        description=(
            "Type of the linked work item (e.g. 'requirement', "
            "'testCase'). Empty when the server does not report a type."
        ),
    )
    status: str = Field(
        default="",
        description=(
            "Workflow status of the linked work item "
            "(e.g. 'draft', 'approved'). Empty when the server does not "
            "report a status."
        ),
    )
    space_id: str = Field(
        default="",
        description=(
            "Space that contains the document the linked work item "
            "belongs to. Empty when not module-bound."
        ),
    )
    document_name: str = Field(
        default="",
        description=(
            "Name of the document the linked work item belongs to. "
            "Empty when not module-bound. Use with ``space_id`` to call "
            "``get_document`` / ``get_document_parts``."
        ),
    )


class LinkedWorkItemsList(BaseModel):
    """All links (forward and back) for a work item.

    Returned by ``get_linked_work_items``.  Merges outgoing and incoming
    links into a single list for complete traceability.
    """

    items: list[LinkedWorkItemSummary] = Field(
        description="All linked work items (forward and back).",
    )
    total_count: int = Field(
        default=0,
        description="Total number of linked work items (forward + back)",
    )
    forward_count: int = Field(
        description="Number of outgoing (forward) links.",
    )
    back_count: int = Field(
        description="Number of incoming (back) links.",
    )


# ---------------------------------------------------------------------------
# Write-result models
# ---------------------------------------------------------------------------


class WorkItemCreateResult(BaseModel):
    """Result of a ``create_work_item`` operation."""

    created: bool = Field(
        description=(
            "True if the work item was actually created. False when dry_run is True."
        ),
    )
    dry_run: bool = Field(
        description="Whether this was a dry-run (preview only, no mutation).",
    )
    work_item_id: str | None = Field(
        description=(
            "ID of the created work item (e.g. 'MCPT-042'). None when dry_run is True."
        ),
    )
    payload_preview: dict[str, JsonValue] | None = Field(
        description=(
            "JSON:API request payload that was (or would be) sent. "
            "Usually populated for dry-run previews and may be None "
            "after a successful real operation."
        ),
    )


class WorkItemUpdateResult(BaseModel):
    """Result of an ``update_work_item`` operation."""

    updated: bool = Field(
        description=(
            "True if the work item was actually updated. False when dry_run is True."
        ),
    )
    dry_run: bool = Field(
        description="Whether this was a dry-run (preview only, no mutation).",
    )
    current: WorkItemDetail | None = Field(
        description=(
            "Current state of the work item before the update. "
            "Included so the LLM can verify what changed."
        ),
    )
    changes: dict[str, JsonValue] = Field(
        description="Map of field names to their new values in the PATCH payload.",
    )


class CommentResult(BaseModel):
    """Result of an ``add_document_comment`` operation."""

    created: bool = Field(
        description=(
            "True if the comment was actually created. False when dry_run is True."
        ),
    )
    dry_run: bool = Field(
        description="Whether this was a dry-run (preview only, no mutation).",
    )
    comment_id: str | None = Field(
        description=("ID of the created comment. None when dry_run is True."),
    )
    payload_preview: dict[str, JsonValue] | None = Field(
        description=(
            "JSON:API request payload that was (or would be) sent. "
            "Usually populated for dry-run previews and may be None "
            "after a successful real operation."
        ),
    )


class LinkResult(BaseModel):
    """Result of a ``link_work_items`` operation."""

    created: bool = Field(
        description=(
            "True if the link was actually created. False when dry_run is True."
        ),
    )
    dry_run: bool = Field(
        description="Whether this was a dry-run (preview only, no mutation).",
    )
    payload_preview: dict[str, JsonValue] | None = Field(
        description=(
            "JSON:API request payload that was (or would be) sent. "
            "Usually populated for dry-run previews and may be None "
            "after a successful real operation."
        ),
    )


class DocumentPartCreateResult(BaseModel):
    """Result of a ``create_document_part`` operation."""

    created: bool = Field(
        description=(
            "True if the document part was actually created. "
            "False when dry_run is True."
        ),
    )
    dry_run: bool = Field(
        description="Whether this was a dry-run (preview only, no mutation).",
    )
    part_id: str | None = Field(
        description=(
            "ID of the created document part "
            "(e.g. 'workitem_MCPT-042'). "
            "None when dry_run is True."
        ),
    )
    payload_preview: dict[str, JsonValue] | None = Field(
        description=(
            "JSON:API request payload that was (or would be) sent. "
            "Usually populated for dry-run previews and may be None "
            "after a successful real operation."
        ),
    )


class WorkItemMoveResult(BaseModel):
    """Result of a ``move_work_item_to_document`` operation."""

    moved: bool = Field(
        description=(
            "True if the work item was actually moved into the target "
            "document. False when dry_run is True."
        ),
    )
    dry_run: bool = Field(
        description="Whether this was a dry-run (preview only, no mutation).",
    )
    payload_preview: dict[str, JsonValue] | None = Field(
        description=(
            "Request payload that was (or would be) sent. "
            "Usually populated for dry-run previews and may be None "
            "after a successful real operation."
        ),
    )


__all__: list[str] = [
    "CommentResult",
    "DocumentDetail",
    "DocumentPart",
    "DocumentPartCreateResult",
    "DocumentSummary",
    "Hyperlink",
    "JsonValue",
    "LinkResult",
    "LinkedWorkItemSummary",
    "LinkedWorkItemsList",
    "PaginatedResult",
    "ProjectSummary",
    "WorkItemCreateResult",
    "WorkItemDetail",
    "WorkItemMoveResult",
    "WorkItemSummary",
    "WorkItemUpdateResult",
]
