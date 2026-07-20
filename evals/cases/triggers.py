"""Trigger cases: neutral request must route to correct tool.

``min_pass_rate = 1.0`` — mis-trigger block deploy. Each prompt admit one
correct tool family; never state rule, else you test prompt instead of tool
docstrings (only guard).
"""

from __future__ import annotations

from pathlib import Path

from strands_evals import Case

from evals.cases._shared import make_case
from evals.harness.fixtures import (
    CHILD_REQ_ID,
    DOC,
    DOC_ATTACHMENT_ID,
    FLOATING_TASK_ID,
    PARENT_REQ_ID,
    PROJECT,
    SPACE,
    TEST_RUN_ID,
    TESTCASE_ID,
)

MIN_PASS_RATE = 1.0

# Real repo file: upload trigger need existing local path (tool read disk
# pre-request; missing path = ValueError before any tool-routing signal).
_UPLOAD_ASSET = Path(__file__).resolve().parents[2] / ".github" / "assets" / "icon.png"


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
        "TRIG-COPY-DOC",
        f"Duplicate the document '{DOC}' in space '{SPACE}' as 'FakeDocCopy'.",
        "triggers_tool",
        intent="Duplicating an existing document must call copy_document; "
        "rebuilding it via create_document/update_document loses contained items.",
        covers=["copy_document"],
        expect="copy_document",
        reject=["create_document", "update_document"],
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
        "TRIG-DOC-ATTACH-CREATE",
        f"Attach the file at {_UPLOAD_ASSET} to the document '{DOC}' in the "
        f"'{SPACE}' space.",
        "triggers_tool",
        intent="Uploading a local file to a document must call "
        "create_document_attachments; listing or reading attachments does not.",
        covers=["create_document_attachments"],
        expect="create_document_attachments",
        reject=["list_document_attachments", "get_document_attachment_content"],
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
        "TRIG-DOC-ATTACHMENTS",
        f"What files are attached to the document '{DOC}' in the '{SPACE}' space?",
        "triggers_tool",
        intent="Listing a document's attached files must call "
        "list_document_attachments; rendering the body does not enumerate them.",
        covers=["list_document_attachments"],
        expect="list_document_attachments",
        reject=["read_document"],
    ),
    _case(
        "TRIG-DOC-ATTACHMENT-CONTENT",
        f"Show me the image attachment '{DOC_ATTACHMENT_ID}' from the document "
        f"'{DOC}' in the '{SPACE}' space.",
        "triggers_tool",
        intent="Viewing an attachment's image content must call "
        "get_document_attachment_content; listing its metadata via "
        "list_document_attachments does not render the image.",
        covers=["get_document_attachment_content"],
        expect="get_document_attachment_content",
        reject=["list_document_attachments"],
    ),
    _case(
        "TRIG-WI-ATTACHMENTS",
        f"What files are attached to work item '{FLOATING_TASK_ID}' in the "
        f"'{PROJECT}' project?",
        "triggers_tool",
        intent="Listing a work item's attached files must call "
        "list_work_item_attachments; rendering the body does not enumerate them.",
        covers=["list_work_item_attachments"],
        expect="list_work_item_attachments",
        reject=["read_work_item"],
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
    _case(
        "TRIG-LIST-TEST-RUNS",
        f"List the test runs in project '{PROJECT}'.",
        "triggers_tool",
        intent="Listing test runs must call list_test_runs, not list_work_items.",
        covers=["list_test_runs"],
        expect="list_test_runs",
        reject=["list_work_items"],
    ),
    _case(
        "TRIG-LIST-TEST-RECORDS",
        f"Which test cases failed in test run '{TEST_RUN_ID}' of project '{PROJECT}'?",
        "triggers_tool",
        intent="Reading per-test-case execution results of a run must call "
        "list_test_records, not fetch run metadata via get_test_run or "
        "list_test_runs.",
        covers=["list_test_records"],
        expect="list_test_records",
        reject=["list_test_runs", "get_test_run"],
    ),
    _case(
        "TRIG-GET-TEST-RUN",
        f"Show the full details of test run '{TEST_RUN_ID}' in project '{PROJECT}'.",
        "triggers_tool",
        intent="Fetching one known run's details must call get_test_run, not "
        "page through list_test_runs.",
        covers=["get_test_run"],
        expect="get_test_run",
        reject=["list_test_runs"],
    ),
    _case(
        "TRIG-GET-TEST-RECORD",
        f"Get the execution comment and test-case revision for test case "
        f"'{PROJECT}/{TESTCASE_ID}' iteration 0 in test run '{TEST_RUN_ID}' "
        f"of project '{PROJECT}'.",
        "triggers_tool",
        intent="Fetching one test case's execution detail inside a run must "
        "call get_test_record, not page through list_test_records or fetch "
        "run metadata via get_test_run.",
        covers=["get_test_record"],
        expect="get_test_record",
        reject=["list_test_records", "get_test_run"],
    ),
    _case(
        "TRIG-CREATE-TEST-RUN",
        f"Create a new manual test run with id 'Fake-TR-Sprint9' in project "
        f"'{PROJECT}'.",
        "triggers_tool",
        intent="Creating a test run must call create_test_runs, not "
        "create_work_items (a run is not a work item).",
        covers=["create_test_runs"],
        expect="create_test_runs",
        reject=["create_work_items"],
    ),
    _case(
        "TRIG-CREATE-TEST-RECORDS",
        f"Record that test case '{TESTCASE_ID}' passed in test run "
        f"'{TEST_RUN_ID}' of project '{PROJECT}'.",
        "triggers_tool",
        intent="Recording a test-case execution result must call "
        "create_test_records, not update_test_runs (run metadata) or "
        "create_work_items.",
        covers=["create_test_records"],
        expect="create_test_records",
        reject=["update_test_runs", "create_work_items"],
    ),
]
