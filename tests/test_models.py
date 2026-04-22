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
    LinkedWorkItemsList,
    LinkedWorkItemSummary,
    LinkResult,
    PaginatedResult,
    ProjectSummary,
    WorkItemCreateResult,
    WorkItemDetail,
    WorkItemSummary,
    WorkItemUpdateResult,
)

# ---------------------------------------------------------------------------
# PaginatedResult[T]
# ---------------------------------------------------------------------------


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


# ---------------------------------------------------------------------------
# ProjectSummary
# ---------------------------------------------------------------------------


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


# ---------------------------------------------------------------------------
# DocumentSummary
# ---------------------------------------------------------------------------


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


# ---------------------------------------------------------------------------
# DocumentDetail
# ---------------------------------------------------------------------------


class TestDocumentDetail:
    def test_valid(self):
        d = DocumentDetail(
            id="SRS",
            title="Software Requirement Specification",
            content="## Overview\n\nSystem requirements.",
            space_id="_default",
            project_id="myproject",
        )
        assert d.id == "SRS"
        assert d.title == "Software Requirement Specification"
        assert d.space_id == "_default"

    def test_empty_content(self):
        d = DocumentDetail(
            id="doc1",
            title="Empty Doc",
            content="",
            space_id="space1",
            project_id="proj1",
        )
        assert d.content == ""

    def test_missing_required_field(self):
        with pytest.raises(ValidationError):
            DocumentDetail(  # type: ignore[call-arg]
                id="doc1",
                title="Missing Fields",
            )


# ---------------------------------------------------------------------------
# DocumentPart
# ---------------------------------------------------------------------------


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
        assert part.previous_part_id == ""

    def test_workitem_part(self):
        part = DocumentPart(
            id="workitem_MCPT-042",
            title="Login Requirement",
            content="The system **shall** allow login.",
            type="workitem",
            level=0,
        )
        assert part.type == "workitem"
        assert part.level == 0

    def test_workitem_part_with_all_fields(self):
        part = DocumentPart(
            id="workitem_MCPT-042",
            title="Login Requirement",
            content="The system **shall** allow login.",
            type="workitem",
            level=0,
            description="Must support SSO.",
            next_part_id="proj/space/doc/heading_MCPT-043",
            previous_part_id="proj/space/doc/heading_MCPT-041",
        )
        assert part.description == "Must support SSO."
        assert part.next_part_id == "proj/space/doc/heading_MCPT-043"
        assert part.previous_part_id == "proj/space/doc/heading_MCPT-041"

    def test_serialization(self):
        part = DocumentPart(
            id="heading_MCPT-010",
            title="Scope",
            content="Project scope.",
            type="heading",
            level=2,
        )
        data = part.model_dump()
        assert data["id"] == "heading_MCPT-010"
        assert data["level"] == 2
        assert data["next_part_id"] == ""
        assert data["previous_part_id"] == ""

    def test_invalid_type_rejected(self):
        with pytest.raises(ValidationError):
            DocumentPart(
                id="part-1",
                title="Bad",
                content="",
                type="invalid",
                level=0,
            )


# ---------------------------------------------------------------------------
# WorkItemSummary
# ---------------------------------------------------------------------------


class TestWorkItemSummary:
    def test_valid(self):
        wi = WorkItemSummary(
            id="MCPT-001",
            title="Login Feature",
            type="requirement",
            status="draft",
        )
        assert wi.id == "MCPT-001"
        assert wi.status == "draft"

    def test_various_types(self):
        for wi_type in ("requirement", "task", "testCase", "defect"):
            wi = WorkItemSummary(
                id="WI-1",
                title="Test",
                type=wi_type,
                status="open",
            )
            assert wi.type == wi_type

    def test_missing_status(self):
        with pytest.raises(ValidationError):
            WorkItemSummary(  # type: ignore[call-arg]
                id="WI-1",
                title="Incomplete",
                type="task",
            )


# ---------------------------------------------------------------------------
# WorkItemDetail
# ---------------------------------------------------------------------------


class TestWorkItemDetail:
    def test_extends_summary(self):
        detail = WorkItemDetail(
            id="MCPT-001",
            title="Login Feature",
            type="requirement",
            status="approved",
            description="## Login\n\nUser must authenticate.",
            project_id="myproject",
        )
        assert isinstance(detail, WorkItemSummary)
        assert detail.description == "## Login\n\nUser must authenticate."
        assert detail.project_id == "myproject"

    def test_empty_description(self):
        detail = WorkItemDetail(
            id="MCPT-002",
            title="Empty WI",
            type="task",
            status="draft",
            description="",
            project_id="proj1",
        )
        assert detail.description == ""


# ---------------------------------------------------------------------------
# LinkedWorkItemSummary
# ---------------------------------------------------------------------------


class TestLinkedWorkItemSummary:
    def test_forward_link(self):
        link = LinkedWorkItemSummary(
            id="MCPT-002",
            title="Child Requirement",
            role="parent",
            direction="forward",
            suspect=False,
        )
        assert link.direction == "forward"
        assert link.suspect is False

    def test_back_link_suspect(self):
        link = LinkedWorkItemSummary(
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
            LinkedWorkItemSummary(
                id="MCPT-004",
                title="Bad",
                role="parent",
                direction="sideways",
                suspect=False,
            )


# ---------------------------------------------------------------------------
# LinkedWorkItemsList
# ---------------------------------------------------------------------------


class TestLinkedWorkItemsList:
    def test_merged_links(self):
        result = LinkedWorkItemsList(
            items=[
                LinkedWorkItemSummary(
                    id="MCPT-002",
                    title="Child",
                    role="parent",
                    direction="forward",
                    suspect=False,
                ),
                LinkedWorkItemSummary(
                    id="MCPT-003",
                    title="Verifier",
                    role="verifies",
                    direction="back",
                    suspect=True,
                ),
            ],
            forward_count=1,
            back_count=1,
            total_count=2,
        )
        assert len(result.items) == 2
        assert result.forward_count == 1
        assert result.back_count == 1
        assert result.total_count == 2

    def test_empty_links(self):
        result = LinkedWorkItemsList(
            items=[],
            forward_count=0,
            back_count=0,
        )
        assert result.items == []
        assert result.total_count == 0


# ---------------------------------------------------------------------------
# WorkItemCreateResult
# ---------------------------------------------------------------------------


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
                    "attributes": {"title": "New WI"},
                }
            },
        )
        assert result.created is False
        assert result.dry_run is True
        assert result.work_item_id is None
        assert result.payload_preview is not None


# ---------------------------------------------------------------------------
# WorkItemUpdateResult
# ---------------------------------------------------------------------------


class TestWorkItemUpdateResult:
    def test_successful_update(self):
        current = WorkItemDetail(
            id="MCPT-001",
            title="Old Title",
            type="requirement",
            status="draft",
            description="Old desc",
            project_id="proj1",
        )
        result = WorkItemUpdateResult(
            updated=True,
            dry_run=False,
            current=current,
            changes={"title": "New Title"},
        )
        assert result.updated is True
        assert result.current is not None
        assert result.current.title == "Old Title"
        assert result.changes["title"] == "New Title"

    def test_dry_run(self):
        result = WorkItemUpdateResult(
            updated=False,
            dry_run=True,
            current=None,
            changes={"status": "approved"},
        )
        assert result.updated is False
        assert result.dry_run is True


# ---------------------------------------------------------------------------
# CommentResult
# ---------------------------------------------------------------------------


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


# ---------------------------------------------------------------------------
# LinkResult
# ---------------------------------------------------------------------------


class TestLinkResult:
    def test_successful_create(self):
        result = LinkResult(
            created=True,
            dry_run=False,
            payload_preview=None,
        )
        assert result.created is True

    def test_dry_run(self):
        result = LinkResult(
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
        assert result.payload_preview is not None


# ---------------------------------------------------------------------------
# DocumentPartCreateResult
# ---------------------------------------------------------------------------


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


# ---------------------------------------------------------------------------
# Cross-model integration
# ---------------------------------------------------------------------------


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
                description="Old description",
                project_id="proj1",
            ),
            changes={"title": "After", "status": "approved"},
        )
        dumped = result.model_dump()
        assert dumped["current"]["title"] == "Before"
        assert dumped["changes"]["title"] == "After"

    def test_all_models_have_field_descriptions(self):
        """Every field in every model must have a description for LLM docs."""
        models = [
            ProjectSummary,
            DocumentSummary,
            DocumentDetail,
            DocumentPart,
            WorkItemSummary,
            WorkItemDetail,
            LinkedWorkItemSummary,
            LinkedWorkItemsList,
            WorkItemCreateResult,
            WorkItemUpdateResult,
            CommentResult,
            LinkResult,
            DocumentPartCreateResult,
        ]
        for model_cls in models:
            schema = model_cls.model_json_schema()
            properties = schema.get("properties", {})
            for field_name, field_schema in properties.items():
                assert "description" in field_schema, (
                    f"{model_cls.__name__}.{field_name} is missing "
                    f"a Field(description=...)"
                )
