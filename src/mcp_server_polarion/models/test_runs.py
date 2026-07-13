"""Test run models — summaries, create/update specs, write results."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


class TestRecordSummary(BaseModel):
    """Compact test-record representation for list results."""

    # pytest collect Test*-named classes on import; opt out.
    __test__ = False

    # Full "project/WI-id" from relationships.testCase; record 5-segment id
    # never parsed. test_case_id + iteration = record identity within run.
    test_case_id: str
    iteration: int = 0
    result: str = ""
    executed: str = ""
    duration: float = 0.0
    executed_by_name: str = ""
    defect_id: str = ""


class TestRecordCreateSpec(BaseModel):
    """One test record to create via create_test_records."""

    __test__ = False

    # LLM input model: reject typo keys, not silent-drop.
    model_config = ConfigDict(extra="forbid")

    test_case_id: str = Field(
        min_length=1,
        description=(
            "'WorkItemId' or 'ProjectId/WorkItemId'; bare id qualified with "
            "the tool's project_id."
        ),
    )
    result: str | None = None
    comment: str | None = None
    comment_format: Literal["text/html", "text/plain"] = "text/plain"
    defect_id: str | None = Field(
        default=None,
        description=(
            "'WorkItemId' or 'ProjectId/WorkItemId', qualified like test_case_id."
        ),
    )


class TestRecordsCreateResult(BaseModel):
    """``create_test_records`` result."""

    __test__ = False

    created: bool
    dry_run: bool
    record_ids: list[str] = Field(default_factory=list)
    payload_preview: Mapping[str, object] | None = None


class TestRunSummary(BaseModel):
    """Compact test-run representation for list results."""

    # pytest collect Test*-named classes on import; opt out.
    __test__ = False

    id: str
    title: str
    type: str
    status: str
    finished_on: str = ""
    updated: str = ""
    author_name: str = ""
    is_template: bool = False
    group_id: str = ""
    # short id, round-trips into create_test_runs(template_id=...)
    template_id: str = ""


class TestRunDetail(TestRunSummary):
    """Full test-run detail from ``get_test_run``."""

    __test__ = False

    project_id: str
    author_id: str = ""
    created: str = ""
    # LiveDoc selection source; empty for non-LiveDoc runs.
    space_id: str = ""
    document_name: str = ""
    query: str = ""
    select_test_cases_by: str = ""
    # True = report inherit from template; Polarion omit homePageContent then.
    use_report_from_template: bool = False
    content_html: str = ""
    custom_fields: dict[str, object] = Field(default_factory=dict)


class TestRunCreateSpec(BaseModel):
    """One test run to create via create_test_runs."""

    __test__ = False

    # LLM input model: reject typo keys, not silent-drop.
    model_config = ConfigDict(extra="forbid")

    id: str = Field(min_length=1)
    title: str | None = None
    type: str | None = None
    status: str | None = None
    template_id: str | None = Field(
        default=None,
        description="Same-project template run id; the run inherits its config.",
    )
    custom_fields: dict[str, object] | None = None


class TestRunsCreateResult(BaseModel):
    """``create_test_runs`` result."""

    __test__ = False

    created: bool
    dry_run: bool
    test_run_ids: list[str] = Field(default_factory=list)
    payload_preview: Mapping[str, object] | None = None


class TestRunUpdateSpec(BaseModel):
    """One update_test_runs batch entry; unset fields stay unchanged."""

    __test__ = False

    # LLM input model: reject typo keys, not silent-drop.
    model_config = ConfigDict(extra="forbid")

    test_run_id: str = Field(
        min_length=1, description="Test run ID (e.g. 'TR-2024-01')."
    )
    title: str | None = None
    status: str | None = None
    group_id: str | None = Field(
        default=None, description="Free-form grouping label (e.g. 'Release-2.5')."
    )
    custom_fields: dict[str, object] | None = Field(
        default=None,
        description="Partial; rich-text values as {'type':'text/html','value':...}.",
    )

    @model_validator(mode="after")
    def _require_effective_change(self) -> TestRunUpdateSpec:
        # None custom entries drop at payload build; attribute-less item 400 batch.
        effective = (
            self.title
            or self.status
            or self.group_id
            or (
                self.custom_fields
                and any(value is not None for value in self.custom_fields.values())
            )
        )
        if not effective:
            msg = (
                f"test run '{self.test_run_id}': no effective change -- set "
                "at least one field (custom_fields values of None are dropped)."
            )
            raise ValueError(msg)
        return self


class TestRunsUpdateResult(BaseModel):
    """``update_test_runs`` result."""

    __test__ = False

    updated: bool
    dry_run: bool
    test_run_ids: list[str] = Field(default_factory=list)
    payload_preview: Mapping[str, object] | None = None
