"""Eval deploy gate (wired into ``publish.yml``): run every case N times,
exit non-zero below ``min_pass_rate`` (1.0 triggers/safety, 0.8
efficiency/orchestration).

    uv run python -m evals.run                      # all cases, EVAL_RUNS (default 10)
    uv run python -m evals.run --category triggers   # one category (gate stages them)
    uv run python -m evals.run --list                # print the case catalog (no model)
    uv run python -m evals.run --case SAFE-READONLY --runs 1
    uv run python -m evals.run --workers 4           # procs (default 8, EVAL_WORKERS)
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

# Put repo root on path so `python evals/run.py` work alongside `-m`.
if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

# Before fastmcp import (settings read env once): rich traceback render spin
# forever on cyclic __cause__ chains (respx SideEffectError wrap/unwrap) and
# freeze event loop mid-eval -- plain logging immune. Hard set, not setdefault:
# inherited "true" would revive hang.
os.environ["FASTMCP_ENABLE_RICH_LOGGING"] = "false"

import argparse
import json
import multiprocessing
import re
import subprocess
import time
from collections.abc import Callable
from concurrent.futures import Executor, ProcessPoolExecutor, as_completed
from dataclasses import dataclass
from typing import Any, NamedTuple, TypedDict

from strands_evals import Case
from strands_evals.types.evaluation import EvaluationData

from evals.cases.efficiency import CASES as EFFICIENCY_CASES
from evals.cases.orchestration import CASES as ORCHESTRATION_CASES
from evals.cases.safety import CASES as SAFETY_CASES
from evals.cases.triggers import CASES as TRIGGER_CASES
from evals.evaluators.dispatch import CheckDispatchEvaluator
from evals.harness.model import resolve_model_id, resolve_reasoning_effort
from evals.harness.runner import AGENT_ERROR_PREFIX, INFRA_ERROR_PREFIX, run_case

_REPORT_DIR = Path(__file__).parent / "reports"

DEFAULT_WORKERS = 8

ALL_CASES = [*TRIGGER_CASES, *SAFETY_CASES, *EFFICIENCY_CASES, *ORCHESTRATION_CASES]

# One CI job per category: cheap early failure skip pricier later ones.
CATEGORIES: dict[str, list[Case]] = {
    "triggers": TRIGGER_CASES,
    "safety": SAFETY_CASES,
    "efficiency": EFFICIENCY_CASES,
    "orchestration": ORCHESTRATION_CASES,
    "all": ALL_CASES,
}

# Reverse lookup: case name -> behaviour category (for catalog + report).
_CASE_CATEGORY: dict[str, str] = {
    str(c.name): cat for cat, cases in CATEGORIES.items() if cat != "all" for c in cases
}


def _git_sha() -> str:
    sha = os.environ.get("GITHUB_SHA")
    if sha:
        return sha[:12]
    try:
        out = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"],
            capture_output=True,
            text=True,
            check=True,
        )
        return out.stdout.strip()
    except (subprocess.CalledProcessError, FileNotFoundError):
        return "unknown"


def _print_catalog(cases: list[Case]) -> None:
    """One row per case: name, category, check, min%, covers, then intent."""
    header = f"{'CASE':<24} {'CATEGORY':<14} {'CHECK':<22} {'MIN%':>5}  COVERS"
    print(header)
    print("-" * len(header))
    for c in cases:
        meta = c.metadata or {}
        name = str(c.name)
        category = _CASE_CATEGORY.get(name, "?")
        check = str(meta.get("check", ""))
        min_pct = f"{float(meta.get('min_pass_rate', 1.0)):.0%}"
        covers = ", ".join(meta.get("covers", []))
        print(f"{name:<24} {category:<14} {check:<22} {min_pct:>5}  {covers}")
        print(f"{'':<24} {'':<14} intent: {meta.get('intent', '')}")


class Verdict(NamedTuple):
    passed: bool
    reason: str
    # Infra failure (network/provider), not agent behaviour: eligible for re-run.
    infra: bool = False


@dataclass(frozen=True)
class RunOutcome:
    """Picklable result of one gate run (after infra re-runs)."""

    passed: bool
    reason: str
    duration_s: float
    infra_retries: int


class CaseResult(TypedDict):
    """Per-case gate verdict = one report ``cases`` entry."""

    name: str
    runs: int
    pass_count: int
    pass_rate: float
    min_pass_rate: float
    passed: bool
    failures: list[str]
    durations_s: list[float]
    infra_retries: int
    category: str
    intent: str
    covers: list[str]


def _evaluate_once(case: Case, evaluator: CheckDispatchEvaluator) -> Verdict:
    task_output = run_case(case)
    output = task_output.get("output")
    if isinstance(output, str) and output.startswith(INFRA_ERROR_PREFIX):
        calls = task_output.get("trajectory") or []
        if not calls:
            return Verdict(False, f"infra error: {output}", infra=True)
        # Tool call already happened: clean re-run would erase a forbidden write.
        return Verdict(
            False,
            f"agent run failed: {output} (after {len(calls)} tool calls; not re-run)",
        )
    if isinstance(output, str) and output.startswith(AGENT_ERROR_PREFIX):
        # Crashed agent: partial trajectory must not read clean.
        return Verdict(False, f"agent run failed: {output}")
    data: EvaluationData[Any, Any] = EvaluationData(
        input=case.input,
        name=case.name,
        actual_output=task_output.get("output"),
        actual_trajectory=task_output.get("trajectory"),
        metadata=case.metadata,
    )
    result = evaluator.evaluate(data)[0]
    return Verdict(result.test_pass, result.reason or "")


def _infra_retry_settings() -> tuple[int, float]:
    # Call-time read: tests + CI env override without reimport.
    retries = max(0, int(os.environ.get("EVAL_INFRA_RETRIES", "2")))
    delay = max(0.0, float(os.environ.get("EVAL_INFRA_RETRY_DELAY", "30")))
    return retries, delay


def _run_job(case: Case, evaluator: CheckDispatchEvaluator) -> RunOutcome:
    max_retries, delay = _infra_retry_settings()
    start = time.monotonic()
    verdict = _evaluate_once(case, evaluator)
    retries = 0
    while verdict.infra and retries < max_retries:
        time.sleep(delay)
        retries += 1
        verdict = _evaluate_once(case, evaluator)
    reason = verdict.reason
    if verdict.infra:
        reason = (
            f"infra error after {retries} retries: "
            f"{reason.removeprefix('infra error: ')}"
        )
    return RunOutcome(verdict.passed, reason, time.monotonic() - start, retries)


def _aggregate(case: Case, outcomes: list[RunOutcome]) -> CaseResult:
    meta = case.metadata or {}
    min_rate = float(meta.get("min_pass_rate", 1.0))
    runs = len(outcomes)
    passes = sum(1 for o in outcomes if o.passed)
    pass_rate = passes / runs if runs else 0.0
    return {
        "name": str(case.name),
        "runs": runs,
        "pass_count": passes,
        "pass_rate": pass_rate,
        "min_pass_rate": min_rate,
        "passed": pass_rate >= min_rate,
        "failures": [
            f"run {i + 1}: {o.reason}" for i, o in enumerate(outcomes) if not o.passed
        ],
        "durations_s": [o.duration_s for o in outcomes],
        "infra_retries": sum(o.infra_retries for o in outcomes),
        "category": _CASE_CATEGORY.get(str(case.name), "?"),
        "intent": str(meta.get("intent", "")),
        "covers": list(meta.get("covers", [])),
    }


def _print_run(name: str, idx: int, runs: int, outcome: RunOutcome) -> None:
    print(f"  {name} run {idx + 1}/{runs}: {'PASS' if outcome.passed else 'FAIL'}")


def _run_case_n_times(
    case: Case, runs: int, evaluator: CheckDispatchEvaluator
) -> CaseResult:
    outcomes: list[RunOutcome] = []
    for i in range(runs):
        outcome = _run_job(case, evaluator)
        outcomes.append(outcome)
        _print_run(str(case.name), i, runs, outcome)
    return _aggregate(case, outcomes)


def _resolve_workers(cli: int | None) -> int:
    if cli is not None:
        return cli
    raw = os.environ.get("EVAL_WORKERS", "").strip()
    return int(raw) if raw else DEFAULT_WORKERS


def _resource_usage() -> tuple[float | None, float | None]:
    """(peak RSS MiB of largest single process, CPU s of all processes)."""
    if sys.platform == "win32":
        # No `resource` module; module-level import would break even --list.
        return None, None
    import resource  # noqa: PLC0415

    self_usage = resource.getrusage(resource.RUSAGE_SELF)
    child_usage = resource.getrusage(resource.RUSAGE_CHILDREN)
    # ru_maxrss = bytes on macOS, KiB on Linux.
    per_mib = 1024 * 1024 if sys.platform == "darwin" else 1024
    peak_rss_mb = max(self_usage.ru_maxrss, child_usage.ru_maxrss) / per_mib
    cpu_s = (
        self_usage.ru_utime
        + self_usage.ru_stime
        + child_usage.ru_utime
        + child_usage.ru_stime
    )
    return peak_rss_mb, cpu_s


def _spawn_pool(n: int) -> Executor:
    # spawn avoids fork copying parent globals/threads mid-state (respx router,
    # os.environ + module patches). Workers still run many jobs sequentially in
    # one process.
    return ProcessPoolExecutor(
        max_workers=n, mp_context=multiprocessing.get_context("spawn")
    )


# Per-process cache: one evaluator per worker, built on first job.
_EVALUATOR: CheckDispatchEvaluator | None = None


def _pool_job(case_name: str, run_idx: int) -> tuple[str, int, RunOutcome]:
    global _EVALUATOR  # noqa: PLW0603
    if _EVALUATOR is None:
        _EVALUATOR = CheckDispatchEvaluator()
    case = next(c for c in ALL_CASES if c.name == case_name)
    return case_name, run_idx, _run_job(case, _EVALUATOR)


def _run_parallel(
    cases: list[Case],
    runs: int,
    workers: int,
    executor_factory: Callable[[int], Executor] = _spawn_pool,
) -> list[CaseResult]:
    slots: dict[str, list[RunOutcome | None]] = {
        str(c.name): [None] * runs for c in cases
    }
    pool = executor_factory(workers)
    try:
        # Round-major: run 0 of every case first, spread load across cases.
        futures = [
            pool.submit(_pool_job, str(c.name), i) for i in range(runs) for c in cases
        ]
        for future in as_completed(futures):
            name, idx, outcome = future.result()
            slots[name][idx] = outcome
            _print_run(name, idx, runs, outcome)
    except BaseException:
        # Job error = gate abort; drop queued jobs (running ones can't cancel).
        # Single shutdown call: 2nd call (e.g. `with` exit) reset cancel flag
        # before process pool manager thread apply it.
        pool.shutdown(wait=True, cancel_futures=True)
        raise
    pool.shutdown(wait=True)
    aggregated: list[CaseResult] = []
    for c in cases:
        outcomes: list[RunOutcome] = []
        for i, o in enumerate(slots[str(c.name)]):
            if o is None:
                # Fail closed: missing slot must not shrink runs.
                raise RuntimeError(f"no outcome for case {c.name} run {i + 1}")
            outcomes.append(o)
        aggregated.append(_aggregate(c, outcomes))
    return aggregated


def main() -> int:
    parser = argparse.ArgumentParser(description="Eval deploy gate")
    parser.add_argument(
        "--category",
        choices=("triggers", "safety", "efficiency", "orchestration", "all"),
        default="all",
        help="behaviour category to run (default all)",
    )
    parser.add_argument("--case", help="run only this case name (e.g. SAFE-READONLY)")
    parser.add_argument(
        "--runs",
        type=int,
        default=int(os.environ.get("EVAL_RUNS", "10")),
        help="runs per case (default EVAL_RUNS or 10)",
    )
    parser.add_argument(
        "--workers",
        type=int,
        default=None,
        help=f"parallel worker processes (default EVAL_WORKERS or {DEFAULT_WORKERS})",
    )
    parser.add_argument(
        "--list",
        action="store_true",
        help="print the case catalog (name/category/check/covers/intent) and exit",
    )
    args = parser.parse_args()
    cases = CATEGORIES[args.category]
    if args.case:
        cases = [c for c in cases if c.name == args.case]
        if not cases:
            print(f"no case named '{args.case}'", file=sys.stderr)
            return 2

    if args.list:
        _print_catalog(cases)
        return 0

    try:
        workers = _resolve_workers(args.workers)
    except ValueError:
        parser.error("EVAL_WORKERS must be an integer")
    if workers < 1:
        parser.error("workers must be >= 1")

    effective_workers = max(1, min(workers, len(cases) * args.runs))
    model = resolve_model_id()
    reasoning_effort = resolve_reasoning_effort()
    print(
        f"Eval gate · category={args.category} · model={model} · "
        f"effort={reasoning_effort} · runs={args.runs} · cases={len(cases)} · "
        f"workers={effective_workers}\n"
    )

    start = time.monotonic()
    if effective_workers == 1:
        evaluator = CheckDispatchEvaluator()
        results = [_run_case_n_times(c, args.runs, evaluator) for c in cases]
    else:
        results = _run_parallel(
            cases, args.runs, effective_workers, executor_factory=_spawn_pool
        )
    wall_s = time.monotonic() - start
    peak_rss_mb, cpu_s = _resource_usage()
    gate_passed = all(r["passed"] for r in results)

    report = {
        "git_sha": _git_sha(),
        "model": model,
        "reasoning_effort": reasoning_effort,
        "runs": args.runs,
        "workers": effective_workers,
        "wall_s": wall_s,
        "peak_rss_mb": peak_rss_mb,
        "cpu_s": cpu_s,
        "gate_passed": gate_passed,
        "cases": results,
    }
    _REPORT_DIR.mkdir(exist_ok=True)
    # Effort in filename: same sha + model at another effort = other verdict.
    run_slug = re.sub(r"[^A-Za-z0-9]+", "-", f"{model}-{reasoning_effort}").strip("-")
    report_path = _REPORT_DIR / f"gate-{report['git_sha']}-{run_slug}.json"
    report_path.write_text(json.dumps(report, indent=2), encoding="utf-8")

    print("\n=== Gate summary ===")
    for r in results:
        status = "PASS" if r["passed"] else "FAIL"
        print(
            f"  [{status}] {r['name']} ({r['category']}): "
            f"{r['pass_count']}/{r['runs']} (need >= {r['min_pass_rate']:.0%})"
        )
        if r["intent"]:
            print(f"        intent: {r['intent']}")
        for f in r["failures"]:
            print(f"        - {f}")
    print(f"\nwall: {wall_s:.0f}s")
    print(f"report: {report_path}")
    print(f"GATE: {'PASS' if gate_passed else 'FAIL'}")
    return 0 if gate_passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
