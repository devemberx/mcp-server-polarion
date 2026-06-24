"""Eval deploy gate (wired into ``publish.yml``): runs every case N times,
exits non-zero below ``min_pass_rate`` (1.0 triggers/safety, 0.8
efficiency/orchestration).

    uv run python -m evals.run                      # all cases, EVAL_RUNS (default 10)
    uv run python -m evals.run --category triggers   # one category (gate stages them)
    uv run python -m evals.run --list                # print the case catalog (no model)
    uv run python -m evals.run --case SAFE-READONLY --runs 1
"""

from __future__ import annotations

import sys
from pathlib import Path

# Put repo root on the path so `python evals/run.py` works alongside `-m`.
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

from evals.cases.efficiency import CASES as EFFICIENCY_CASES
from evals.cases.orchestration import CASES as ORCHESTRATION_CASES
from evals.cases.safety import CASES as SAFETY_CASES
from evals.cases.triggers import CASES as TRIGGER_CASES
from evals.evaluators.dispatch import CheckDispatchEvaluator
from evals.harness.model import resolve_model_id
from evals.harness.runner import AGENT_ERROR_PREFIX, run_case

_REPORT_DIR = Path(__file__).parent / "reports"

ALL_CASES = [*TRIGGER_CASES, *SAFETY_CASES, *EFFICIENCY_CASES, *ORCHESTRATION_CASES]

# One CI job per category: a cheap early failure skips pricier later ones.
CATEGORIES: dict[str, list[Case]] = {
    "triggers": TRIGGER_CASES,
    "safety": SAFETY_CASES,
    "efficiency": EFFICIENCY_CASES,
    "orchestration": ORCHESTRATION_CASES,
    "all": ALL_CASES,
}

# Reverse lookup: case name -> its behaviour category (for catalog and report).
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


def _evaluate_once(case: Case, evaluator: CheckDispatchEvaluator) -> tuple[bool, str]:
    task_output = run_case(case)
    output = task_output.get("output")
    if isinstance(output, str) and output.startswith(AGENT_ERROR_PREFIX):
        # Crashed agent: partial trajectory must not read clean.
        return False, f"agent run failed: {output}"
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
    case: Case, runs: int, evaluator: CheckDispatchEvaluator
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

    evaluator = CheckDispatchEvaluator()
    model = resolve_model_id()
    print(
        f"Eval gate · category={args.category} · model={model} · "
        f"runs={args.runs} · cases={len(cases)}\n"
    )

    results = [_run_case_n_times(c, args.runs, evaluator) for c in cases]
    for case, result in zip(cases, results, strict=True):
        meta = case.metadata or {}
        result["category"] = _CASE_CATEGORY.get(str(case.name), "?")
        result["intent"] = str(meta.get("intent", ""))
        result["covers"] = list(meta.get("covers", []))
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
    report_path = _REPORT_DIR / f"gate-{report['git_sha']}-{model_slug}.json"
    report_path.write_text(json.dumps(report, indent=2), encoding="utf-8")

    print("\n=== Gate summary ===")
    for r in results:
        status = "PASS" if r["passed"] else "FAIL"
        print(
            f"  [{status}] {r['name']} ({r.get('category', '?')}): "
            f"{r['pass_count']}/{r['runs']} (need >= {r['min_pass_rate']:.0%})"
        )
        if r.get("intent"):
            print(f"        intent: {r['intent']}")
        for f in r["failures"]:
            print(f"        - {f}")
    print(f"\nreport: {report_path}")
    print(f"GATE: {'PASS' if gate_passed else 'FAIL'}")
    return 0 if gate_passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
