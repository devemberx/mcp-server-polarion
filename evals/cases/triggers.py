"""Trigger cases: a neutral request must route to the correct tool.

``min_pass_rate = 1.0`` — a mis-trigger blocks deploy. Each prompt admits one
correct tool family; never state the rule, or you test the prompt instead of
the tool docstrings (the only guard).
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
        "triggers_tool",
        intent="Adding a work item to a document must move it in (which needs a "
        "prior create); update_document fails.",
        covers=["move_work_item_to_document"],
        expect="move_work_item_to_document",
        reject=["update_document"],
    ),
    _case(
        "TRIG-HEADING-TO-DOC",
        f"Add a new section heading titled 'Performance' to the document "
        f"'{DOC}' in space '{SPACE}'.",
        "triggers_tool",
        intent="Adding a heading must go through update_document; create/move fails.",
        covers=["update_document"],
        expect="update_document",
        reject=["create_work_items", "move_work_item_to_document"],
    ),
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
        "TRIG-WI-ENUM",
        f"What severity levels can be set on work item {FLOATING_TASK_ID}?",
        "triggers_tool",
        intent="Asking for a work item field's options must call "
        "list_work_item_enum_options.",
        covers=["list_work_item_enum_options"],
        expect="list_work_item_enum_options",
    ),
    _case(
        "TRIG-WI-COMMENT-RESOLVE",
        f"The note on work item {FLOATING_TASK_ID} has been handled -- mark it "
        f"resolved.",
        "triggers_tool",
        intent="Resolving a work item comment must call update_work_item_comment.",
        covers=["update_work_item_comment"],
        expect="update_work_item_comment",
    ),
    _case(
        "TRIG-LINK-SUSPECT",
        f"Flag the link from work item {CHILD_REQ_ID} to its parent requirement "
        f"as suspect.",
        "triggers_tool",
        intent="Changing an existing link's suspect flag must call "
        "update_work_item_link, not delete.",
        covers=["update_work_item_link"],
        expect="update_work_item_link",
        reject=["delete_work_item_links"],
    ),
    _case(
        "TRIG-READ-NOT-GET",
        f"Summarize the document '{DOC}' in space '{SPACE}'.",
        "triggers_tool",
        intent="Summarizing a document routes to read_document (renders the full "
        "body); get_document (metadata only) and read_document_parts (structure) "
        "are the wrong read path.",
        covers=["read_document"],
        expect="read_document",
        reject=["get_document", "read_document_parts"],
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
