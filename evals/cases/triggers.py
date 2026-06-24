"""Trigger cases: a neutral request must route to the correct tool / path.

The behaviour under test is tool selection — did the right tool fire (and the
tempting wrong one not)? ``min_pass_rate = 1.0``: a mis-trigger blocks deploy,
so each prompt is phrased to admit exactly one correct tool family. Tasks stay
neutral — never state the rule, or you test the prompt instead of the tool
docstrings (the only guard).
"""

from __future__ import annotations

from strands_evals import Case

from evals.harness.fixtures import DOC, SPACE

MIN_PASS_RATE = 1.0


def _case(
    name: str,
    prompt: str,
    check: str,
    *,
    intent: str,
    covers: list[str],
    **params: object,
) -> Case:
    return Case(
        name=name,
        input=prompt,
        metadata={
            "check": check,
            "params": params,
            "min_pass_rate": MIN_PASS_RATE,
            "intent": intent,
            "covers": covers,
        },
    )


CASES: list[Case] = [
    _case(
        "TRIG-WI-TO-DOC",
        f"Add a new requirement work item titled 'Login latency budget' "
        f"into the document '{DOC}' in space '{SPACE}'.",
        "no_update_document",
        intent="Adding a work item to a document must create + move; using "
        "update_document fails.",
        covers=["create_work_items", "move_work_item_to_document"],
    ),
    _case(
        "TRIG-HEADING-TO-DOC",
        f"Add a new section heading titled 'Performance' to the document "
        f"'{DOC}' in space '{SPACE}'.",
        "heading_to_doc",
        intent="Adding a heading must go through update_document; create/move fails.",
        covers=["update_document"],
    ),
]
