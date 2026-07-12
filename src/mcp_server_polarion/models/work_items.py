"""Work item models — summaries, details, create specs, write results."""

from __future__ import annotations

from collections.abc import Mapping

from pydantic import BaseModel, ConfigDict, Field, model_validator

from mcp_server_polarion.models.common import MAX_BODY_HTML_LEN


class WorkItemSummary(BaseModel):
    """Compact work-item view for list + search results."""

    id: str
    title: str
    type: str
    status: str
    priority: str = ""
    updated: str = ""
    space_id: str = ""
    document_name: str = ""
    author_name: str = ""


class Hyperlink(BaseModel):
    """Single external hyperlink on a work item."""

    # LLM input model: reject typo keys, not silent-drop.
    model_config = ConfigDict(extra="forbid")

    role: str
    title: str = ""
    uri: str


class WorkItemDetail(WorkItemSummary):
    """Full work-item detail from ``get_work_item``."""

    description_html: str = ""
    project_id: str
    author_id: str = ""
    assignee_ids: list[str] = Field(default_factory=list)
    assignee_names: list[str] = Field(default_factory=list)
    created: str = ""
    resolution: str = ""
    severity: str = ""
    outline_number: str = ""
    hyperlinks: list[Hyperlink] = Field(default_factory=list)
    custom_fields: dict[str, object] = Field(default_factory=dict)


class WorkItemRead(WorkItemSummary):
    """LLM-friendly work-item view from ``read_work_item``."""

    description: str = ""
    project_id: str
    author_id: str = ""
    assignee_ids: list[str] = Field(default_factory=list)
    assignee_names: list[str] = Field(default_factory=list)
    created: str = ""
    resolution: str = ""
    severity: str = ""
    outline_number: str = ""
    hyperlinks: list[Hyperlink] = Field(default_factory=list)
    custom_fields: dict[str, object] = Field(default_factory=dict)


class WorkItemCreateSpec(BaseModel):
    """One work item to create via create_work_items."""

    model_config = ConfigDict(extra="forbid")

    title: str = Field(min_length=1)
    type: str = Field(min_length=1)
    description: str | None = Field(default=None, max_length=MAX_BODY_HTML_LEN)
    status: str | None = None
    priority: str | None = None
    severity: str | None = None
    assignee_ids: list[str] | None = None
    due_date: str | None = None
    initial_estimate: str | None = None
    hyperlinks: list[Hyperlink] | None = None
    custom_fields: dict[str, object] | None = None


class WorkItemsCreateResult(BaseModel):
    """``create_work_items`` result."""

    created: bool
    dry_run: bool
    work_item_ids: list[str] = Field(default_factory=list)
    payload_preview: Mapping[str, object] | None = None


class WorkItemUpdateSpec(BaseModel):
    """One update_work_items batch entry; unset fields stay unchanged."""

    model_config = ConfigDict(extra="forbid")

    work_item_id: str = Field(
        min_length=1, description="Work item ID (e.g. 'MCPT-042')."
    )
    title: str | None = None
    description_html: str | None = Field(
        default=None,
        max_length=MAX_BODY_HTML_LEN,
        description=(
            "Raw Polarion HTML from get_work_item("
            "include_description_html=True), sent verbatim. Adding a table, "
            "caption, or widget requires get_html_recipes first — build this "
            "field from its template; hand-written table markup is rejected."
        ),
    )
    status: str | None = Field(
        default=None,
        description="New status; prefer workflow_action for real transitions.",
    )
    priority: str | None = Field(default=None, description="e.g. '50.0'.")
    severity: str | None = None
    due_date: str | None = Field(default=None, description="'YYYY-MM-DD'.")
    initial_estimate: str | None = Field(
        default=None,
        description="Polarion duration (e.g. '5 1/2d', '1w 2d').",
    )
    resolution: str | None = Field(
        default=None,
        description="Prefer workflow_action so workflow rules apply.",
    )
    hyperlinks: list[Hyperlink] | None = Field(
        default=None,
        description=(
            "REPLACES the stored hyperlink list — to add one, resubmit every "
            "existing hyperlink plus the new entry."
        ),
    )
    assignee_ids: list[str] | None = Field(
        default=None,
        description="REPLACES the assignee list — pass the full list, not a delta.",
    )
    custom_fields: dict[str, object] | None = Field(
        default=None,
        description="Partial; rich-text values as {'type':'text/html','value':...}.",
    )

    @model_validator(mode="after")
    def _require_effective_change(self) -> WorkItemUpdateSpec:
        # None custom entries drop at payload build; attribute-less item 400 the batch.
        effective = (
            self.title
            or self.description_html
            or self.status
            or self.priority
            or self.severity
            or self.due_date
            or self.initial_estimate
            or self.resolution
            or self.hyperlinks
            or self.assignee_ids
            or (
                self.custom_fields
                and any(value is not None for value in self.custom_fields.values())
            )
        )
        if not effective:
            msg = (
                f"work item '{self.work_item_id}': no effective change -- set "
                "at least one field (custom_fields values of None are "
                "dropped); workflow_action/change_type_to also need >=1 body "
                "field per item."
            )
            raise ValueError(msg)
        return self


class WorkItemsUpdateResult(BaseModel):
    """``update_work_items`` result."""

    updated: bool
    dry_run: bool
    work_item_ids: list[str] = Field(default_factory=list)
    payload_preview: Mapping[str, object] | None = None


class WorkItemMoveResult(BaseModel):
    """Result of ``move_work_item_to_document`` or sibling move call."""

    moved: bool
    dry_run: bool
    payload_preview: Mapping[str, object] | None
