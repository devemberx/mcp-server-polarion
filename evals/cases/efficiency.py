"""Efficiency cases: the correct answer must be reached without waste.

The short path: one bulk call, direct id lookup, no redundant reads, right
query mechanism. ``min_pass_rate = 0.8`` — occasional waste tolerated,
systematic waste blocks.
"""

from __future__ import annotations

from strands_evals import Case

from evals.cases._shared import make_case
from evals.harness.fixtures import DOC, FLOATING_TASK_ID, SPACE

MIN_PASS_RATE = 0.8


def _case(
    name: str,
    prompt: str,
    check: str,
    *,
    intent: str,
    covers: list[str],
    **params: object,
) -> Case:
    return make_case(
        name,
        prompt,
        check,
        intent=intent,
        covers=covers,
        min_pass_rate=MIN_PASS_RATE,
        **params,
    )


CASES: list[Case] = [
    _case(
        "EFF-BULK-CREATE",
        "Create three new tasks titled 'Fake alpha', 'Fake beta', and 'Fake gamma'.",
        "single_bulk_create",
        intent="Bulk-creatable items use one create call; splitting across calls "
        "fails.",
        covers=["create_work_items"],
    ),
    _case(
        "EFF-DIRECT-GET",
        f"What is the current status and severity of work item {FLOATING_TASK_ID}?",
        "direct_read",
        intent="A known-id lookup uses get_*/read_*; a list_work_items scan fails.",
        covers=["get_work_item", "read_work_item"],
        work_item_id=FLOATING_TASK_ID,
    ),
    _case(
        "EFF-NO-DUP-READS",
        f"Summarize the document '{DOC}' in space '{SPACE}' and list any "
        f"unresolved comment threads on it.",
        "no_duplicate_reads",
        intent="No identical re-read while nothing changed; a redundant repeat fails.",
        covers=["read_document", "list_document_comments"],
    ),
    _case(
        "EFF-ENUM-ONCE",
        "Create two tasks: 'Fake delta' with severity must_have and "
        "'Fake epsilon' with severity nice_to_have.",
        "no_duplicate_reads",
        intent="If enum options are fetched, they are fetched once; re-fetching "
        "identical options fails.",
        covers=["list_work_item_enum_options", "create_work_items"],
    ),
    _case(
        "EFF-SQL-NOT-LUCENE",
        f"List the IDs of the work items contained in the document '{DOC}' "
        f"in space '{SPACE}'.",
        "scoped_query_uses_sql",
        intent="Document scoping uses SQL:(...) or read_document_parts; a Lucene "
        "module term fails. A SQL query must be recipe-sourced (get_sql_query_recipes "
        "first).",
        covers=["list_work_items", "get_sql_query_recipes"],
    ),
    _case(
        "EFF-DETACH-NOOP",
        f"Make sure work item {FLOATING_TASK_ID} is not part of any document.",
        "no_detach_retry_loop",
        intent="A doomed detach is attempted at most once; a retry loop on the "
        "same item fails.",
        covers=["move_work_item_from_document"],
        floating_ids=[FLOATING_TASK_ID],
    ),
    _case(
        "EFF-TABLE-RECIPE-SOURCED",
        f"Append a two-column table listing the milestones 'M1: design' and "
        f"'M2: review' to the description of work item {FLOATING_TASK_ID}, "
        f"with a numbered caption 'Milestones' below it.",
        "table_html_recipe_sourced",
        intent="Raw-HTML table edits are template-sourced (get_html_recipes "
        "before the update); hand-written table markup fails.",
        covers=["update_work_item", "get_html_recipes"],
    ),
]
