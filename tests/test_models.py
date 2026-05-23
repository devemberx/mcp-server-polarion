"""Tests for Pydantic models defined in ``mcp_server_polarion.models``."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from mcp_server_polarion.models import (
    CommentResult,
    DocumentDetail,
    DocumentPart,
    DocumentPartCreateResult,
    DocumentSummary,
    Hyperlink,
    PaginatedResult,
    ProjectSummary,
    WorkItemCreateResult,
    WorkItemDetail,
    WorkItemLink,
    WorkItemLinkRef,
    WorkItemLinksCreateResult,
    WorkItemLinksDeleteResult,
    WorkItemLinkSpec,
    WorkItemRead,
    WorkItemSummary,
    WorkItemUpdateResult,
)


class TestPaginatedResult:
    """Tests for the generic ``PaginatedResult[T]`` wrapper."""

    def test_with_project_summaries(self):
        result = PaginatedResult[ProjectSummary](
            items=[
                ProjectSummary(id="proj1", name="Project One"),
                ProjectSummary(id="proj2", name="Project Two"),
            ],
            total_count=5,
            page=1,
            page_size=2,
        )
        assert len(result.items) == 2
        assert result.total_count == 5
        assert result.page == 1
        assert result.page_size == 2
        assert result.has_more is False
        assert result.items[0].id == "proj1"

    def test_with_work_item_summaries(self):
        result = PaginatedResult[WorkItemSummary](
            items=[
                WorkItemSummary(
                    id="MCPT-001",
                    title="Login Feature",
                    type="requirement",
                    status="draft",
                ),
            ],
            total_count=1,
            page=1,
            page_size=100,
        )
        assert result.items[0].type == "requirement"

    def test_has_more_true(self):
        result = PaginatedResult[ProjectSummary](
            items=[
                ProjectSummary(id="p1", name="P1"),
            ],
            total_count=10,
            page=1,
            page_size=1,
            has_more=True,
        )
        assert result.has_more is True

    def test_has_more_default_false(self):
        result = PaginatedResult[ProjectSummary](
            items=[],
            total_count=0,
            page=1,
            page_size=100,
        )
        assert result.has_more is False

    def test_empty_page(self):
        result = PaginatedResult[ProjectSummary](
            items=[],
            total_count=0,
            page=1,
            page_size=100,
        )
        assert result.items == []
        assert result.total_count == 0

    def test_serialization_round_trip(self):
        original = PaginatedResult[ProjectSummary](
            items=[ProjectSummary(id="p1", name="P1")],
            total_count=1,
            page=1,
            page_size=10,
        )
        data = original.model_dump()
        restored = PaginatedResult[ProjectSummary].model_validate(data)
        assert restored == original

    def test_json_schema_generation(self):
        schema = PaginatedResult[ProjectSummary].model_json_schema()
        assert "properties" in schema
        assert "items" in schema["properties"]
        assert "total_count" in schema["properties"]


class TestProjectSummary:
    def test_valid(self):
        p = ProjectSummary(id="myproject", name="My Project")
        assert p.id == "myproject"
        assert p.name == "My Project"

    def test_active_defaults_true(self):
        p = ProjectSummary(id="myproject", name="My Project")
        assert p.active is True

    def test_active_explicit_false(self):
        p = ProjectSummary(id="archived", name="Old Project", active=False)
        assert p.active is False

    def test_missing_id(self):
        with pytest.raises(ValidationError):
            ProjectSummary(name="No ID")  # type: ignore[call-arg]

    def test_missing_name(self):
        with pytest.raises(ValidationError):
            ProjectSummary(id="proj")  # type: ignore[call-arg]


class TestDocumentSummary:
    def test_valid(self):
        d = DocumentSummary(space_id="_default", document_name="SRS")
        assert d.space_id == "_default"
        assert d.document_name == "SRS"

    def test_custom_document_name(self):
        d = DocumentSummary(
            space_id="Design",
            document_name="Software Requirement Specification",
        )
        assert d.document_name == "Software Requirement Specification"


class TestDocumentDetail:
    def test_valid(self):
        d = DocumentDetail(
            title="Software Requirement Specification",
            content_html="<h2>Overview</h2><p>System requirements.</p>",
        )
        assert d.title == "Software Requirement Specification"
        assert "<h2>Overview</h2>" in d.content_html

    def test_empty_content(self):
        d = DocumentDetail(
            title="Empty Doc",
            content_html="",
        )
        assert d.content_html == ""

    def test_missing_required_field(self):
        with pytest.raises(ValidationError):
            DocumentDetail()  # type: ignore[call-arg]

    def test_custom_fields_default_empty(self):
        d = DocumentDetail(title="Doc", content_html="")
        assert d.custom_fields == {}

    def test_custom_fields_round_trip(self):
        rich = {"type": "text/html", "value": "<p>x</p>"}
        d = DocumentDetail(
            title="Doc",
            content_html="",
            custom_fields={"reviewedBy": "alice", "richNote": rich},
        )
        restored = DocumentDetail.model_validate(d.model_dump())
        assert restored.custom_fields == d.custom_fields
        assert restored.custom_fields["richNote"] == rich


class TestDocumentPart:
    def test_heading_part(self):
        part = DocumentPart(
            id="heading_MCPT-001",
            title="Introduction",
            content="",
            type="heading",
            level=1,
        )
        assert part.type == "heading"
        assert part.level == 1
        assert part.description == ""
        assert part.next_part_id == ""
        assert part.work_item_id == ""
        assert part.work_item_type == ""
        assert part.work_item_status == ""
        assert part.external is False

    def test_workitem_part(self):
        part = DocumentPart(
            id="workitem_MCPT-042",
            title="Login Requirement",
            content="",
            type="workitem",
            level=0,
        )
        assert part.type == "workitem"
        assert part.level == 0

    def test_workitem_part_with_all_fields(self):
        part = DocumentPart(
            id="workitem_MCPT-042",
            title="Login Requirement",
            content="",
            type="workitem",
            level=0,
            description="Must support SSO.",
            work_item_id="MCPT-042",
            work_item_type="requirement",
            work_item_status="approved",
            external=True,
            next_part_id="proj/space/doc/heading_MCPT-043",
        )
        assert part.description == "Must support SSO."
        assert part.work_item_id == "MCPT-042"
        assert part.work_item_type == "requirement"
        assert part.work_item_status == "approved"
        assert part.external is True
        assert part.next_part_id == "proj/space/doc/heading_MCPT-043"

    def test_serialization(self):
        part = DocumentPart(
            id="heading_MCPT-010",
            title="Scope",
            content="",
            type="heading",
            level=2,
        )
        data = part.model_dump()
        assert data["id"] == "heading_MCPT-010"
        assert data["level"] == 2
        assert data["next_part_id"] == ""
        assert data["work_item_id"] == ""
        assert data["work_item_type"] == ""
        assert data["work_item_status"] == ""
        assert data["external"] is False
        assert "previous_part_id" not in data

    def test_invalid_type_rejected(self):
        with pytest.raises(ValidationError):
            DocumentPart(
                id="part-1",
                title="Bad",
                content="",
                type="invalid",
                level=0,
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
        # The HTML field defaults to "" so callers can omit it on the
        # ``include_description_html=False`` (blank) path.
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
        # Custom-field values are intentionally `object`-typed: primitives
        # and rich-text `{type: text/html, value: ...}` dicts must both
        # survive a serialization round-trip unchanged.
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


class TestWorkItemCreateResult:
    def test_successful_create(self):
        result = WorkItemCreateResult(
            created=True,
            dry_run=False,
            work_item_id="MCPT-042",
            payload_preview={"data": {"type": "workitems"}},
        )
        assert result.created is True
        assert result.work_item_id == "MCPT-042"

    def test_dry_run(self):
        result = WorkItemCreateResult(
            created=False,
            dry_run=True,
            work_item_id=None,
            payload_preview={
                "data": {
                    "type": "workitems",
                    "attributes": {"title": "New work item"},
                }
            },
        )
        assert result.created is False
        assert result.dry_run is True
        assert result.work_item_id is None
        assert result.payload_preview is not None


class TestWorkItemUpdateResult:
    def test_successful_update(self):
        current = WorkItemDetail(
            id="MCPT-001",
            title="Old Title",
            type="requirement",
            status="draft",
            description_html="<p>Old desc</p>",
            project_id="proj1",
        )
        result = WorkItemUpdateResult(
            updated=True,
            dry_run=False,
            current=current,
            changes={"title": "New Title"},
            payload_preview=None,
        )
        assert result.updated is True
        assert result.current is not None
        assert result.current.title == "Old Title"
        assert result.changes["title"] == "New Title"
        assert result.payload_preview is None

    def test_dry_run(self):
        result = WorkItemUpdateResult(
            updated=False,
            dry_run=True,
            current=None,
            changes={"status": "approved"},
            payload_preview={
                "data": {
                    "type": "workitems",
                    "id": "proj1/MCPT-001",
                    "attributes": {"status": "approved"},
                }
            },
        )
        assert result.updated is False
        assert result.dry_run is True
        assert result.payload_preview is not None


class TestCommentResult:
    def test_successful_create(self):
        result = CommentResult(
            created=True,
            dry_run=False,
            comment_id="comment-123",
            payload_preview=None,
        )
        assert result.created is True
        assert result.comment_id == "comment-123"

    def test_dry_run(self):
        result = CommentResult(
            created=False,
            dry_run=True,
            comment_id=None,
            payload_preview={
                "data": {
                    "type": "document_comments",
                    "attributes": {
                        "text": {"type": "text/html", "value": "<p>Note</p>"},
                    },
                }
            },
        )
        assert result.dry_run is True
        assert result.comment_id is None


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


class TestDocumentPartCreateResult:
    def test_successful_create(self):
        result = DocumentPartCreateResult(
            created=True,
            dry_run=False,
            part_id="workitem_MCPT-042",
            payload_preview=None,
        )
        assert result.created is True
        assert result.part_id == "workitem_MCPT-042"

    def test_dry_run(self):
        result = DocumentPartCreateResult(
            created=False,
            dry_run=True,
            part_id=None,
            payload_preview={
                "data": [
                    {
                        "type": "document_parts",
                        "relationships": {
                            "workItem": {
                                "data": {"type": "workitems", "id": "proj/MCPT-042"}
                            }
                        },
                    }
                ]
            },
        )
        assert result.dry_run is True
        assert result.part_id is None


class TestCrossModelIntegration:
    """Ensure models compose correctly as they would in real tool usage."""

    def test_paginated_work_items_json_round_trip(self):
        page = PaginatedResult[WorkItemSummary](
            items=[
                WorkItemSummary(
                    id="MCPT-001",
                    title="Feature A",
                    type="requirement",
                    status="approved",
                ),
                WorkItemSummary(
                    id="MCPT-002",
                    title="Bug B",
                    type="defect",
                    status="open",
                ),
            ],
            total_count=42,
            page=1,
            page_size=100,
        )
        json_str = page.model_dump_json()
        restored = PaginatedResult[WorkItemSummary].model_validate_json(json_str)
        assert restored.total_count == 42
        assert restored.items[1].id == "MCPT-002"

    def test_paginated_document_parts(self):
        page = PaginatedResult[DocumentPart](
            items=[
                DocumentPart(
                    id="heading_MCPT-001",
                    title="Chapter 1",
                    content="",
                    type="heading",
                    level=1,
                ),
            ],
            total_count=1,
            page=1,
            page_size=100,
        )
        assert page.items[0].type == "heading"

    def test_update_result_with_nested_detail(self):
        result = WorkItemUpdateResult(
            updated=True,
            dry_run=False,
            current=WorkItemDetail(
                id="MCPT-001",
                title="Before",
                type="requirement",
                status="draft",
                description_html="<p>Old description</p>",
                project_id="proj1",
            ),
            changes={"title": "After", "status": "approved"},
            payload_preview=None,
        )
        dumped = result.model_dump()
        assert dumped["current"]["title"] == "Before"
        assert dumped["changes"]["title"] == "After"

    def test_workitemread_metadata_mirrors_workitemdetail(self):
        """Drift between the two models makes ``read_work_item`` silently
        return less than ``get_work_item``; they must match exactly except
        for the body field (``description_html`` vs ``description``)."""
        detail_meta = set(WorkItemDetail.model_fields) - {"description_html"}
        read_meta = set(WorkItemRead.model_fields) - {"description"}
        assert detail_meta == read_meta, (
            "WorkItemRead and WorkItemDetail metadata fields drifted — "
            f"only in WorkItemDetail: {detail_meta - read_meta}; "
            f"only in WorkItemRead: {read_meta - detail_meta}"
        )

    def test_field_descriptions_are_non_empty_when_set(self):
        """When a model field carries a ``Field(description=...)`` it must be
        non-empty; fields whose name alone is unambiguous may omit it."""
        models = [
            ProjectSummary,
            DocumentSummary,
            DocumentDetail,
            DocumentPart,
            WorkItemSummary,
            WorkItemDetail,
            WorkItemRead,
            WorkItemLink,
            WorkItemCreateResult,
            WorkItemUpdateResult,
            CommentResult,
            WorkItemLinkSpec,
            WorkItemLinkRef,
            WorkItemLinksCreateResult,
            WorkItemLinksDeleteResult,
            DocumentPartCreateResult,
        ]
        for model_cls in models:
            schema = model_cls.model_json_schema()
            properties = schema.get("properties", {})
            for field_name, field_schema in properties.items():
                description = field_schema.get("description")
                if description is None:
                    continue
                assert description.strip(), (
                    f"{model_cls.__name__}.{field_name} has an empty "
                    f"Field(description=...)"
                )
