"""Bridge-helper tests: ``TrajectoryRecorder`` + result flatteners are pure,
driven with stub objects — no real MCP client, no agent.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import pytest

# ``mcp_bridge`` imports ``strands`` at module load; skip on the bare dev install.
pytest.importorskip("strands")

from evals.harness.mcp_bridge import (
    TrajectoryRecorder,
    _result_payload,
    _result_text,
)


@dataclass
class _Block:
    text: str


@dataclass
class _Result:
    structured_content: Any = None
    content: list[_Block] | None = None


class TestTrajectoryRecorder:
    def test_record_call_appends_and_returns_index(self) -> None:
        recorder = TrajectoryRecorder()
        i0 = recorder.record_call("get_document", {"id": "D"})
        i1 = recorder.record_call("get_work_item", {"id": "W"})
        assert (i0, i1) == (0, 1)
        assert recorder.calls[0] == {
            "name": "get_document",
            "args": {"id": "D"},
            "result": None,
        }

    def test_record_result_targets_the_right_index(self) -> None:
        recorder = TrajectoryRecorder()
        idx = recorder.record_call("get_document", {})
        recorder.record_call("list_documents", {})
        recorder.record_result(idx, {"data": 1})
        assert recorder.calls[0]["result"] == {"data": 1}
        assert recorder.calls[1]["result"] is None


class TestResultPayload:
    def test_structured_content_preferred(self) -> None:
        result = _Result(structured_content={"id": "W-1"})
        assert _result_payload(result) == {"id": "W-1"}

    def test_text_is_json_parsed(self) -> None:
        result = _Result(content=[_Block('{"a": 1}')])
        assert _result_payload(result) == {"a": 1}

    def test_non_json_text_falls_back_to_raw(self) -> None:
        result = _Result(content=[_Block("plain text")])
        assert _result_payload(result) == "plain text"

    def test_empty_content_is_none(self) -> None:
        assert _result_payload(_Result(content=[])) is None
        assert _result_payload(_Result()) is None


class TestResultText:
    def test_structured_content_serialized(self) -> None:
        assert _result_text(_Result(structured_content={"a": 1})) == '{"a": 1}'

    def test_text_blocks_joined(self) -> None:
        result = _Result(content=[_Block("line one"), _Block("line two")])
        assert _result_text(result) == "line one\nline two"
