"""Test run models — summaries, create specs, write results."""

from __future__ import annotations

from collections.abc import Mapping

from pydantic import BaseModel, ConfigDict, Field


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
    """One test run to create via ``create_test_runs``."""

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
