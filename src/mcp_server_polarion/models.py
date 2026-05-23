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

    items: list[T]
    total_count: int
    page: int
    page_size: int
    has_more: bool = Field(default=False, description="True if more pages follow.")


# ---------------------------------------------------------------------------
# Read models — summaries and details
# ---------------------------------------------------------------------------


class ProjectSummary(BaseModel):
    """Summary of a Polarion project returned by ``list_projects``."""

    id: str
    name: str
    active: bool = Field(
        default=True,
        description="False means archived; skip these when picking a target.",
    )


class EnumOption(BaseModel):
    """Single enum option returned by ``list_document_enum_options``.

    Captures only the attributes useful when an LLM picks a value before a
    write call. The Polarion schema carries several more fields (color,
    iconURL, columnWidth, createDefect, limited, minValue, oppositeName,
    parent, requiresSignatureForTestCaseExecution, templateWorkItem) that
    are intentionally not surfaced; add them on demand if a future caller
    needs them.
    """

    id: str = Field(
        description=(
            "Option id -- pass this verbatim to write tools (e.g. 'open',"
            " 'systemReqSpecification')."
        ),
    )
    name: str = Field(description="Human-readable display name.")
    description: str = Field(
        default="",
        description="Option description; empty when Polarion has none.",
    )
    default: bool = Field(
        default=False,
        description="True if Polarion uses this option when none is specified.",
    )
    hidden: bool = Field(
        default=False,
        description=(
            "True if the option is hidden in the UI;"
            " avoid selecting unless explicitly needed."
        ),
    )
    terminal: bool = Field(
        default=False,
        description="For status fields: True if this is a workflow end-state.",
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
    content_html: str = Field(
        default="",
        description=(
            "Document body (homePageContent) as raw Polarion HTML. Only "
            "populated when ``get_document`` is called with "
            "``include_homepage_content_html=True``; otherwise an empty "
            "string. The same shape round-trips back through "
            "``update_document(home_page_content_html=...)`` without "
            "lossy Markdown conversion. NOTE: incomplete for end-to-end "
            "reading — heading text and embedded work-item bodies live in "
            "separate work items, not in homePageContent; use "
            "``read_document`` for the assembled body."
        ),
    )
    custom_fields: dict[str, object] = Field(
        default_factory=dict,
        description=(
            "User-defined custom fields configured per project and "
            "document type. Keys are project-specific custom field IDs; "
            "values are heterogeneous (string, int, float, bool, list, "
            "or ``{'type': 'text/html', 'value': '<...>'}`` for rich-text "
            "custom fields — kept raw, NOT converted to Markdown, so the "
            "shape round-trips back to Polarion unchanged). Empty dict "
            "when no custom fields are populated on the document."
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
            "Part body in Markdown. Populated for 'normal' and 'wikiblock' "
            "parts; empty for 'heading' (text in ``title``, depth in "
            "``level``), 'workitem' (body in ``description``), and the "
            "'toc' / 'tof' / 'page_break' widget placeholders."
        ),
    )
    type: Literal[
        "heading",
        "workitem",
        "normal",
        "toc",
        "wikiblock",
        "tof",
        "page_break",
    ] = Field(
        description=(
            "Part type: 'heading', 'workitem', 'normal' (rich text), "
            "'toc' (table of contents widget), 'wikiblock' (wiki macro "
            "block), 'tof' (table of figures widget), or 'page_break'. "
            "'tof' and 'page_break' are inferred from the part ID prefix "
            "because Polarion reports both as plain 'normal'."
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
            "``get_work_item`` or ``list_work_item_links``."
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
    outline_number: str = Field(
        default="",
        description=(
            "Hierarchical position inside the document (e.g. '1.2.3'). "
            "Populated for 'heading' and 'workitem' parts when Polarion "
            "has assigned one; empty for prose and widget parts."
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
    """

    content: str = Field(
        description=(
            "Rendered Markdown for the parts on this page. "
            "Empty placeholder paragraphs from Polarion are skipped, "
            "and runs of blank lines are collapsed."
        ),
    )
    part_count: int = Field(
        description=(
            "Number of document parts on this page (i.e. parts consumed "
            "from ``read_document_parts``). Parts that produce no output "
            "(empty placeholder paragraphs, ``toc``) still count toward "
            "this — use it for pagination accounting, not chunk count."
        ),
    )
    page: int = Field(
        description="Current page number (1-based).",
    )
    page_size: int = Field(
        description="Maximum number of parts per page.",
    )
    total_parts: int = Field(
        description="Total number of parts across the entire document.",
    )
    has_more: bool = Field(
        default=False,
        description=(
            "True when there are more pages of parts after this one. "
            "Use to decide whether to call ``read_document`` again with "
            "``page_number + 1``."
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
            "``read_document_parts``."
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

    description_html: str = Field(
        default="",
        description=(
            "Raw Polarion HTML body for the round-trip pair "
            "``get_work_item(include_description_html=True)`` → "
            "``update_work_item(description_html=...)``. Empty when the "
            "work item has no description or when the read flag was "
            "False. Never feed through a Markdown converter or sanitizer "
            "(would strip Polarion-specific spans and break the round-trip)."
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
    custom_fields: dict[str, object] = Field(
        default_factory=dict,
        description=(
            "User-defined custom fields configured per project and "
            "work-item type. Keys are project-specific custom field IDs "
            "(e.g. 'riskLevel', 'effortHours'); values are heterogeneous "
            "(string, int, float, bool, list, or "
            "``{'type': 'text/html', 'value': '<...>'}`` for rich-text "
            "custom fields — kept raw, NOT converted to Markdown, so the "
            "shape round-trips back to Polarion unchanged). Empty dict "
            "when no custom fields are populated on the work item."
        ),
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
    """

    description: str = Field(
        default="",
        description=(
            "Work-item body rendered as Markdown. Empty when the item has "
            "no description. Read-only; do NOT feed to ``update_work_item``."
        ),
    )
    project_id: str = Field(
        description="Project that contains this work item.",
    )
    author_id: str = Field(
        default="",
        description="Short user ID of the author; empty when not reported.",
    )
    created: str = Field(
        default="",
        description="ISO-8601 creation timestamp; empty when not reported.",
    )
    resolution: str = Field(
        default="",
        description=(
            "Resolution outcome for closed items (e.g. 'fixed', 'wontfix'); "
            "empty otherwise."
        ),
    )
    severity: str = Field(
        default="",
        description=(
            "Severity classification, used for defects (e.g. 'blocker', "
            "'critical'); empty otherwise."
        ),
    )
    outline_number: str = Field(
        default="",
        description=(
            "Hierarchical position inside the containing document "
            "(e.g. '1.2.3'); empty when not part of a document."
        ),
    )
    hyperlinks: list[Hyperlink] = Field(
        default_factory=list,
        description="External hyperlinks attached to this work item.",
    )
    custom_fields: dict[str, object] = Field(
        default_factory=dict,
        description=(
            "User-defined custom fields as ``{fieldId: value}``. Rich-text "
            "values stay raw as ``{'type': 'text/html', 'value': '<...>'}`` "
            "dicts so this dict alone may be copied back into "
            "``update_work_item(custom_fields=...)``."
        ),
    )


class WorkItemLink(BaseModel):
    """A work item link with the target's summary metadata.

    ``direction='forward'`` is an outgoing link (this work item links to
    the target); ``'back'`` is an incoming link (the target links to this
    work item).
    """

    id: str = Field(
        description="Linked Work Item ID (e.g. 'MCPT-002').",
    )
    title: str = Field(
        description="Linked Work Item title.",
    )
    role: str | None = Field(
        default=None,
        description=(
            "Link role (e.g. 'parent', 'relates_to', 'verifies'); "
            "``None`` for back-direction links."
        ),
    )
    direction: Literal["forward", "back"] = Field(
        description=(
            "'forward' for outgoing links (this work item links to the target). "
            "'back' for incoming links (the target links to this work item)."
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
            "``get_document`` / ``read_document_parts``."
        ),
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
            "Post-update state of the work item, fetched after the PATCH "
            "succeeds. Included so the LLM can verify the change applied. "
            "None on dry-run."
        ),
    )
    changes: dict[str, JsonValue] = Field(
        description="Map of field names to their new values in the PATCH payload.",
    )
    payload_preview: dict[str, JsonValue] | None = Field(
        description=(
            "JSON:API request payload that was (or would be) sent. "
            "Usually populated for dry-run previews and may be None "
            "after a successful real operation."
        ),
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


class WorkItemLinkSpec(BaseModel):
    """One link to create under a source work item."""

    role: str = Field(
        min_length=1,
        description="Link role id (e.g. 'parent', 'relates_to', 'verifies').",
    )
    target_work_item_id: str = Field(
        min_length=1,
        description="Target work item ID (the link's incoming endpoint).",
    )
    target_project_id: str | None = Field(
        default=None,
        description="Target's project; defaults to the source's project.",
    )
    suspect: bool = Field(
        default=False,
        description="Mark the link as suspect (target changed since last review).",
    )
    revision: str | None = Field(
        default=None,
        description="Optional revision pin (current HEAD when omitted).",
    )


class WorkItemLinkRef(BaseModel):
    """One existing link identified for deletion."""

    role: str = Field(
        min_length=1,
        description="Link role id of the existing link; must match exactly.",
    )
    target_work_item_id: str = Field(
        min_length=1,
        description="Target work item ID (the link's incoming endpoint).",
    )
    target_project_id: str | None = Field(
        default=None,
        description="Target's project; defaults to the source's project.",
    )


class WorkItemLinksCreateResult(BaseModel):
    """Result of a ``create_work_item_links`` operation."""

    created: bool = Field(
        description="True if Polarion accepted the bulk create. False on dry_run.",
    )
    dry_run: bool = Field(
        description="Whether this was a dry-run (preview only, no mutation).",
    )
    link_ids: list[str] = Field(
        default_factory=list,
        description=(
            "Composite 5-segment link ids"
            " (``<srcProj>/<srcWI>/<role>/<tgtProj>/<tgtWI>``)"
            " returned by Polarion, in input order; empty on dry-run."
        ),
    )
    payload_preview: dict[str, JsonValue] | None = Field(
        default=None,
        description=(
            "JSON:API payload that was (or would be) sent;"
            " populated for dry-run, None after a real create."
        ),
    )


class WorkItemLinksDeleteResult(BaseModel):
    """Result of a ``delete_work_item_links`` operation."""

    deleted: bool = Field(
        description="True if Polarion accepted the bulk delete. False on dry_run.",
    )
    dry_run: bool = Field(
        description="Whether this was a dry-run (preview only, no mutation).",
    )
    link_ids: list[str] = Field(
        default_factory=list,
        description=(
            "Composite 5-segment link ids REQUESTED for deletion, in input"
            " order, reconstructed from the input refs. Polarion silently"
            " ignores body-level refs that do not match an existing link,"
            " so this echoes the request -- not necessarily what was"
            " actually deleted. Cross-check with ``list_work_item_links``"
            " if exact accounting is required."
        ),
    )
    payload_preview: dict[str, JsonValue] | None = Field(
        default=None,
        description=(
            "JSON:API payload that was (or would be) sent;"
            " populated for dry-run, None after a real delete."
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
    """Result of a ``move_work_item_to_document`` or sibling move-document call."""

    moved: bool = Field(
        description=(
            "True if the work item was actually moved (into the target "
            "document for moveToDocument, or detached to free-floating for "
            "moveFromDocument). False when dry_run is True."
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


class DocumentCreateResult(BaseModel):
    """Result of a ``create_document`` operation."""

    created: bool = Field(
        description=(
            "True if the document was actually created. False when dry_run is True."
        ),
    )
    dry_run: bool = Field(
        description="Whether this was a dry-run (preview only, no mutation).",
    )
    document_name: str | None = Field(
        description=(
            "Module name of the created document (e.g. 'MySpecV1'). "
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


class DocumentUpdateResult(BaseModel):
    """Result of an ``update_document`` operation."""

    updated: bool = Field(
        description=(
            "True if the document metadata was actually patched. "
            "False when dry_run is True."
        ),
    )
    dry_run: bool = Field(
        description="Whether this was a dry-run (preview only, no mutation).",
    )
    payload_preview: dict[str, JsonValue] | None = Field(
        description=(
            "JSON:API request payload that was (or would be) sent. "
            "Populated for dry-run; None after a successful real update."
        ),
    )


__all__: list[str] = [
    "CommentResult",
    "DocumentCreateResult",
    "DocumentDetail",
    "DocumentPart",
    "DocumentPartCreateResult",
    "DocumentReadResult",
    "DocumentSummary",
    "DocumentUpdateResult",
    "Hyperlink",
    "JsonValue",
    "PaginatedResult",
    "ProjectSummary",
    "WorkItemCreateResult",
    "WorkItemDetail",
    "WorkItemLink",
    "WorkItemLinkRef",
    "WorkItemLinkSpec",
    "WorkItemLinksCreateResult",
    "WorkItemLinksDeleteResult",
    "WorkItemMoveResult",
    "WorkItemRead",
    "WorkItemSummary",
    "WorkItemUpdateResult",
]
