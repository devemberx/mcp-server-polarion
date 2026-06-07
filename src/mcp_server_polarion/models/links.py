"""Work item link models — link views, create/delete/update specs and results."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Literal

from pydantic import BaseModel, Field, model_validator


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
