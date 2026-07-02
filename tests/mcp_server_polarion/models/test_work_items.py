"""Tests for work item models in ``mcp_server_polarion.models.work_items``."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from mcp_server_polarion.models import (
    Hyperlink,
    WorkItemDetail,
    WorkItemsCreateResult,
    WorkItemSummary,
    WorkItemsUpdateResult,
    WorkItemUpdateSpec,
)


class TestWorkItemSummary:
    def test_valid(self):
        work_item = WorkItemSummary(
            id="MCPT-001",
            title="Login Feature",
            type="requirement",
            status="draft",
        )
        assert work_item.id == "MCPT-001"
        assert work_item.status == "draft"

    def test_default_optional_fields(self):
        work_item = WorkItemSummary(
            id="MCPT-001",
            title="Login Feature",
            type="requirement",
            status="draft",
        )
        assert work_item.priority == ""
        assert work_item.updated == ""
        assert work_item.space_id == ""
        assert work_item.document_name == ""
        assert work_item.assignee_ids == []

    def test_full_metadata(self):
        work_item = WorkItemSummary(
            id="MCPT-042",
            title="Login Feature",
            type="requirement",
            status="approved",
            priority="90.0",
            updated="2026-04-29T10:23:00Z",
            space_id="Design",
            document_name="Software Requirement Specification",
            assignee_ids=["alice", "bob"],
        )
        assert work_item.priority == "90.0"
        assert work_item.updated == "2026-04-29T10:23:00Z"
        assert work_item.space_id == "Design"
        assert work_item.document_name == "Software Requirement Specification"
        assert work_item.assignee_ids == ["alice", "bob"]

    def test_various_types(self):
        for work_item_type in ("requirement", "task", "testCase", "defect"):
            work_item = WorkItemSummary(
                id="WI-1",
                title="Test",
                type=work_item_type,
                status="open",
            )
            assert work_item.type == work_item_type

    def test_missing_status(self):
        with pytest.raises(ValidationError):
            WorkItemSummary(  # type: ignore[call-arg]
                id="WI-1",
                title="Incomplete",
                type="task",
            )


class TestWorkItemDetail:
    def test_extends_summary(self):
        detail = WorkItemDetail(
            id="MCPT-001",
            title="Login Feature",
            type="requirement",
            status="approved",
            description_html="<h2>Login</h2><p>User must authenticate.</p>",
            project_id="myproject",
        )
        assert isinstance(detail, WorkItemSummary)
        assert detail.description_html == (
            "<h2>Login</h2><p>User must authenticate.</p>"
        )
        assert detail.project_id == "myproject"

    def test_empty_description(self):
        detail = WorkItemDetail(
            id="MCPT-002",
            title="Empty work item",
            type="task",
            status="draft",
            description_html="",
            project_id="proj1",
        )
        assert detail.description_html == ""

    def test_description_defaults_to_empty(self):
        # Defaults to "" for the include_description_html=False path.
        detail = WorkItemDetail(
            id="MCPT-003",
            title="No desc",
            type="task",
            status="draft",
            project_id="proj1",
        )
        assert detail.description_html == ""

    def test_inherits_summary_metadata(self):
        detail = WorkItemDetail(
            id="MCPT-100",
            title="Login Feature",
            type="requirement",
            status="approved",
            priority="50.0",
            updated="2026-04-30T01:00:00Z",
            space_id="Design",
            document_name="SRS",
            assignee_ids=["alice"],
            description_html="<p>body</p>",
            project_id="proj1",
        )
        assert detail.priority == "50.0"
        assert detail.space_id == "Design"
        assert detail.document_name == "SRS"
        assert detail.assignee_ids == ["alice"]

    def test_detail_default_optional_fields(self):
        detail = WorkItemDetail(
            id="MCPT-001",
            title="Minimal",
            type="task",
            status="open",
            description_html="",
            project_id="proj1",
        )
        assert detail.author_id == ""
        assert detail.created == ""
        assert detail.resolution == ""
        assert detail.severity == ""
        assert detail.outline_number == ""
        assert detail.hyperlinks == []
        assert detail.custom_fields == {}

    def test_custom_fields_round_trip_heterogeneous_values(self):
        # object-typed customs: primitives and rich-text dicts both round-trip.
        rich = {"type": "text/html", "value": "<p>note</p>"}
        detail = WorkItemDetail(
            id="MCPT-999",
            title="With customs",
            type="requirement",
            status="approved",
            description_html="<p>body</p>",
            project_id="proj1",
            custom_fields={
                "riskLevel": "high",
                "effortHours": 8.0,
                "approved": True,
                "richNote": rich,
            },
        )
        restored = WorkItemDetail.model_validate(detail.model_dump())
        assert restored.custom_fields == detail.custom_fields
        assert restored.custom_fields["richNote"] == rich

    def test_detail_specific_fields(self):
        detail = WorkItemDetail(
            id="MCPT-200",
            title="Login Bug",
            type="defect",
            status="closed",
            description_html="<p>repro steps</p>",
            project_id="proj1",
            author_id="alice",
            created="2026-04-01T09:00:00Z",
            resolution="fixed",
            severity="blocker",
            outline_number="2.3.1",
            hyperlinks=[
                Hyperlink(role="ref_ext", title="Spec", uri="https://example.com"),
            ],
        )
        assert detail.author_id == "alice"
        assert detail.created == "2026-04-01T09:00:00Z"
        assert detail.resolution == "fixed"
        assert detail.severity == "blocker"
        assert detail.outline_number == "2.3.1"
        assert len(detail.hyperlinks) == 1
        assert detail.hyperlinks[0].uri == "https://example.com"


class TestHyperlink:
    def test_valid(self):
        link = Hyperlink(
            role="ref_ext",
            title="Reference Spec",
            uri="https://example.com/spec",
        )
        assert link.role == "ref_ext"
        assert link.title == "Reference Spec"
        assert link.uri == "https://example.com/spec"

    def test_default_title(self):
        link = Hyperlink(role="ref_ext", uri="https://example.com")
        assert link.title == ""

    def test_missing_uri_rejected(self):
        with pytest.raises(ValidationError):
            Hyperlink(role="ref_ext")  # type: ignore[call-arg]


class TestWorkItemsCreateResult:
    def test_successful_create(self):
        result = WorkItemsCreateResult(
            created=True,
            dry_run=False,
            work_item_ids=["MCPT-042", "MCPT-043"],
            payload_preview=None,
        )
        assert result.created is True
        assert result.work_item_ids == ["MCPT-042", "MCPT-043"]

    def test_dry_run(self):
        result = WorkItemsCreateResult(
            created=False,
            dry_run=True,
            work_item_ids=[],
            payload_preview={
                "data": [
                    {
                        "type": "workitems",
                        "attributes": {"title": "New work item"},
                    }
                ]
            },
        )
        assert result.created is False
        assert result.dry_run is True
        assert result.work_item_ids == []
        assert result.payload_preview is not None


class TestWorkItemUpdateSpec:
    def test_single_field_is_enough(self):
        spec = WorkItemUpdateSpec(work_item_id="MCPT-001", title="New Title")
        assert spec.work_item_id == "MCPT-001"
        assert spec.title == "New Title"

    def test_no_updatable_field_raises(self):
        # Polarion 400s on a body-less resource; the spec fails closed instead.
        with pytest.raises(ValidationError, match="Nothing to update"):
            WorkItemUpdateSpec(work_item_id="MCPT-001")

    def test_empty_strings_do_not_count_as_fields(self):
        with pytest.raises(ValidationError, match="Nothing to update"):
            WorkItemUpdateSpec(work_item_id="MCPT-001", title="", status="")

    def test_empty_work_item_id_rejected(self):
        with pytest.raises(ValidationError):
            WorkItemUpdateSpec(work_item_id="", title="t")

    def test_description_html_rejects_overlong_input(self):
        # max_length=MAX_BODY_HTML_LEN caps runaway HTML (2 MiB + 1 rejected).
        with pytest.raises(ValidationError):
            WorkItemUpdateSpec(
                work_item_id="MCPT-001",
                description_html="x" * (2_000_000 + 1),
            )

    def test_custom_fields_alone_satisfies_validator(self):
        spec = WorkItemUpdateSpec(
            work_item_id="MCPT-001", custom_fields={"riskLevel": "high"}
        )
        assert spec.custom_fields == {"riskLevel": "high"}


class TestWorkItemsUpdateResult:
    def test_successful_update(self):
        result = WorkItemsUpdateResult(
            updated=True,
            dry_run=False,
            work_item_ids=["MCPT-001", "MCPT-002"],
            payload_preview=None,
        )
        assert result.updated is True
        assert result.work_item_ids == ["MCPT-001", "MCPT-002"]
        assert result.payload_preview is None

    def test_dry_run(self):
        result = WorkItemsUpdateResult(
            updated=False,
            dry_run=True,
            work_item_ids=[],
            payload_preview={
                "data": [
                    {
                        "type": "workitems",
                        "id": "proj1/MCPT-001",
                        "attributes": {"status": "approved"},
                    }
                ]
            },
        )
        assert result.updated is False
        assert result.dry_run is True
        assert result.work_item_ids == []
        assert result.payload_preview is not None
