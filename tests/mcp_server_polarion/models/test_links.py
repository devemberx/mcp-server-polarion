"""Tests for work item link models in ``mcp_server_polarion.models.links``."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from mcp_server_polarion.models import (
    WorkItemLink,
    WorkItemLinkRef,
    WorkItemLinksCreateResult,
    WorkItemLinksDeleteResult,
    WorkItemLinkSpec,
)


class TestWorkItemLink:
    def test_forward_link(self):
        link = WorkItemLink(
            id="MCPT-002",
            title="Child Requirement",
            role="parent",
            direction="forward",
            suspect=False,
        )
        assert link.direction == "forward"
        assert link.suspect is False

    def test_back_link_suspect(self):
        link = WorkItemLink(
            id="MCPT-003",
            title="Changed Requirement",
            role="verifies",
            direction="back",
            suspect=True,
        )
        assert link.suspect is True
        assert link.direction == "back"

    def test_invalid_direction_rejected(self):
        with pytest.raises(ValidationError):
            WorkItemLink(
                id="MCPT-004",
                title="Bad",
                role="parent",
                direction="sideways",
                suspect=False,
            )

    def test_role_defaults_none_and_metadata_defaults_empty(self):
        link = WorkItemLink(
            id="MCPT-005",
            title="Minimal",
            direction="back",
            suspect=False,
        )
        assert link.role is None
        assert link.type == ""
        assert link.status == ""
        assert link.space_id == ""
        assert link.document_name == ""

    def test_full_metadata(self):
        link = WorkItemLink(
            id="MCPT-006",
            title="Login Feature",
            role="verifies",
            direction="forward",
            suspect=False,
            type="testCase",
            status="passed",
            space_id="Design",
            document_name="Software Test Case Specification",
        )
        assert link.type == "testCase"
        assert link.status == "passed"
        assert link.space_id == "Design"
        assert link.document_name == "Software Test Case Specification"


class TestWorkItemLinkSpec:
    def test_minimal_spec(self):
        spec = WorkItemLinkSpec(role="parent", target_work_item_id="MCPT-2")
        assert spec.role == "parent"
        assert spec.target_work_item_id == "MCPT-2"
        assert spec.target_project_id is None
        assert spec.suspect is False
        assert spec.revision is None

    def test_full_spec(self):
        spec = WorkItemLinkSpec(
            role="verifies",
            target_work_item_id="MCPT-3",
            target_project_id="OtherProj",
            suspect=True,
            revision="1234",
        )
        assert spec.target_project_id == "OtherProj"
        assert spec.suspect is True
        assert spec.revision == "1234"

    def test_role_min_length(self):
        with pytest.raises(ValidationError):
            WorkItemLinkSpec(role="", target_work_item_id="MCPT-2")

    def test_target_work_item_id_min_length(self):
        with pytest.raises(ValidationError):
            WorkItemLinkSpec(role="parent", target_work_item_id="")


class TestWorkItemLinkRef:
    def test_minimal_ref(self):
        ref = WorkItemLinkRef(role="parent", target_work_item_id="MCPT-2")
        assert ref.role == "parent"
        assert ref.target_work_item_id == "MCPT-2"
        assert ref.target_project_id is None

    def test_cross_project_ref(self):
        ref = WorkItemLinkRef(
            role="verifies",
            target_work_item_id="MCPT-3",
            target_project_id="OtherProj",
        )
        assert ref.target_project_id == "OtherProj"

    def test_role_min_length(self):
        with pytest.raises(ValidationError):
            WorkItemLinkRef(role="", target_work_item_id="MCPT-2")


class TestWorkItemLinksCreateResult:
    def test_successful_create(self):
        result = WorkItemLinksCreateResult(
            created=True,
            dry_run=False,
            link_ids=[
                "MyProj/MCPT-1/parent/MyProj/MCPT-2",
                "MyProj/MCPT-1/verifies/MyProj/MCPT-3",
            ],
            payload_preview=None,
        )
        assert result.created is True
        assert result.link_ids == [
            "MyProj/MCPT-1/parent/MyProj/MCPT-2",
            "MyProj/MCPT-1/verifies/MyProj/MCPT-3",
        ]

    def test_dry_run(self):
        result = WorkItemLinksCreateResult(
            created=False,
            dry_run=True,
            payload_preview={
                "data": [
                    {
                        "type": "linkedworkitems",
                        "attributes": {"role": "parent"},
                    }
                ]
            },
        )
        assert result.dry_run is True
        assert result.link_ids == []
        assert result.payload_preview is not None

    def test_link_ids_default_empty(self):
        result = WorkItemLinksCreateResult(
            created=False,
            dry_run=True,
            payload_preview=None,
        )
        assert result.link_ids == []


class TestWorkItemLinksDeleteResult:
    def test_successful_delete(self):
        result = WorkItemLinksDeleteResult(
            deleted=True,
            dry_run=False,
            link_ids=["MyProj/MCPT-1/parent/MyProj/MCPT-2"],
            payload_preview=None,
        )
        assert result.deleted is True
        assert result.link_ids == ["MyProj/MCPT-1/parent/MyProj/MCPT-2"]

    def test_dry_run(self):
        result = WorkItemLinksDeleteResult(
            deleted=False,
            dry_run=True,
            link_ids=["MyProj/MCPT-1/parent/MyProj/MCPT-2"],
            payload_preview={
                "data": [
                    {
                        "type": "linkedworkitems",
                        "id": "MyProj/MCPT-1/parent/MyProj/MCPT-2",
                    }
                ]
            },
        )
        assert result.dry_run is True
        assert result.deleted is False
        assert result.payload_preview is not None
