"""Tier-1 deploy gate entry point.

Runs every Tier-1 case N times against the mocked-Polarion harness, applies
the deterministic ``ForbiddenBehaviorEvaluator``, and exits non-zero if any
case's pass rate falls below its ``min_pass_rate`` (1.0 for prohibitions).
Wired into ``publish.yml`` ahead of the PyPI publish jobs.

    uv run python -m evals.run                 # all cases, EVAL_RUNS (default 10)
    uv run python -m evals.run --case T1-READONLY --runs 1
"""

from __future__ import annotations

import sys
from pathlib import Path

# Allow `python evals/run.py` in addition to `python -m evals.run` by putting
# the repo root on the path before the package-relative imports below.
if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import argparse
import json
import os
import re
import subprocess
from typing import Any

from strands_evals import Case
from strands_evals.types.evaluation import EvaluationData

from evals.cases.tier1_prohibitions import CASES
from evals.evaluators.tier1 import ForbiddenBehaviorEvaluator
from evals.harness.model import resolve_model_id
from evals.harness.runner import run_case

_REPORT_DIR = Path(__file__).parent / "reports"


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


def _evaluate_once(
    case: Case, evaluator: ForbiddenBehaviorEvaluator
) -> tuple[bool, str]:
    task_output = run_case(case)
    data: EvaluationData[Any, Any] = EvaluationData(
        input=case.input,
        name=case.name,
        actual_output=task_output.get("output"),
        actual_trajectory=task_output.get("trajectory"),
        metadata=case.metadata,
    )
    result = evaluator.evaluate(data)[0]
    return result.test_pass, result.reason or ""


def _run_case_n_times(
    case: Case, runs: int, evaluator: ForbiddenBehaviorEvaluator
) -> dict[str, Any]:
    min_rate = float((case.metadata or {}).get("min_pass_rate", 1.0))
    passes = 0
    failures: list[str] = []
    for i in range(runs):
        passed, reason = _evaluate_once(case, evaluator)
        if passed:
            passes += 1
        else:
            failures.append(f"run {i + 1}: {reason}")
        print(f"  {case.name} run {i + 1}/{runs}: {'PASS' if passed else 'FAIL'}")
    pass_rate = passes / runs if runs else 0.0
    return {
        "name": case.name,
        "runs": runs,
        "pass_count": passes,
        "pass_rate": pass_rate,
        "min_pass_rate": min_rate,
        "passed": pass_rate >= min_rate,
        "failures": failures,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Tier-1 deploy gate")
    parser.add_argument("--case", help="run only this case name (e.g. T1-READONLY)")
    parser.add_argument(
        "--runs",
        type=int,
        default=int(os.environ.get("EVAL_RUNS", "10")),
        help="runs per case (default EVAL_RUNS or 10)",
    )
    args = parser.parse_args()

    cases = CASES
    if args.case:
        cases = [c for c in CASES if c.name == args.case]
        if not cases:
            print(f"no case named '{args.case}'", file=sys.stderr)
            return 2

    evaluator = ForbiddenBehaviorEvaluator()
    model = resolve_model_id()
    print(f"Tier-1 gate · model={model} · runs={args.runs} · cases={len(cases)}\n")

    results = [_run_case_n_times(c, args.runs, evaluator) for c in cases]
    gate_passed = all(r["passed"] for r in results)

    report = {
        "git_sha": _git_sha(),
        "model": model,
        "runs": args.runs,
        "gate_passed": gate_passed,
        "cases": results,
    }
    _REPORT_DIR.mkdir(exist_ok=True)
    model_slug = re.sub(r"[^A-Za-z0-9]+", "-", model).strip("-")
    report_path = _REPORT_DIR / f"tier1-{report['git_sha']}-{model_slug}.json"
    report_path.write_text(json.dumps(report, indent=2), encoding="utf-8")

    print("\n=== Tier-1 summary ===")
    for r in results:
        status = "PASS" if r["passed"] else "FAIL"
        print(
            f"  [{status}] {r['name']}: {r['pass_count']}/{r['runs']} "
            f"(need >= {r['min_pass_rate']:.0%})"
        )
        for f in r["failures"]:
            print(f"        - {f}")
    print(f"\nreport: {report_path}")
    print(f"GATE: {'PASS' if gate_passed else 'FAIL'}")
    return 0 if gate_passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
