"""Unit tests for the Tier-1 ``ForbiddenBehaviorEvaluator``.

The evaluator is a thin dispatcher: it pulls ``check`` / ``params`` from the
case metadata, fail-closes on an empty or non-list trajectory, and otherwise
delegates the verdict to the named pure check in ``checks.py``. These tests
exercise every branch with synthetic trajectories -- no LLM, no I/O.
"""

from __future__ import annotations

from typing import Any

import pytest

pytest.importorskip("strands_evals")

from strands_evals.types.evaluation import EvaluationData

from evals.evaluators.tier1 import ForbiddenBehaviorEvaluator


def _call(name: str, args: dict[str, Any] | None = None) -> dict[str, Any]:
    return {"name": name, "args": args or {}, "result": None}


def _data(
    trajectory: Any,
    *,
    check: str | None = "readonly",
    params: dict[str, Any] | None = None,
) -> EvaluationData[Any, Any]:
    metadata: dict[str, Any] = {}
    if check is not None:
        metadata["check"] = check
    if params is not None:
        metadata["params"] = params
    return EvaluationData(
        input="task",
        name="case",
        actual_output="",
        actual_trajectory=trajectory,
        metadata=metadata,
    )


class TestForbiddenBehaviorEvaluator:
    def test_empty_trajectory_fails_closed(self) -> None:
        result = ForbiddenBehaviorEvaluator().evaluate(_data([]))[0]
        assert result.test_pass is False
        assert result.score == 0.0
        assert "no tool-call trajectory" in (result.reason or "")

    def test_unknown_check_fails_closed(self) -> None:
        data = _data([_call("get_document")], check="does_not_exist")
        result = ForbiddenBehaviorEvaluator().evaluate(data)[0]
        assert result.test_pass is False
        assert "does_not_exist" in (result.reason or "")

    def test_missing_check_name_fails_closed(self) -> None:
        data = _data([_call("get_document")], check=None)
        result = ForbiddenBehaviorEvaluator().evaluate(data)[0]
        assert result.test_pass is False

    def test_registered_check_passing_scores_one(self) -> None:
        data = _data([_call("get_document")], check="readonly")
        result = ForbiddenBehaviorEvaluator().evaluate(data)[0]
        assert result.test_pass is True
        assert result.score == 1.0
        assert result.label == "readonly"

    def test_registered_check_failing_scores_zero_with_reason(self) -> None:
        data = _data([_call("create_work_items")], check="readonly")
        result = ForbiddenBehaviorEvaluator().evaluate(data)[0]
        assert result.test_pass is False
        assert result.score == 0.0
        assert "create_work_items" in (result.reason or "")

    async def test_evaluate_async_delegates(self) -> None:
        data = _data([_call("get_document")], check="readonly")
        sync = ForbiddenBehaviorEvaluator().evaluate(data)[0]
        asy = (await ForbiddenBehaviorEvaluator().evaluate_async(data))[0]
        assert (asy.test_pass, asy.score, asy.label) == (
            sync.test_pass,
            sync.score,
            sync.label,
        )
