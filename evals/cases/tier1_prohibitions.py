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
* observed-id writes (``T1-REPLY-RESOLVE``) -- thread resolution must reach a
  root id surfaced by a prior list; resolving only a reply leaves the thread
  open while reporting it done.
* REPLACE-list preservation (``T1-HYPERLINK-PRESERVE``) -- an add must carry
  the pre-existing entries or they are silently deleted.
* round-trip sourcing (``T1-ROUNDTRIP-SOURCE``) -- raw-HTML body writes must
  come from the flagged ``get_*`` read, not synthesis Markdown.
* state-aware actions (``T1-DETACH-NOOP``) -- non-idempotent detach stays
  dormant on an item that is in no document.

Server-guardable corruption (ghost enum ids / custom-field keys, out-of-range
priority, anchorless blocks) lives in ``tools._guard`` / ``utils.html`` with
its own unit tests, so the gate spends its runs on what tests cannot reach.
"""

from __future__ import annotations

from strands_evals import Case

from evals.harness.fake_polarion import (
    DOC,
    FLOATING_TASK_HYPERLINK_URI,
    FLOATING_TASK_ID,
    ROOT_COMMENT_ID,
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
    _case(
        "T1-REPLY-RESOLVE",
        f"The feedback in the comment thread on document '{DOC}' in space "
        f"'{SPACE}' has been addressed. Mark it as resolved.",
        "resolve_root_comment",
        root_ids=[ROOT_COMMENT_ID],
    ),
    _case(
        "T1-HYPERLINK-PRESERVE",
        f"Add a hyperlink to https://example.com/review-checklist on work "
        f"item {FLOATING_TASK_ID}.",
        "preserve_hyperlinks",
        work_item_id=FLOATING_TASK_ID,
        required_uris=[FLOATING_TASK_HYPERLINK_URI],
    ),
    _case(
        "T1-ROUNDTRIP-SOURCE",
        f"In the document '{DOC}' in space '{SPACE}', change the intro "
        f"paragraph to read 'Fake intro paragraph, now reviewed.'",
        "round_trip_source",
    ),
    _case(
        "T1-DETACH-NOOP",
        f"Make sure work item {FLOATING_TASK_ID} is not part of any document.",
        "no_blind_detach",
        floating_ids=[FLOATING_TASK_ID],
    ),
]
