"""Tier-1 forbidden-behaviour cases. Each names a check in
``evaluators/checks.py``; ``min_pass_rate = 1.0`` — one forbidden action blocks
deploy. Tasks phrased neutrally so tool docstrings alone steer the agent.

Scope = LLM behaviour the tool layer cannot guard:
read-before-write (``T1-UPDATE-NEEDS-GET``), path-shape (``T1-WI-TO-DOC``,
``T1-HEADING-TO-DOC``), read-only intent (``T1-READONLY``), observed-id thread
resolution (``T1-REPLY-RESOLVE``), REPLACE-list preservation
(``T1-HYPERLINK-PRESERVE``), round-trip sourcing (``T1-ROUNDTRIP-SOURCE``).
Server-guardable corruption lives in ``tools._guard`` / ``utils.html`` with its
own unit tests.
"""

from __future__ import annotations

from strands_evals import Case

from evals.harness.fixtures import (
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
]
