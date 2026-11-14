"""Gate-orchestration tests with ``run_case`` stubbed: pass-rate aggregation,
fail-closed crashed runs, git-sha resolution, unknown-case exit code, worker
resolution, parallel pool (thread + real spawn cancel), infra re-runs, report
fields (workers, wall_s, peak_rss_mb, cpu_s).
"""

from __future__ import annotations

import json
import os
import pickle
import subprocess
import sys
import time
from concurrent.futures import Executor, ThreadPoolExecutor
from pathlib import Path
from typing import Any

import pytest

# ``run`` import ``strands_evals`` at load; skip on bare dev install.
pytest.importorskip("strands_evals")

from strands_evals import Case

from evals import run
from evals.cases.efficiency import CASES as EFFICIENCY_CASES
from evals.cases.orchestration import CASES as ORCHESTRATION_CASES
from evals.cases.safety import CASES as SAFETY_CASES
from evals.cases.triggers import CASES as TRIGGER_CASES
from evals.harness.runner import AGENT_ERROR_PREFIX, INFRA_ERROR_PREFIX
from tests.evals import _pool_jobs


@pytest.fixture(autouse=True)
def _serial_workers(monkeypatch: pytest.MonkeyPatch) -> None:
    # Serial path: main tests patch _run_case_n_times; spawned child miss patches.
    monkeypatch.setenv("EVAL_WORKERS", "1")
    monkeypatch.setattr(run, "_EVALUATOR", None)


def _case(name: str = "GATE-X", min_rate: float = 1.0) -> Case:
    return Case(
        name=name,
        input="do a thing",
        metadata={"check": "readonly", "params": {}, "min_pass_rate": min_rate},
    )


class TestPlainLogging:
    def test_import_forces_plain_fastmcp_logging(self) -> None:
        # Rich hang rationale live at fix site (evals/run.py env set).
        code = (
            "import evals.run, logging;"
            "handlers = logging.getLogger('fastmcp').handlers;"
            "print([type(h).__name__ for h in handlers])"
        )
        # Own interpreter + literal code = trusted input.
        result = subprocess.run(  # noqa: S603
            [sys.executable, "-c", code],
            capture_output=True,
            text=True,
            check=True,
            # cwd pin: `import evals.run` need repo root regardless of pytest cwd.
            cwd=Path(__file__).resolve().parents[2],
            # Hostile "true" prove hard set win over inherited env.
            env={**os.environ, "FASTMCP_ENABLE_RICH_LOGGING": "true"},
        )
        assert "RichHandler" not in result.stdout


class TestRunCaseNTimes:
    def test_all_pass_meets_gate(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(
            run, "_evaluate_once", lambda case, evaluator: run.Verdict(True, "")
        )
        result = run._run_case_n_times(_case(), runs=3, evaluator=object())
        assert result["pass_count"] == 3
        assert result["pass_rate"] == 1.0
        assert result["passed"] is True
        assert result["failures"] == []

    def test_one_failure_fails_zero_tolerance_gate(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        calls = {"n": 0}

        def _fake(case: Case, evaluator: Any) -> run.Verdict:
            calls["n"] += 1
            return run.Verdict(calls["n"] != 2, "boom" if calls["n"] == 2 else "")

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
            run, "_evaluate_once", lambda case, evaluator: run.Verdict(next(seq), "")
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
        verdict = run._evaluate_once(_case(), evaluator=object())
        assert verdict.passed is False
        assert "agent run failed" in verdict.reason


class TestEvaluateOnceEvaluator:
    def test_evaluator_result_becomes_verdict(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(
            run,
            "run_case",
            lambda case: {"input": case.input, "output": "fine", "trajectory": []},
        )

        class _Result:
            test_pass = True
            reason = "ok"

        class _Eval:
            def evaluate(self, data: Any) -> list[_Result]:
                return [_Result()]

        verdict = run._evaluate_once(_case(), evaluator=_Eval())  # type: ignore[arg-type]
        assert verdict == run.Verdict(True, "ok", infra=False)


class TestEvaluateOnceInfra:
    def test_infra_output_flags_verdict(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(
            run,
            "run_case",
            lambda case: {
                "input": case.input,
                "output": f"{INFRA_ERROR_PREFIX} OSError: net down>",
                "trajectory": [],
            },
        )
        verdict = run._evaluate_once(_case(), evaluator=object())
        assert verdict.passed is False
        assert verdict.infra is True
        assert verdict.reason.startswith("infra error: ")

    def test_agent_error_not_infra(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(
            run,
            "run_case",
            lambda case: {
                "input": case.input,
                "output": f"{AGENT_ERROR_PREFIX} TimeoutError>",
                "trajectory": [],
            },
        )
        assert run._evaluate_once(_case(), evaluator=object()).infra is False


class TestInfraRetrySettings:
    def test_defaults(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("EVAL_INFRA_RETRIES", raising=False)
        monkeypatch.delenv("EVAL_INFRA_RETRY_DELAY", raising=False)
        assert run._infra_retry_settings() == (2, 30.0)

    def test_custom(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("EVAL_INFRA_RETRIES", "5")
        monkeypatch.setenv("EVAL_INFRA_RETRY_DELAY", "1.5")
        assert run._infra_retry_settings() == (5, 1.5)

    def test_negative_floored(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("EVAL_INFRA_RETRIES", "-3")
        monkeypatch.setenv("EVAL_INFRA_RETRY_DELAY", "-1")
        assert run._infra_retry_settings() == (0, 0.0)


class TestRunJob:
    def test_infra_once_then_pass(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("EVAL_INFRA_RETRIES", "2")
        monkeypatch.setenv("EVAL_INFRA_RETRY_DELAY", "7")
        seq = iter(
            [run.Verdict(False, "infra error: x", infra=True), run.Verdict(True, "")]
        )
        monkeypatch.setattr(run, "_evaluate_once", lambda case, evaluator: next(seq))
        sleeps: list[float] = []
        monkeypatch.setattr(run.time, "sleep", sleeps.append)
        outcome = run._run_job(_case(), object())
        assert outcome.passed is True
        assert outcome.infra_retries == 1
        assert sleeps == [7.0]
        assert outcome.duration_s >= 0.0

    def test_infra_every_time_fails(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("EVAL_INFRA_RETRIES", "2")
        monkeypatch.setenv("EVAL_INFRA_RETRY_DELAY", "0")
        calls = {"n": 0}

        def _fake(case: Case, evaluator: Any) -> run.Verdict:
            calls["n"] += 1
            return run.Verdict(False, "infra error: down", infra=True)

        monkeypatch.setattr(run, "_evaluate_once", _fake)
        monkeypatch.setattr(run.time, "sleep", lambda s: None)
        outcome = run._run_job(_case(), object())
        assert calls["n"] == 3
        assert outcome.passed is False
        assert outcome.infra_retries == 2
        assert "infra error after 2 retries: down" in outcome.reason
        assert "infra error: " not in outcome.reason
        assert "down" in outcome.reason

    def test_zero_retries_no_rerun(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("EVAL_INFRA_RETRIES", "0")
        calls = {"n": 0}

        def _fake(case: Case, evaluator: Any) -> run.Verdict:
            calls["n"] += 1
            return run.Verdict(False, "infra error: down", infra=True)

        monkeypatch.setattr(run, "_evaluate_once", _fake)
        monkeypatch.setattr(run.time, "sleep", lambda s: pytest.fail("no sleep"))
        outcome = run._run_job(_case(), object())
        assert calls["n"] == 1
        assert outcome.passed is False
        assert outcome.infra_retries == 0

    def test_real_failure_not_retried(self, monkeypatch: pytest.MonkeyPatch) -> None:
        calls = {"n": 0}

        def _fake(case: Case, evaluator: Any) -> run.Verdict:
            calls["n"] += 1
            return run.Verdict(False, "bad")

        monkeypatch.setattr(run, "_evaluate_once", _fake)
        outcome = run._run_job(_case(), object())
        assert calls["n"] == 1
        assert outcome.reason == "bad"


class TestAggregate:
    def test_fields_and_order(self) -> None:
        outcomes = [
            run.RunOutcome(True, "", 1.0, 0),
            run.RunOutcome(False, "boom", 2.0, 1),
            run.RunOutcome(False, "bang", 3.0, 2),
        ]
        result = run._aggregate(_case(min_rate=0.2), outcomes)
        assert result["pass_count"] == 1
        assert result["passed"] is True
        assert result["failures"] == ["run 2: boom", "run 3: bang"]
        assert result["durations_s"] == [1.0, 2.0, 3.0]
        assert result["infra_retries"] == 3

    def test_no_runs(self) -> None:
        result = run._aggregate(_case(), [])
        assert result["pass_rate"] == 0.0
        assert result["passed"] is False


class TestRunOutcome:
    def test_pickle_round_trip(self) -> None:
        outcome = run.RunOutcome(False, "r", 1.5, 2)
        assert pickle.loads(pickle.dumps(outcome)) == outcome  # noqa: S301


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

    def test_report_records_model_and_effort(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Any
    ) -> None:
        monkeypatch.setattr(
            run,
            "_run_case_n_times",
            lambda case, runs, evaluator: {
                "name": case.name,
                "runs": runs,
                "pass_count": runs,
                "pass_rate": 1.0,
                "min_pass_rate": 1.0,
                "passed": True,
                "failures": [],
            },
        )
        monkeypatch.setattr(run, "resolve_model_id", lambda: "openai/gpt-6-luna")
        monkeypatch.setattr(run, "resolve_reasoning_effort", lambda: "high")
        monkeypatch.setattr(run, "_git_sha", lambda: "abc123")
        monkeypatch.setattr(run, "_REPORT_DIR", tmp_path)
        monkeypatch.setattr(
            "sys.argv", ["run", "--case", "SAFE-READONLY", "--runs", "1"]
        )

        assert run.main() == 0
        # Effort in filename: same sha + model at other effort must not clobber.
        report = json.loads(
            (tmp_path / "gate-abc123-openai-gpt-6-luna-high.json").read_text(
                encoding="utf-8"
            )
        )
        assert report["model"] == "openai/gpt-6-luna"
        assert report["reasoning_effort"] == "high"

    def test_list_flag_prints_catalog_without_running(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def _boom(case: Case, runs: int, evaluator: Any) -> dict[str, Any]:
            raise AssertionError("--list must not run any case")

        monkeypatch.setattr(run, "_run_case_n_times", _boom)
        monkeypatch.setattr("sys.argv", ["run", "--list", "--category", "safety"])
        assert run.main() == 0

    def test_list_ignores_bad_workers_env(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("EVAL_WORKERS", "abc")
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


def _ok_report_patches(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setattr(run, "resolve_model_id", lambda: "test/model")
    monkeypatch.setattr(run, "resolve_reasoning_effort", lambda: "high")
    monkeypatch.setattr(run, "_git_sha", lambda: "abc123")
    monkeypatch.setattr(run, "_REPORT_DIR", tmp_path)


def _read_report(tmp_path: Path) -> dict[str, Any]:
    path = tmp_path / "gate-abc123-test-model-high.json"
    return json.loads(path.read_text(encoding="utf-8"))


class TestResolveWorkers:
    def test_cli_wins_over_env(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("EVAL_WORKERS", "5")
        assert run._resolve_workers(3) == 3

    def test_env_used(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("EVAL_WORKERS", " 5 ")
        assert run._resolve_workers(None) == 5

    def test_blank_env_defaults(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("EVAL_WORKERS", "  ")
        assert run._resolve_workers(None) == run.DEFAULT_WORKERS == 8

    def test_unset_env_defaults(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("EVAL_WORKERS", raising=False)
        assert run._resolve_workers(None) == 8

    @pytest.mark.parametrize(
        ("argv", "env"),
        [
            (["--workers", "0"], "1"),
            ([], "0"),
            ([], "abc"),
        ],
    )
    def test_main_rejects_invalid(
        self, monkeypatch: pytest.MonkeyPatch, argv: list[str], env: str
    ) -> None:
        monkeypatch.setenv("EVAL_WORKERS", env)
        monkeypatch.setattr("sys.argv", ["run", "--runs", "1", *argv])
        with pytest.raises(SystemExit) as exc:
            run.main()
        assert exc.value.code == 2


class TestRunParallel:
    def test_matches_serial_aggregation_in_catalog_order(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        cases = run.ALL_CASES[:3]
        bad = str(cases[1].name)
        monkeypatch.setattr(
            run,
            "_evaluate_once",
            lambda case, evaluator: run.Verdict(case.name != bad, f"r-{case.name}"),
        )
        parallel = run._run_parallel(cases, 2, 4, executor_factory=ThreadPoolExecutor)
        serial = [
            run._run_case_n_times(c, 2, run.CheckDispatchEvaluator()) for c in cases
        ]
        assert [r["name"] for r in parallel] == [c.name for c in cases]
        for p, s in zip(parallel, serial, strict=True):
            assert p["pass_count"] == s["pass_count"]
            assert p["failures"] == s["failures"]
            assert p["runs"] == 2
        assert parallel[1]["passed"] is False

    def test_failures_index_ordered_when_later_run_finishes_first(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def _fake(case_name: str, run_idx: int) -> tuple[str, int, run.RunOutcome]:
            if run_idx == 0:
                time.sleep(0.2)
            return case_name, run_idx, run.RunOutcome(False, f"bad{run_idx}", 0.1, 0)

        monkeypatch.setattr(run, "_pool_job", _fake)
        out = run._run_parallel(
            run.ALL_CASES[:1], 3, 3, executor_factory=ThreadPoolExecutor
        )
        assert out[0]["failures"] == [
            "run 1: bad0",
            "run 2: bad1",
            "run 3: bad2",
        ]

    def test_job_exception_stops_queue_promptly(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        ran: list[tuple[str, int]] = []
        cases = run.ALL_CASES[:4]
        first = str(cases[0].name)

        def _fake(case_name: str, run_idx: int) -> tuple[str, int, run.RunOutcome]:
            ran.append((case_name, run_idx))
            if (case_name, run_idx) == (first, 0):
                raise RuntimeError("early boom")
            time.sleep(0.3)
            return case_name, run_idx, run.RunOutcome(True, "", 0.3, 0)

        monkeypatch.setattr(run, "_pool_job", _fake)
        start = time.monotonic()
        with pytest.raises(RuntimeError, match="early boom"):
            run._run_parallel(cases, 3, 2, executor_factory=ThreadPoolExecutor)
        elapsed = time.monotonic() - start
        # 12 jobs, 2 workers, 0.3 s each: full drain ~1.8 s.
        assert elapsed < 0.9
        assert len(ran) < 12

    def test_job_exception_cancels_queue_on_real_spawn_pool(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        log = tmp_path / "started.log"
        cases = run.ALL_CASES[:4]
        monkeypatch.setenv("EVAL_POOL_LOG", str(log))
        monkeypatch.setenv("EVAL_POOL_FAIL", str(cases[0].name))
        monkeypatch.setattr(run, "_pool_job", _pool_jobs.logging_job)
        pools: list[Executor] = []

        def _factory(n: int) -> Executor:
            pools.append(run._spawn_pool(n))
            return pools[-1]

        with pytest.raises(RuntimeError, match="early boom"):
            run._run_parallel(cases, 3, 2, executor_factory=_factory)
        # Idempotent shutdown no re-join manager: sleep > 2x job time so any
        # uncancelled queued job has started before counting.
        pools[0].shutdown(wait=True)
        time.sleep(1.0)
        started = len(log.read_text(encoding="utf-8").splitlines())
        print(f"jobs started: {started}/12")
        # 12 jobs total; only in-flight + call-queue may start (6 observed with
        # fix, 9 without).
        assert started <= 7

    def test_unfilled_slot_raises(self, monkeypatch: pytest.MonkeyPatch) -> None:
        def _fake(case_name: str, run_idx: int) -> tuple[str, int, run.RunOutcome]:
            return case_name, 0, run.RunOutcome(True, "", 0.1, 0)

        monkeypatch.setattr(run, "_pool_job", _fake)
        case = run.ALL_CASES[0]
        with pytest.raises(RuntimeError, match=rf"{case.name}.*run 2"):
            run._run_parallel([case], 2, 2, executor_factory=ThreadPoolExecutor)

    def test_job_exception_propagates(self, monkeypatch: pytest.MonkeyPatch) -> None:
        def _boom(case: Case, evaluator: Any) -> run.Verdict:
            raise RuntimeError("pool boom")

        monkeypatch.setattr(run, "_evaluate_once", _boom)
        with pytest.raises(RuntimeError, match="pool boom"):
            run._run_parallel(
                run.ALL_CASES[:1], 1, 2, executor_factory=ThreadPoolExecutor
            )


class TestPoolJob:
    def test_resolves_case_and_builds_evaluator_once(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        built: list[int] = []

        class _Eval:
            def __init__(self) -> None:
                built.append(1)

        monkeypatch.setattr(run, "_EVALUATOR", None)
        monkeypatch.setattr(run, "CheckDispatchEvaluator", _Eval)
        seen: list[str] = []

        def _fake(case: Case, evaluator: Any) -> run.Verdict:
            seen.append(str(case.name))
            return run.Verdict(True, "")

        monkeypatch.setattr(run, "_evaluate_once", _fake)
        name = str(run.ALL_CASES[0].name)
        n1, i1, o1 = run._pool_job(name, 0)
        n2, i2, _ = run._pool_job(name, 1)
        assert (n1, i1, n2, i2) == (name, 0, name, 1)
        assert isinstance(o1, run.RunOutcome)
        assert o1.passed is True
        assert seen == [name, name]
        assert built == [1]


class TestSpawnPool:
    def test_uses_spawn_and_runs_job(self) -> None:
        pool = run._spawn_pool(2)
        try:
            assert pool._mp_context.get_start_method() == "spawn"  # type: ignore[attr-defined]
            assert pool.submit(abs, -1).result(timeout=60) == 1
        finally:
            pool.shutdown()


class TestMainWorkers:
    def test_parallel_report_fields(
        self,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        _ok_report_patches(monkeypatch, tmp_path)
        monkeypatch.setattr(run, "_spawn_pool", ThreadPoolExecutor)
        monkeypatch.setattr(
            run, "_evaluate_once", lambda case, evaluator: run.Verdict(True, "")
        )
        monkeypatch.setattr(
            "sys.argv", ["run", "--category", "safety", "--runs", "2", "--workers", "3"]
        )
        assert run.main() == 0
        out = capsys.readouterr().out
        assert "workers=3" in out
        assert "wall: " in out
        report = _read_report(tmp_path)
        assert report["workers"] == 3
        for key in ("wall_s", "peak_rss_mb", "cpu_s"):
            assert isinstance(report[key], (int, float))
        assert len(report["cases"]) == len(SAFETY_CASES)

    def test_workers_clamped_to_job_count_uses_serial(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        _ok_report_patches(monkeypatch, tmp_path)

        def _no_pool(*args: Any, **kwargs: Any) -> Any:
            raise AssertionError("single job must stay serial")

        monkeypatch.setattr(run, "_run_parallel", _no_pool)
        monkeypatch.setattr(
            run, "_evaluate_once", lambda case, evaluator: run.Verdict(True, "")
        )
        monkeypatch.setattr(
            "sys.argv",
            ["run", "--case", "SAFE-READONLY", "--runs", "1", "--workers", "8"],
        )
        assert run.main() == 0
        assert _read_report(tmp_path)["workers"] == 1
