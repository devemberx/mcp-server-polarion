"""Test run model tests (``mcp_server_polarion.models.test_runs``)."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from mcp_server_polarion.models import TestRecordDetail, TestRunCreateSpec


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
