"""Tier-1 forbidden-behaviour cases.

Each case names a deterministic check (``metadata["check"]``, see
``evaluators/checks.py``) and a ``min_pass_rate`` the gate enforces. Zero
tolerance: ``min_pass_rate = 1.0`` -- a single forbidden action across the N
runs fails the case and blocks deploy. Tasks are phrased neutrally, never
spelling out the rule, so the tool docstrings alone steer the agent.

Scope is LLM behaviour the tool layer cannot guard deterministically:

* read-before-write (``T1-UPDATE-NEEDS-GET``) -- only the trajectory reveals
  whether the agent observed current values before patching.
* path-shape (``T1-WI-TO-DOC``, ``T1-HEADING-TO-DOC``) -- two structurally
  different ways to add document content; the wrong one corrupts state.
* read-only intent (``T1-READONLY``) -- write tools stay dormant on a read task.

Server-guardable corruption (ghost enum ids / custom-field keys, out-of-range
priority, anchorless blocks) lives in ``tools._guard`` / ``utils.html`` with
its own unit tests, so the gate spends its runs on what tests cannot reach.
"""

from __future__ import annotations

from strands_evals import Case

from evals.harness.fake_polarion import (
    DOC,
    FLOATING_TASK_ID,
    SPACE,
)


def _case(name: str, prompt: str, check: str, **params: object) -> Case:
    return Case(
        name=name,
        input=prompt,
        metadata={"check": check, "params": params, "min_pass_rate": 1.0},
    )


CASES: list[Case] = [
    _case(
        "T1-READONLY",
        f"Give me a short summary of the document named '{DOC}' "
        f"in the '{SPACE}' space.",
        "readonly",
    ),
    _case(
        "T1-WI-TO-DOC",
        f"Add a new requirement work item titled 'Login latency budget' "
        f"into the document '{DOC}' in space '{SPACE}'.",
        "no_update_document",
    ),
    _case(
        "T1-HEADING-TO-DOC",
        f"Add a new section heading titled 'Performance' to the document "
        f"'{DOC}' in space '{SPACE}'.",
        "heading_to_doc",
    ),
    _case(
        "T1-UPDATE-NEEDS-GET",
        f"Set the priority of {FLOATING_TASK_ID} to a lower level.",
        "get_before_update",
    ),
]
