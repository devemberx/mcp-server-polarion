"""Tests for document models in ``mcp_server_polarion.models.documents``."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from mcp_server_polarion.models import (
    DocumentDetail,
    DocumentPart,
    DocumentSummary,
)


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

    def test_metadata_defaults_empty(self):
        d = DocumentSummary(space_id="_default", document_name="SRS")
        assert d.status == ""
        assert d.updated == ""
        assert d.author == ""
        assert d.last_updated_by == ""


class TestDocumentDetail:
    def test_valid(self):
        d = DocumentDetail(
            title="Software Requirement Specification",
            content_html="<h2>Overview</h2><p>System requirements.</p>",
        )
        assert d.title == "Software Requirement Specification"
        assert "<h2>Overview</h2>" in d.content_html

    def test_editor_metadata_defaults_empty(self):
        d = DocumentDetail(title="Doc")
        assert d.updated == ""
        assert d.author == ""
        assert d.last_updated_by == ""

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
