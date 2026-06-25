"""Gate-orchestration tests with ``run_case`` stubbed: pass-rate aggregation,
fail-closed crashed runs, git-sha resolution, unknown-case exit code.
"""

from __future__ import annotations

from typing import Any

import pytest

# ``run`` imports ``strands_evals`` at load; skip on the bare dev install.
pytest.importorskip("strands_evals")

from strands_evals import Case

from evals import run
from evals.cases.efficiency import CASES as EFFICIENCY_CASES
from evals.cases.orchestration import CASES as ORCHESTRATION_CASES
from evals.cases.safety import CASES as SAFETY_CASES
from evals.cases.triggers import CASES as TRIGGER_CASES
from evals.harness.runner import AGENT_ERROR_PREFIX


def _case(name: str = "GATE-X", min_rate: float = 1.0) -> Case:
    return Case(
        name=name,
        input="do a thing",
        metadata={"check": "readonly", "params": {}, "min_pass_rate": min_rate},
    )


class TestRunCaseNTimes:
    def test_all_pass_meets_gate(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(run, "_evaluate_once", lambda case, evaluator: (True, ""))
        result = run._run_case_n_times(_case(), runs=3, evaluator=object())
        assert result["pass_count"] == 3
        assert result["pass_rate"] == 1.0
        assert result["passed"] is True
        assert result["failures"] == []

    def test_one_failure_fails_zero_tolerance_gate(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        calls = {"n": 0}

        def _fake(case: Case, evaluator: Any) -> tuple[bool, str]:
            calls["n"] += 1
            return (calls["n"] != 2, "boom" if calls["n"] == 2 else "")

        monkeypatch.setattr(run, "_evaluate_once", _fake)
        result = run._run_case_n_times(_case(min_rate=1.0), runs=3, evaluator=object())
        assert result["pass_count"] == 2
        assert result["passed"] is False
        assert any("run 2" in f and "boom" in f for f in result["failures"])

    def test_partial_pass_meets_lower_threshold(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        seq = iter([True, False, True, True])
        monkeypatch.setattr(
            run, "_evaluate_once", lambda case, evaluator: (next(seq), "")
        )
        result = run._run_case_n_times(_case(min_rate=0.5), runs=4, evaluator=object())
        assert result["pass_rate"] == 0.75
        assert result["passed"] is True


class TestEvaluateOnce:
    def test_agent_error_output_fails_closed(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(
            run,
            "run_case",
            lambda case: {
                "input": case.input,
                "output": f"{AGENT_ERROR_PREFIX} TimeoutError>",
                "trajectory": [],
            },
        )
        passed, reason = run._evaluate_once(_case(), evaluator=object())
        assert passed is False
        assert "agent run failed" in reason


class TestGitSha:
    def test_prefers_github_sha_env(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("GITHUB_SHA", "0123456789abcdef")
        assert run._git_sha() == "0123456789ab"


class TestMain:
    def test_unknown_case_returns_2(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr("sys.argv", ["run", "--case", "DOES-NOT-EXIST"])
        assert run.main() == 2

    def test_category_flag_runs_only_that_category(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Any
    ) -> None:
        seen: list[str] = []

        def _stub(case: Case, runs: int, evaluator: Any) -> dict[str, Any]:
            seen.append(case.name)
            return {
                "name": case.name,
                "runs": runs,
                "pass_count": runs,
                "pass_rate": 1.0,
                "min_pass_rate": 1.0,
                "passed": True,
                "failures": [],
            }

        monkeypatch.setattr(run, "_run_case_n_times", _stub)
        monkeypatch.setattr(run, "resolve_model_id", lambda: "test/model")
        monkeypatch.setattr(run, "_REPORT_DIR", tmp_path)
        monkeypatch.setattr(
            "sys.argv", ["run", "--category", "efficiency", "--runs", "1"]
        )

        assert run.main() == 0
        assert seen == [c.name for c in EFFICIENCY_CASES]

    def test_list_flag_prints_catalog_without_running(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def _boom(case: Case, runs: int, evaluator: Any) -> dict[str, Any]:
            raise AssertionError("--list must not run any case")

        monkeypatch.setattr(run, "_run_case_n_times", _boom)
        monkeypatch.setattr("sys.argv", ["run", "--list", "--category", "safety"])
        assert run.main() == 0


class TestAllCases:
    def test_gate_loads_all_categories(self) -> None:
        expected = [
            *TRIGGER_CASES,
            *SAFETY_CASES,
            *EFFICIENCY_CASES,
            *ORCHESTRATION_CASES,
        ]
        assert expected == run.ALL_CASES

    def test_categories_map_to_expected_case_lists(self) -> None:
        assert run.CATEGORIES["triggers"] == TRIGGER_CASES
        assert run.CATEGORIES["safety"] == SAFETY_CASES
        assert run.CATEGORIES["efficiency"] == EFFICIENCY_CASES
        assert run.CATEGORIES["orchestration"] == ORCHESTRATION_CASES
        assert run.CATEGORIES["all"] == run.ALL_CASES

    def test_all_case_names_unique(self) -> None:
        names = [c.name for c in run.ALL_CASES]
        assert len(names) == len(set(names))
