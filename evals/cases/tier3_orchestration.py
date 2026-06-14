"""Tier-3 orchestration cases: "walked the correct multi-step path".

Each case asserts an ordered tool subsequence (other calls may interleave) plus
id threading between steps via the single ``ordered_trajectory`` check; the
sequence lives entirely in ``metadata["params"]``. ``min_pass_rate = 0.8`` like
Tier-2 — ordered+observed-id is flaky on weak models, so occasional waste is
tolerated, systematic mis-orchestration fails.

Groups: W = document authoring (write), R = traceability/analysis (read-only),
M = read-then-write (gated). Design principle: enumerating a document's work
items uses ``get_sql_query_recipes`` + ``list_work_items`` (SQL), never
``read_document_parts`` — the latter appears only to fetch a part-id anchor for
a positional ``move_work_item_to_document``.
"""

from __future__ import annotations

from strands_evals import Case

from evals.harness.fake_polarion import (
    CHILD_REQ_ID,
    DOC,
    FLOATING_TASK_ID,
    SPACE,
)

MIN_PASS_RATE = 0.8

# Reused step fragments. The move must run after the create (needs the new id)
# and after read_document_parts (the anchor) -- the latter is enforced by the
# observed-id source, the former by ``after``.
_READ_PARTS = {"tool": "read_document_parts", "match": {"document_name": DOC}}
_MOVE_ANCHORED = {
    "tool": "move_work_item_to_document",
    "match": {"target_document_name": DOC},
    "after": ["create_work_items"],
    "observed_arg": ["previous_part_id", "next_part_id"],
    "observed_in": "read_document_parts",
    "observed_path": "items[].id",
}


def _case(name: str, prompt: str, **params: object) -> Case:
    return Case(
        name=name,
        input=prompt,
        metadata={
            "check": "ordered_trajectory",
            "params": params,
            "min_pass_rate": MIN_PASS_RATE,
        },
    )


CASES: list[Case] = [
    # --- W: document authoring ----------------------------------------------
    _case(
        "T3-SPEC-INTO-DOC",
        f"In the document '{DOC}' in space '{SPACE}', add a new requirement work "
        f"item titled 'Cache size limit' immediately after the 'Section A' heading.",
        steps=[
            {"tool": "create_work_items"},
            _READ_PARTS,
            _MOVE_ANCHORED,
        ],
        forbid=["update_document"],
    ),
    _case(
        "T3-MIXED-DOC-UPDATE",
        f"In the document '{DOC}' in space '{SPACE}', first add a new 'Performance' "
        f"section heading with a one-sentence intro. Then add a requirement work "
        f"item titled 'p95 latency under 200ms' into the document under that heading.",
        # Crux is the split: prose/heading via update_document, the spec via
        # create + move (not everything through one tool). No anchored move --
        # the fresh heading isn't in the static fake parts.
        steps=[
            {"tool": "update_document", "match": {"document_name": DOC}},
            {"tool": "create_work_items"},
            {
                "tool": "move_work_item_to_document",
                "match": {"target_document_name": DOC},
                "after": ["create_work_items"],
            },
        ],
    ),
    _case(
        "T3-BULK-SPEC-INTO-DOC",
        f"Add these three requirement work items to the document '{DOC}' in space "
        f"'{SPACE}', right after the 'Section A' heading: 'Throughput target', "
        f"'Error budget', and 'Retry policy'.",
        steps=[
            {"tool": "create_work_items"},
            _READ_PARTS,
            _MOVE_ANCHORED,
        ],
        max_create_calls=1,
        no_dup_reads=True,
    ),
    _case(
        "T3-SPEC-WITH-LINK",
        f"Add a new requirement work item titled 'Session timeout' to the document "
        f"'{DOC}' in space '{SPACE}' after 'Section A', then add a 'relates_to' "
        f"link from it to work item {FLOATING_TASK_ID}.",
        steps=[
            {"tool": "create_work_items"},
            _READ_PARTS,
            _MOVE_ANCHORED,
            {
                "tool": "create_work_item_links",
                "after": ["move_work_item_to_document"],
            },
        ],
    ),
    # --- R: traceability / analysis (read-only) -----------------------------
    _case(
        "T3-CONSISTENCY",
        f"Check the consistency between the document '{DOC}' in space '{SPACE}' and "
        f"its parent document: for a requirement in '{DOC}' that links to a parent "
        f"requirement, compare their contents.",
        # Enumerate the doc's reqs, follow a link, read the linked parent's
        # content. Enumeration and the target read each accept the equivalent
        # tools the model picks; "SQL vs read_document_parts" is owned by Tier-2.
        steps=[
            {"tool": ["list_work_items", "read_document_parts"]},
            {"tool": "list_work_item_links"},
            {
                "tool": ["read_work_item", "get_work_item"],
                "observed_arg": "work_item_id",
                "observed_in": "list_work_item_links",
                "observed_path": "items[].id",
            },
        ],
        read_only=True,
    ),
    _case(
        "T3-IMPACT-ANALYSIS",
        f"For each work item linked to {CHILD_REQ_ID}, give a short summary of its "
        f"description.",
        # The link summary already carries title/type/status, so a description
        # summary forces a follow-up read of each linked target.
        steps=[
            {
                "tool": "list_work_item_links",
                "match": {"work_item_id": CHILD_REQ_ID},
            },
            {
                "tool": ["read_work_item", "get_work_item"],
                "observed_arg": "work_item_id",
                "observed_in": "list_work_item_links",
                "observed_path": "items[].id",
            },
        ],
        read_only=True,
    ),
    _case(
        "T3-COVERAGE-GAP",
        f"Which requirements in the document '{DOC}' in space '{SPACE}' have no "
        f"linked test case?",
        # Enumerate the doc's reqs, then inspect each one's links. Enumeration
        # accepts the equivalent tools the model picks.
        steps=[
            {"tool": ["list_work_items", "read_document_parts"]},
            {"tool": "list_work_item_links"},
        ],
        read_only=True,
    ),
    # --- M: read -> decide -> write (gated) ---------------------------------
    _case(
        "T3-CONDITIONAL-UPDATE",
        f"If work item {FLOATING_TASK_ID} is still open, raise its priority by one "
        f"level.",
        steps=[
            {"tool": "get_work_item", "match": {"work_item_id": FLOATING_TASK_ID}},
            {
                "tool": "update_work_item",
                "match": {"work_item_id": FLOATING_TASK_ID},
                "after": ["get_work_item"],
            },
        ],
    ),
    _case(
        "T3-DEDUP-BEFORE-CREATE",
        f"Add a requirement work item titled 'Cache eviction policy' to the document "
        f"'{DOC}' in space '{SPACE}' after 'Section A', but only if no work item "
        f"with that title already exists.",
        steps=[
            {"tool": "list_work_items"},
            {"tool": "create_work_items", "after": ["list_work_items"]},
            _READ_PARTS,
            _MOVE_ANCHORED,
        ],
    ),
]
