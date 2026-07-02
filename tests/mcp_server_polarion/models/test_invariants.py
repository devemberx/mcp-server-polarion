"""Cross-model invariants spanning ``mcp_server_polarion.models`` submodules."""

from __future__ import annotations

from mcp_server_polarion.models import (
    DocumentDetail,
    DocumentPart,
    DocumentSummary,
    PaginatedResult,
    ProjectSummary,
    WorkItemCreateSpec,
    WorkItemDetail,
    WorkItemLink,
    WorkItemLinkRef,
    WorkItemLinksCreateResult,
    WorkItemLinksDeleteResult,
    WorkItemLinkSpec,
    WorkItemRead,
    WorkItemsCreateResult,
    WorkItemSummary,
    WorkItemsUpdateResult,
    WorkItemUpdateSpec,
)


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

    def test_update_spec_json_round_trip(self):
        spec = WorkItemUpdateSpec(
            work_item_id="MCPT-001",
            title="After",
            status="approved",
        )
        restored = WorkItemUpdateSpec.model_validate_json(spec.model_dump_json())
        assert restored.title == "After"
        assert restored.status == "approved"

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
            WorkItemCreateSpec,
            WorkItemsCreateResult,
            WorkItemsUpdateResult,
            WorkItemUpdateSpec,
            WorkItemLinkSpec,
            WorkItemLinkRef,
            WorkItemLinksCreateResult,
            WorkItemLinksDeleteResult,
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
