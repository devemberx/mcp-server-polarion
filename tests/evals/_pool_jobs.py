"""Picklable job for real spawn-pool tests: log each start, fail on the first job."""

from __future__ import annotations

import os
import time
from pathlib import Path

from evals import run


def logging_job(case_name: str, run_idx: int) -> tuple[str, int, run.RunOutcome]:
    # Env inherited by spawned children; set before pool spawns.
    with Path(os.environ["EVAL_POOL_LOG"]).open("a", encoding="utf-8") as fh:
        fh.write(f"{case_name}:{run_idx}\n")
    if (case_name, run_idx) == (os.environ["EVAL_POOL_FAIL"], 0):
        raise RuntimeError("early boom")
    time.sleep(0.3)
    return case_name, run_idx, run.RunOutcome(True, "", 0.3, 0)
