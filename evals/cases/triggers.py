"""Trigger cases: a neutral request must route to the correct tool / path.

The behaviour under test is tool selection — did the right tool fire (and the
tempting wrong one not)? ``min_pass_rate = 1.0``: a mis-trigger blocks deploy,
so each prompt is phrased to admit exactly one correct tool family. Tasks stay
neutral — never state the rule, or you test the prompt instead of the tool
docstrings (the only guard).
"""

from __future__ import annotations

from strands_evals import Case

from evals.harness.fixtures import (
    CHILD_REQ_ID,
    DOC,
    FLOATING_TASK_ID,
    PARENT_REQ_ID,
    SPACE,
)

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
    # --- natural-language routing: request -> the one correct tool family -----
    _case(
        "TRIG-PROJECTS",
        "List the Polarion projects.",
        "triggers_tool",
        intent="A project-listing request must call list_projects.",
        covers=["list_projects"],
        expect="list_projects",
    ),
    _case(
        "TRIG-LIST-DOCS",
        f"What documents exist in the '{SPACE}' space?",
        "triggers_tool",
        intent="A document-enumeration request must call list_documents.",
        covers=["list_documents"],
        expect="list_documents",
    ),
    _case(
        "TRIG-CREATE-DOC",
        f"Create a new document titled 'Release Notes' in the '{SPACE}' space.",
        "triggers_tool",
        intent="Creating a document must call create_document, not update_document.",
        covers=["create_document"],
        expect="create_document",
        reject=["update_document"],
    ),
    _case(
        "TRIG-WI-COMMENT",
        f"Add a comment 'Looks good' to work item {FLOATING_TASK_ID}.",
        "triggers_tool",
        intent="Commenting on a work item must call create_work_item_comments.",
        covers=["create_work_item_comments"],
        expect="create_work_item_comments",
    ),
    _case(
        "TRIG-DOC-COMMENT",
        f"Leave a comment 'Needs review' on the document '{DOC}' in the "
        f"'{SPACE}' space.",
        "triggers_tool",
        intent="Commenting on a document must call create_document_comments.",
        covers=["create_document_comments"],
        expect="create_document_comments",
    ),
    _case(
        "TRIG-WI-COMMENTS-LIST",
        f"Show the comments on work item {FLOATING_TASK_ID}.",
        "triggers_tool",
        intent="Reading a work item's comments must call list_work_item_comments.",
        covers=["list_work_item_comments"],
        expect="list_work_item_comments",
    ),
    _case(
        "TRIG-DOC-ENUM",
        f"What are the allowed values for the 'status' field on the document "
        f"'{DOC}' in the '{SPACE}' space?",
        "triggers_tool",
        intent="Asking for a document field's options must call "
        "list_document_enum_options.",
        covers=["list_document_enum_options"],
        expect="list_document_enum_options",
    ),
    _case(
        "TRIG-UNLINK",
        f"Remove the link between work item {CHILD_REQ_ID} and {PARENT_REQ_ID}.",
        "triggers_tool",
        intent="Deleting a link must call delete_work_item_links, not "
        "update_work_item_link.",
        covers=["delete_work_item_links"],
        expect="delete_work_item_links",
        reject=["update_work_item_link"],
    ),
]
