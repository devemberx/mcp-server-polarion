"""Work item models — summaries, details, create specs, and write results."""

from __future__ import annotations

from collections.abc import Mapping

from pydantic import BaseModel, Field

from mcp_server_polarion.models.common import MAX_BODY_HTML_LEN


class SqlRecipeGallery(BaseModel):
    """Copy-paste SQL recipe gallery returned by ``get_sql_query_recipes``.

    ``recipes`` is a self-contained Markdown document: table schema plus
    parameterised recipes for document scope, custom-field search, and
    traceability. Adapt a recipe rather than hand-writing joins.
    """

    recipes: str


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


class WorkItemMoveResult(BaseModel):
    """Result of a ``move_work_item_to_document`` or sibling move-document call."""

    moved: bool
    dry_run: bool
    payload_preview: Mapping[str, object] | None
