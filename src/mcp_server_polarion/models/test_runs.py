"""Test run models — summaries, create specs, and write results."""

from __future__ import annotations

from collections.abc import Mapping

from pydantic import BaseModel, ConfigDict, Field


class TestRunSummary(BaseModel):
    """Compact test-run representation for list results."""

    # pytest collects Test*-named classes as tests on import; opt out here.
    __test__ = False

    id: str
    title: str
    type: str
    status: str
    finished_on: str = ""
    updated: str = ""
    author_name: str = ""
    is_template: bool = False


class TestRunCreateSpec(BaseModel):
    """One test run to create via ``create_test_runs``."""

    __test__ = False

    # LLM input model: reject typo keys instead of silently dropping them.
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
    """Result of a ``create_test_runs`` operation."""

    __test__ = False

    created: bool
    dry_run: bool
    test_run_ids: list[str] = Field(default_factory=list)
    payload_preview: Mapping[str, object] | None = None
