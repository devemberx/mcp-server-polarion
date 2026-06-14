"""Runner tests: text extractor, runaway-agent ``_CycleGuard``, hermetic env
pinning. End-to-end ``run_case`` (real agent + LLM) out of scope.
"""

from __future__ import annotations

import os
from typing import Any

import pytest

# ``runner`` imports ``strands`` / ``strands_evals`` at load; skip on bare install.
pytest.importorskip("strands_evals")

from evals.harness.runner import (
    AGENT_ERROR_PREFIX,
    POLARION_HOST,
    _CycleGuard,
    _extract_text,
    _set_polarion_env,
)


class _Result:
    def __init__(self, message: Any) -> None:
        self.message = message


class TestExtractText:
    def test_joins_text_blocks_from_dict_message(self) -> None:
        result = _Result({"content": [{"text": "hello"}, {"text": "world"}]})
        assert _extract_text(result) == "hello\nworld"

    def test_skips_blocks_without_text(self) -> None:
        result = _Result({"content": [{"text": ""}, {"other": 1}, {"text": "kept"}]})
        assert _extract_text(result) == "kept"

    def test_non_dict_message_falls_back_to_str(self) -> None:
        result = _Result("plain")  # message is a str, not a dict
        assert _extract_text(result) == str(result)


class _Event:
    def __init__(self) -> None:
        self.invocation_state: dict[str, Any] = {}


class TestCycleGuard:
    def test_counts_model_calls(self) -> None:
        guard = _CycleGuard(max_cycles=3)
        for _ in range(3):
            guard._on_before_model_call(_Event())
        assert guard.count == 3

    def test_does_not_stop_within_budget(self) -> None:
        guard = _CycleGuard(max_cycles=3)
        event = _Event()
        guard._on_before_model_call(event)
        assert event.invocation_state == {}

    def test_trips_stop_event_loop_past_budget(self) -> None:
        guard = _CycleGuard(max_cycles=1)
        guard._on_before_model_call(_Event())  # count -> 1, still within budget
        event = _Event()
        guard._on_before_model_call(event)  # count -> 2, over budget
        assert event.invocation_state["request_state"]["stop_event_loop"] is True


class TestSetPolarionEnv:
    def test_hard_sets_both_vars(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("POLARION_URL", "https://real.example.com")
        monkeypatch.delenv("POLARION_TOKEN", raising=False)
        _set_polarion_env()
        assert os.environ["POLARION_URL"] == POLARION_HOST
        assert os.environ["POLARION_TOKEN"] == "fake-token"


def test_agent_error_prefix_is_a_sentinel() -> None:
    assert AGENT_ERROR_PREFIX.startswith("<agent-error")
