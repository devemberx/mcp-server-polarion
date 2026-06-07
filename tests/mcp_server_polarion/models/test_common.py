"""Tests for shared models in ``mcp_server_polarion.models.common``."""

from __future__ import annotations

from mcp_server_polarion.models import (
    PaginatedResult,
    ProjectSummary,
    WorkItemSummary,
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
