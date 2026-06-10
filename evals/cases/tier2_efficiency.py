"""Tier-2 efficiency cases.

Same harness and deterministic checks as Tier 1, but the rule is "took the
short, correct path", not "never corrupted state", so ``min_pass_rate = 0.8``
-- occasional wasteful runs are tolerated, systematic waste fails the gate.
Tasks state the goal only; the efficient path must be discoverable from the
tool docstrings alone.

* single bulk call (``T2-BULK-CREATE``) -- N items go through one
  ``create_work_items``, not N calls.
* direct lookup (``T2-DIRECT-GET``) -- a known id is fetched, not scanned for.
* no redundant reads (``T2-NO-DUP-READS``, ``T2-ENUM-ONCE``) -- identical
  reads repeat only after a write could have changed the answer.
* query mechanism (``T2-SQL-NOT-LUCENE``) -- document scoping uses the
  ``SQL:(...)`` prefix or ``read_document_parts``, never a Lucene ``module``
  term (not indexed).
"""

from __future__ import annotations

from strands_evals import Case

from evals.harness.fake_polarion import (
    DOC,
    FLOATING_TASK_ID,
    SPACE,
)

MIN_PASS_RATE = 0.8


def _case(name: str, prompt: str, check: str, **params: object) -> Case:
    return Case(
        name=name,
        input=prompt,
        metadata={"check": check, "params": params, "min_pass_rate": MIN_PASS_RATE},
    )


CASES: list[Case] = [
    _case(
        "T2-BULK-CREATE",
        "Create three new tasks titled 'Fake alpha', 'Fake beta', and 'Fake gamma'.",
        "single_bulk_create",
    ),
    _case(
        "T2-DIRECT-GET",
        f"What is the current status and severity of work item {FLOATING_TASK_ID}?",
        "direct_read",
        work_item_id=FLOATING_TASK_ID,
    ),
    _case(
        "T2-NO-DUP-READS",
        f"Summarize the document '{DOC}' in space '{SPACE}' and list any "
        f"unresolved comment threads on it.",
        "no_duplicate_reads",
    ),
    _case(
        "T2-ENUM-ONCE",
        "Create two tasks: 'Fake delta' with severity must_have and "
        "'Fake epsilon' with severity nice_to_have.",
        "no_duplicate_reads",
    ),
    _case(
        "T2-SQL-NOT-LUCENE",
        f"List the IDs of the work items contained in the document '{DOC}' "
        f"in space '{SPACE}'.",
        "scoped_query_uses_sql",
    ),
]
