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

from pydantic import BaseModel, Field

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


class SpaceSummary(BaseModel):
    """Summary of a Polarion space returned by ``list_spaces``."""

    id: str = Field(
        description="Space identifier (e.g. '_default', 'Design').",
    )
    name: str = Field(
        description=(
            "Human-readable space name. "
            "Defaults to the space ID when no display name is available."
        ),
    )


class DocumentDetail(BaseModel):
    """Full details of a Polarion document returned by ``get_document``."""

    id: str = Field(
        description="Document identifier within the space.",
    )
    title: str = Field(
        description="Document title.",
    )
    description: str = Field(
        description=(
            "Document description converted to Markdown. "
            "Empty string when the document has no description."
        ),
    )
    space_id: str = Field(
        description="Space that contains this document.",
    )
    project_id: str = Field(
        description="Project that contains this document.",
    )


class DocumentPart(BaseModel):
    """A single part (heading or work item) within a Polarion document."""

    id: str = Field(
        description=(
            "Part identifier (e.g. 'heading_MCPT-001' or 'workitem_MCPT-001')."
        ),
    )
    title: str = Field(
        description="Part title or heading text.",
    )
    content: str = Field(
        description=(
            "Part body content converted to Markdown. "
            "Empty string when the part has no body content."
        ),
    )
    type: str = Field(
        description="Part type: 'heading' or 'workitem'.",
    )
    level: int = Field(
        description=(
            "Heading level (1-4) for heading parts. "
            "0 for work-item parts that have no heading level."
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


class WorkItemDetail(WorkItemSummary):
    """Full work-item details returned by ``get_work_item``.

    Extends ``WorkItemSummary`` with the description and project context.
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


class LinkedWorkItemSummary(BaseModel):
    """A single linked work item with its link metadata."""

    id: str = Field(
        description="Linked Work Item ID (e.g. 'MCPT-002').",
    )
    title: str = Field(
        description="Linked Work Item title.",
    )
    role: str = Field(
        description=("Link role identifier (e.g. 'parent', 'relates_to', 'verifies')."),
    )
    direction: str = Field(
        description="Link direction: 'forward' or 'back'.",
    )
    suspect: bool = Field(
        description=(
            "Whether the link is marked as suspect. "
            "Suspect links indicate that the linked item has changed "
            "since the link was last reviewed."
        ),
    )


class LinkedWorkItemsList(BaseModel):
    """Combined forward and back links for a work item.

    Returned by ``get_linked_work_items``.  Merges both directions into
    a single result for complete traceability.
    """

    items: list[LinkedWorkItemSummary] = Field(
        description="All linked work items (both forward and back links).",
    )
    forward_count: int = Field(
        description="Number of forward (outgoing) links.",
    )
    back_count: int = Field(
        description="Number of back (incoming) links.",
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
    payload_preview: dict[str, object] | None = Field(
        description=(
            "JSON:API request payload that was (or would be) sent. "
            "Present for both real and dry-run operations."
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
    changes: dict[str, object] = Field(
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
    payload_preview: dict[str, object] | None = Field(
        description=(
            "JSON:API request payload that was (or would be) sent. "
            "Present for both real and dry-run operations."
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
    payload_preview: dict[str, object] | None = Field(
        description=(
            "JSON:API request payload that was (or would be) sent. "
            "Present for both real and dry-run operations."
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
    payload_preview: dict[str, object] | None = Field(
        description=(
            "JSON:API request payload that was (or would be) sent. "
            "Present for both real and dry-run operations."
        ),
    )


__all__: list[str] = [
    "CommentResult",
    "DocumentDetail",
    "DocumentPart",
    "DocumentPartCreateResult",
    "LinkResult",
    "LinkedWorkItemSummary",
    "LinkedWorkItemsList",
    "PaginatedResult",
    "ProjectSummary",
    "SpaceSummary",
    "WorkItemCreateResult",
    "WorkItemDetail",
    "WorkItemSummary",
    "WorkItemUpdateResult",
]
