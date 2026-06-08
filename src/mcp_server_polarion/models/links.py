"""Work item link models — link views, create/delete/update specs and results."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Literal

from pydantic import BaseModel, Field, model_validator


class WorkItemLink(BaseModel):
    """A work item link with the target's summary metadata."""

    id: str
    title: str
    role: str | None = None
    direction: Literal["forward", "back"]
    suspect: bool
    type: str = ""
    status: str = ""
    space_id: str = ""
    document_name: str = ""


class WorkItemLinkSpec(BaseModel):
    """One link to create under a source work item."""

    role: str = Field(min_length=1)
    target_work_item_id: str = Field(min_length=1)
    target_project_id: str | None = None
    suspect: bool = False
    revision: str | None = None


class WorkItemLinkRef(BaseModel):
    """One existing link identified for deletion."""

    role: str = Field(min_length=1)
    target_work_item_id: str = Field(min_length=1)
    target_project_id: str | None = None


class WorkItemLinksCreateResult(BaseModel):
    """Result of a ``create_work_item_links`` operation."""

    created: bool
    dry_run: bool
    link_ids: list[str] = Field(default_factory=list)
    payload_preview: Mapping[str, object] | None = None


class WorkItemLinksDeleteResult(BaseModel):
    """Result of a ``delete_work_item_links`` operation."""

    deleted: bool
    dry_run: bool
    link_ids: list[str] = Field(default_factory=list)
    deleted_link_ids: list[str] = Field(default_factory=list)
    not_found_link_ids: list[str] = Field(default_factory=list)
    payload_preview: Mapping[str, object] | None = None


class WorkItemLinkUpdateSpec(BaseModel):
    """One existing link to update; ``suspect``/``revision`` tri-state, ≥1 set."""

    role: str = Field(min_length=1)
    target_work_item_id: str = Field(min_length=1)
    target_project_id: str | None = None
    suspect: bool | None = None
    revision: str | None = None

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
    link_id: str
    payload_preview: Mapping[str, object] | None
