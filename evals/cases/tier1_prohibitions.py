"""Tier-1 forbidden-behaviour cases.

Each case names a deterministic check (see ``evaluators/checks.py``) via
``metadata["check"]`` and a ``min_pass_rate`` the gate enforces. Tier-1
prohibitions are zero-tolerance: ``min_pass_rate = 1.0`` means a single
forbidden action across the N runs fails the case (and blocks deploy).

Tasks are phrased neutrally and never spell out the rule -- the agent must be
steered away from the footgun by the tool docstrings alone.

Scope is the subset of LLM behaviour the mcp-server tool layer cannot guard
deterministically:

* read-before-write discipline (``T1-UPDATE-NEEDS-GET``) -- the server can
  fetch state internally for validation, but only the agent's trajectory
  reveals whether it actually called ``get_*`` to observe current values
  before patching.
* path-shape discipline (``T1-WI-TO-DOC``, ``T1-HEADING-TO-DOC``) -- there
  are two structurally different ways to add content to a document and the
  wrong one silently corrupts state.
* read-only intent (``T1-READONLY``) -- write tools must stay dormant on a
  pure read task.

Silent-corruption modes that *can* be guarded server-side (ghost enum ids,
ghost custom-field keys, out-of-range priority, anchorless body blocks) are
enforced by ``mcp_server_polarion.tools._guard`` / ``utils.html`` and verified
by ``tests/mcp_server_polarion/tools/test_guard.py`` /
``tests/mcp_server_polarion/utils/test_html.py`` -- they do not
appear here so the gate spends its runs on behaviours unit tests cannot reach.
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
