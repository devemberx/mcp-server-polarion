"""Test run model tests (``mcp_server_polarion.models.test_runs``)."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from mcp_server_polarion.models import (
    TestRecordCreateSpec,
    TestRecordDetail,
    TestRecordsCreateResult,
    TestRecordUpdateSpec,
    TestRunCreateSpec,
)


class TestTestRunCreateSpec:
    def test_minimal_spec(self):
        spec = TestRunCreateSpec(id="RUN-1")
        assert spec.id == "RUN-1"
        assert spec.title is None
        assert spec.custom_fields is None

    def test_typo_key_rejected(self):
        # extra='forbid': typo key must error, not silently drop field.
        with pytest.raises(ValidationError, match="titel"):
            TestRunCreateSpec.model_validate({"id": "RUN-1", "titel": "oops"})


class TestTestRecordDetail:
    def test_required_and_inherited_summary_fields(self):
        detail = TestRecordDetail(
            project_id="proj",
            test_run_id="TR-1",
            test_case_id="proj/WI-1",
            iteration=2,
            result="passed",
            executed="2026-01-01T00:00:00Z",
            duration=1.5,
            executed_by_name="Jane",
            defect_id="proj/WI-99",
        )
        assert detail.project_id == "proj"
        assert detail.test_run_id == "TR-1"
        assert detail.test_case_id == "proj/WI-1"
        assert detail.iteration == 2
        assert detail.result == "passed"
        assert detail.executed == "2026-01-01T00:00:00Z"
        assert detail.duration == 1.5
        assert detail.executed_by_name == "Jane"
        assert detail.defect_id == "proj/WI-99"

    def test_detail_only_fields_default_empty(self):
        detail = TestRecordDetail(
            project_id="proj", test_run_id="TR-1", test_case_id="proj/WI-1"
        )
        assert detail.executed_by_id == ""
        assert detail.test_case_revision == ""
        assert detail.comment_html == ""

    def test_missing_required_fields_raises(self):
        with pytest.raises(ValidationError):
            TestRecordDetail(test_case_id="proj/WI-1")


class TestTestRecordUpdateSpec:
    def test_extra_key_rejected(self):
        # extra='forbid': typo key must error, not silently drop field.
        with pytest.raises(ValidationError, match="bogus"):
            TestRecordUpdateSpec.model_validate(
                {"record_id": "P/TR-1/P/WI-1/0", "result": "passed", "bogus": "x"}
            )

    def test_no_effective_change_rejected(self):
        with pytest.raises(ValidationError, match="no effective change"):
            TestRecordUpdateSpec(record_id="P/TR-1/P/WI-1/0")

    def test_comment_format_alone_not_effective(self):
        # comment_format alone (no comment/result/defect) is not a change.
        with pytest.raises(ValidationError, match="no effective change"):
            TestRecordUpdateSpec(
                record_id="P/TR-1/P/WI-1/0", comment_format="text/html"
            )

    def test_happy_construction(self):
        spec = TestRecordUpdateSpec(
            record_id="P/TR-1/P/WI-1/0",
            result="passed",
            comment="looks good",
            comment_format="text/html",
            defect_work_item_id="P/WI-2",
        )
        assert spec.record_id == "P/TR-1/P/WI-1/0"
        assert spec.result == "passed"
        assert spec.comment == "looks good"
        assert spec.comment_format == "text/html"
        assert spec.defect_work_item_id == "P/WI-2"

    def test_default_comment_format_is_text_plain(self):
        spec = TestRecordUpdateSpec(record_id="P/TR-1/P/WI-1/0", result="passed")
        assert spec.comment_format == "text/plain"


class TestTestRecordCreateSpec:
    def test_minimal_spec(self):
        spec = TestRecordCreateSpec(test_case_id="WI-1")
        assert spec.test_case_id == "WI-1"
        assert spec.result is None
        assert spec.comment is None
        assert spec.comment_format == "text/plain"
        assert spec.defect_id is None

    def test_full_spec(self):
        spec = TestRecordCreateSpec(
            test_case_id="Proj/WI-1",
            result="passed",
            comment="looks fine",
            comment_format="text/html",
            defect_id="Proj/BUG-1",
        )
        assert spec.result == "passed"
        assert spec.comment == "looks fine"
        assert spec.comment_format == "text/html"
        assert spec.defect_id == "Proj/BUG-1"

    def test_typo_key_rejected(self):
        # extra='forbid': typo key must error, not silently drop.
        with pytest.raises(ValidationError, match="tets_case_id"):
            TestRecordCreateSpec.model_validate({"tets_case_id": "WI-1"})

    def test_empty_test_case_id_rejected(self):
        with pytest.raises(ValidationError):
            TestRecordCreateSpec(test_case_id="")


class TestTestRecordsCreateResult:
    def test_defaults(self):
        result = TestRecordsCreateResult(created=True, dry_run=False)
        assert result.record_ids == []
        assert result.payload_preview is None
