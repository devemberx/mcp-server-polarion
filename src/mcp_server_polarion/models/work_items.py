"""Work item models — summaries, details, create specs, and write results."""

from __future__ import annotations

from collections.abc import Mapping

from pydantic import BaseModel, Field, model_validator

from mcp_server_polarion.models.common import MAX_BODY_HTML_LEN


class SqlRecipeGallery(BaseModel):
    """Copy-paste SQL recipe gallery returned by ``get_sql_query_recipes``."""

    recipes: str


class WorkItemSummary(BaseModel):
    """Compact work-item representation for list and search results."""

    id: str
    title: str
    type: str
    status: str
    priority: str = ""
    updated: str = ""
    space_id: str = ""
    document_name: str = ""
    assignee_ids: list[str] = Field(default_factory=list)


class Hyperlink(BaseModel):
    """A single external hyperlink attached to a work item."""

    role: str
    title: str = ""
    uri: str


class WorkItemDetail(WorkItemSummary):
    """Full work-item details returned by ``get_work_item``."""

    description_html: str = ""
    project_id: str
    author_id: str = ""
    created: str = ""
    resolution: str = ""
    severity: str = ""
    outline_number: str = ""
    hyperlinks: list[Hyperlink] = Field(default_factory=list)
    custom_fields: dict[str, object] = Field(default_factory=dict)


class WorkItemRead(WorkItemSummary):
    """LLM-friendly work-item view returned by ``read_work_item``."""

    description: str = ""
    project_id: str
    author_id: str = ""
    created: str = ""
    resolution: str = ""
    severity: str = ""
    outline_number: str = ""
    hyperlinks: list[Hyperlink] = Field(default_factory=list)
    custom_fields: dict[str, object] = Field(default_factory=dict)


class WorkItemCreateSpec(BaseModel):
    """One work item to create via ``create_work_items``."""

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
    """Result of a ``create_work_items`` operation."""

    created: bool
    dry_run: bool
    work_item_ids: list[str] = Field(default_factory=list)
    payload_preview: Mapping[str, object] | None = None


class WorkItemUpdateSpec(BaseModel):
    """One work item to update via ``update_work_items``."""

    work_item_id: str = Field(min_length=1)
    title: str | None = None
    description_html: str | None = Field(default=None, max_length=MAX_BODY_HTML_LEN)
    status: str | None = None
    priority: str | None = None
    severity: str | None = None
    due_date: str | None = None
    initial_estimate: str | None = None
    resolution: str | None = None
    hyperlinks: list[Hyperlink] | None = None
    assignee_ids: list[str] | None = None
    custom_fields: dict[str, object] | None = None

    @model_validator(mode="after")
    def _at_least_one_field(self) -> WorkItemUpdateSpec:
        # Polarion 400s on a body-less resource even with workflowAction /
        # changeTypeTo query params, so every item must carry >=1 field.
        if not any(
            getattr(self, name)
            for name in type(self).model_fields
            if name != "work_item_id"
        ):
            raise ValueError(
                "Nothing to update -- each item needs at least one of title, "
                "description_html, status, priority, severity, due_date, "
                "initial_estimate, resolution, hyperlinks, assignee_ids, "
                "or custom_fields."
            )
        return self


class WorkItemsUpdateResult(BaseModel):
    """Result of an ``update_work_items`` operation."""

    updated: bool
    dry_run: bool
    work_item_ids: list[str] = Field(default_factory=list)
    payload_preview: Mapping[str, object] | None = None


class WorkItemMoveResult(BaseModel):
    """Result of a ``move_work_item_to_document`` or sibling move-document call."""

    moved: bool
    dry_run: bool
    payload_preview: Mapping[str, object] | None
