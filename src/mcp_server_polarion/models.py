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
            "Full JSON:API part identifier "
            "(e.g. 'projectId/spaceId/documentName/heading_MCPT-001')."
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
    next_part_id: str = Field(
        default="",
        description=(
            "Full ID of the next part in the document order "
            "(e.g. 'projectId/spaceId/documentName/workitem_MCPT-002'). "
            "Empty string when this is the last part."
        ),
    )
    previous_part_id: str = Field(
        default="",
        description=(
            "Full ID of the previous part in the document order "
            "(e.g. 'projectId/spaceId/documentName/heading_MCPT-001'). "
            "Empty string when this is the first part."
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


__all__: list[str] = [
    "CommentResult",
    "DocumentDetail",
    "DocumentPart",
    "DocumentPartCreateResult",
    "DocumentSummary",
    "JsonValue",
    "LinkResult",
    "LinkedWorkItemSummary",
    "LinkedWorkItemsList",
    "PaginatedResult",
    "ProjectSummary",
    "WorkItemCreateResult",
    "WorkItemDetail",
    "WorkItemSummary",
    "WorkItemUpdateResult",
]
