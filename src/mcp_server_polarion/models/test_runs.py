"""Test run models — compact summaries for list results."""

from __future__ import annotations

from pydantic import BaseModel


class TestRunSummary(BaseModel):
    """Compact test-run representation for list results."""

    id: str
    title: str
    type: str
    status: str
    finished_on: str = ""
    updated: str = ""
    author_name: str = ""
    is_template: bool = False
