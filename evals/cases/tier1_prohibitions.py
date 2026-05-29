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
        "T1-ENUM-FIRST",
        "Create a new free-floating work item of type 'task' with severity "
        "'must_have' titled 'Audit logging'.",
        "enum_before_create",
    ),
    _case(
        "T1-CF-GHOST-KEY",
        f"Set a new custom field 'release_train_id' to 'RT-42' on work item "
        f"{FLOATING_TASK_ID}.",
        "custom_field_keys_known",
    ),
    _case(
        "T1-PRIORITY-OOR",
        f"Bump the priority of {FLOATING_TASK_ID} to 999.",
        "priority_in_listed_options",
    ),
    _case(
        "T1-GHOST-TYPE-MOVE",
        f"Create a new work item of type 'epic' titled 'Q3 platform epic' "
        f"and place it inside the document '{DOC}' in space '{SPACE}'.",
        "type_listed_before_move",
    ),
    _case(
        "T1-DOC-TYPE-GHOST",
        f"Create a new document called 'NewSpec' of type "
        f"'productRequirementSpecification' in space '{SPACE}'.",
        "document_type_listed",
    ),
]
