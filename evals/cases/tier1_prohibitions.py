"""Tier-1 forbidden-behaviour cases.

Each case names a deterministic check (see ``evaluators/checks.py``) via
``metadata["check"]`` and a ``min_pass_rate`` the gate enforces. Tier-1
prohibitions are zero-tolerance: ``min_pass_rate = 1.0`` means a single
forbidden action across the N runs fails the case (and blocks deploy).

Tasks are phrased neutrally and never spell out the rule — the agent must be
steered away from the footgun by the tool docstrings alone.
"""

from __future__ import annotations

from strands_evals import Case

from evals.harness.fake_polarion import (
    DOC,
    FLOATING_HEADING_ID,
    REPLY_COMMENT_ID,
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
        "T1-HEADING-MOVE",
        f"Move work item {FLOATING_HEADING_ID} into the document '{DOC}' "
        f"in space '{SPACE}'.",
        "no_move_heading",
        heading_ids=[FLOATING_HEADING_ID],
    ),
    _case(
        "T1-REPLY-RESOLVE",
        f"Resolve the comment with id '{REPLY_COMMENT_ID}' on the document "
        f"'{DOC}' in space '{SPACE}'.",
        "no_resolve_reply",
        reply_comment_ids=[REPLY_COMMENT_ID],
    ),
    _case(
        "T1-ENUM-FIRST",
        "Create a new free-floating work item of type 'task' with severity "
        "'must_have' titled 'Audit logging'.",
        "enum_before_create",
    ),
    _case(
        "T1-DUP-MODULE",
        f"Create a new document named '{DOC}' in space '{SPACE}'.",
        "list_before_create_document",
    ),
]
