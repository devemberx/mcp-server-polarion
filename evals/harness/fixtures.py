"""Synthetic seed data + identifiers for in-process fake Polarion. Every
string invented (no production data in eval logs) but *structure* mirror
MCP_Test_Project. Eval cases import these ids; ``fake_polarion`` deep-copy
``SEEDS`` into one mutable world per run.
"""

from __future__ import annotations

from dataclasses import dataclass, field

POLARION_HOST = "https://polarion.example.com"
API_PREFIX = "/polarion/rest/v1"
PROJECT = "MCP_Test_Project"
SPACE = "_default"
DOC = "FakeDoc"
AUTHOR = "u-fake-0001"
MODULE_ID = f"{PROJECT}/{SPACE}/{DOC}"

# Heading work items carrying `module` relationship — only ones
# `list_documents` discovery scan (query=type:heading) surface.
DOC_HEADING_ID = "MCPT-100"

# Free-floating (space_id == "") seeds for move/heading cases.
FLOATING_TASK_ID = "MCPT-200"
FLOATING_HEADING_ID = "MCPT-201"
FLOATING_GHOST_ID = "MCPT-202"

# Reply comment id (parent == root "1") used by SAFE-REPLY-RESOLVE.
ROOT_COMMENT_ID = "1"
REPLY_COMMENT_ID = "2"

# Pre-existing hyperlink on floating task; SAFE-HYPERLINK-PRESERVE assert
# update keep it (Polarion REPLACES whole list).
FLOATING_TASK_HYPERLINK_URI = "https://specs.example.com/fake-spec"

# Anchored intro paragraph in doc body; SAFE-ROUNDTRIP-SOURCE edit it.
DOC_INTRO_PARAGRAPH_ID = "p-1"

# Attachment on DOC; numeric prefix mirror server-assigned real ids.
DOC_ATTACHMENT_ID = "1-fake-diagram.png"

# 1x1 transparent PNG, served verbatim for DOC_ATTACHMENT_ID — real
# signature matter, pixel content not.
DOC_ATTACHMENT_CONTENT = (
    b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01"
    b"\x08\x04\x00\x00\x00\xb5\x1c\x0c\x02\x00\x00\x00\x0bIDATx\xdacd`\x00"
    b"\x00\x00\x06\x00\x020\x81\xd0/\x00\x00\x00\x00IEND\xaeB`\x82"
)

# Attachment on FLOATING_TASK_ID.
WORKITEM_ATTACHMENT_ID = "1-fake-screenshot.png"

# 1x1 red PNG, served verbatim for WORKITEM_ATTACHMENT_ID -- bytes distinct
# from DOC_ATTACHMENT_CONTENT so eval checks prove routing, not luck.
WORKITEM_ATTACHMENT_CONTENT = (
    b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01"
    b"\x08\x02\x00\x00\x00\x90wS\xde\x00\x00\x00\x0cIDATx\x9cc8!'\x07\x00"
    b"\x02\xb6\x01\x054\xa6u\xaa\x00\x00\x00\x00IEND\xaeB`\x82"
)

# Second document + requirement traceability seeds (orchestration cases).
PARENT_DOC = "FakeParentDoc"
PARENT_MODULE_ID = f"{PROJECT}/{SPACE}/{PARENT_DOC}"
CHILD_REQ_ID = (
    "MCPT-300"  # in FakeDoc; satisfies PARENT_REQ_ID, verified by TESTCASE_ID
)
PARENT_REQ_ID = "MCPT-400"  # in FakeParentDoc
UNCOVERED_REQ_ID = "MCPT-301"  # in FakeDoc; no test-case link (coverage-gap signal)
TESTCASE_ID = "MCPT-500"  # test case linked from CHILD_REQ_ID

# Section A heading part id served by read_document_parts; anchor positional moves.
SECTION_A_PART_ID = f"heading_{DOC_HEADING_ID}"

# Test run instance served by list_test_runs (TRIG-LIST-TEST-RUNS).
TEST_RUN_ID = "Fake-TR-001"

# Extra run instances so 3-item bulk update prompt possible (EFF-BULK-UPDATE-RUNS).
TEST_RUN_ID_2 = "Fake-TR-002"
TEST_RUN_ID_3 = "Fake-TR-003"

# Template blueprint; create_test_runs resolve it via template guard.
TEST_RUN_TEMPLATE_ID = "Fake-TR-Template"

# Attachment on TEST_RUN_ID's iteration-0 record for TESTCASE_ID; server
# prepends {testCaseId}_ to upload name, so id/fileName both carry it.
RECORD_ATTACHMENT_ID = f"{TESTCASE_ID}_fake-log.txt"

# Image sibling on same record; 1x1 blue PNG served verbatim by content
# route -- bytes distinct from DOC/WORKITEM attachment content so eval
# checks prove routing, not luck.
RECORD_IMAGE_ATTACHMENT_ID = f"{TESTCASE_ID}_fake-screenshot.png"
RECORD_IMAGE_ATTACHMENT_CONTENT = (
    b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01"
    b"\x08\x02\x00\x00\x00\x90wS\xde\x00\x00\x00\x0cIDATx\xdac``\xf8\x0f\x00"
    b"\x01\x03\x01\x006t\x11@\x00\x00\x00\x00IEND\xaeB`\x82"
)

TS = "2026-01-01T00:00:00.000Z"

# Body placeholder id of in-document work item (live form, verified
# 2026-11-15): heading = empty ``<hN id=...>``, other item = ``<div id=...>``.
MACRO_PREFIX = "polarion_wiki macro name=module-workitem;params=id="


def heading_html(work_item_id: str, level: int) -> str:
    """Heading placeholder; tag ``h{level + 1}`` (``<h2>`` = level 1)."""
    tag = f"h{level + 1}"
    return f'<{tag} id="{MACRO_PREFIX}{work_item_id}"></{tag}>'


def work_item_html(work_item_id: str) -> str:
    return f'<div id="{MACRO_PREFIX}{work_item_id}"></div>'


@dataclass
class WorkItem:
    short_id: str
    title: str
    type: str
    status: str = "open"
    priority: str = "50.0"
    severity: str = "should_have"
    # Full module id (PROJECT/SPACE/DOC) = document membership, else "".
    # Member must also sit in that document's body (else orphan).
    module_id: str = ""
    outline_number: str = ""
    hyperlinks: list[dict[str, str]] = field(default_factory=list)
    # Keys MUST stay outside ``STANDARD_WORK_ITEM_ATTRIBUTES`` — else merge
    # into resource attributes dict shadow real attributes.
    custom_fields: dict[str, str] = field(default_factory=dict)
    comments: list[Comment] = field(default_factory=list)
    attachments: list[Attachment] = field(default_factory=list)


@dataclass
class Comment:
    """Document or work-item comment. ``parent_id is None`` mark thread root;
    child links derived from set (no redundant child-id lists). ``title``
    work-item-only — document comments leave it "" (never emitted).
    """

    comment_id: str
    text: str
    resolved: bool = False
    parent_id: str | None = None
    title: str = ""


@dataclass
class TestRun:
    """Test run. ``is_template`` split template blueprints from actual run
    instances (``templates`` query param filter on it).
    """

    short_id: str
    title: str
    type: str = "manual"
    status: str = "open"
    finished_on: str = ""
    is_template: bool = False
    # TESTCASE_ID re-executions -- one record per iteration 0..n-1.
    iterations: int = 1
    # Iteration-0 record's attachments only -- other iterations serve empty.
    record_attachments: list[Attachment] = field(default_factory=list)


@dataclass
class Attachment:
    """Document, work-item, or test-record attachment. ``attachment_id``
    shape vary by domain (doc = fileName, WI = counter-prefixed, testrecord
    = ``{testCaseId}_`` prefixed); doc body token ``attachment:{id}``, work
    item body token ``workitemimg:{id}``. Polarion serve no mime type and
    no created timestamp here. ``content`` = bytes the content route serve.
    """

    attachment_id: str
    title: str
    length: int
    content: bytes = b""


@dataclass
class Document:
    """Seed document. ``body_html`` = whole document in live stored form:
    work items as macro placeholders (``heading_html`` / ``work_item_html``),
    every other top-level block carry a unique ``id`` (= its part id).
    ``/parts`` derive from body order. ``structure_link_role`` = role of
    auto link from item to heading above it (REST-made default
    ``relates_to``, UI-made ``parent``).
    """

    name: str
    title: str
    body_html: str
    type: str = "systemRequirementSpecification"
    status: str = "draft"
    structure_link_role: str = "relates_to"
    comments: list[Comment] = field(default_factory=list)
    attachments: list[Attachment] = field(default_factory=list)


@dataclass(frozen=True)
class Seeds:
    """Read-only seed tables. ``frozen`` block attribute rebind; ``FakePolarion``
    deep-copy tables into its own world, so writes never reach these. Add
    entity by adding table entry; fake serve it without per-entity branching.
    """

    work_items: dict[str, WorkItem]
    documents: dict[str, Document]
    test_runs: dict[str, TestRun]
    links: dict[str, list[tuple[str, str]]]
    enums: dict[tuple[str, str], list[tuple[str, bool]]]
    project_enums: dict[str, list[str]]


SEEDS = Seeds(
    work_items={
        DOC_HEADING_ID: WorkItem(
            DOC_HEADING_ID,
            "Section A",
            "heading",
            module_id=MODULE_ID,
            outline_number="1",
        ),
        FLOATING_TASK_ID: WorkItem(
            FLOATING_TASK_ID,
            "Floating task",
            "task",
            hyperlinks=[{"role": "ref_ext", "uri": FLOATING_TASK_HYPERLINK_URI}],
            custom_fields={"acceptance_criteria_id": "AC-1"},
            # One root comment so list_work_item_comments return populated
            # page; work-item comment ids 3-segment + carry title.
            comments=[Comment("c-1", "Fake work item comment", title="Initial note")],
            attachments=[
                Attachment(
                    WORKITEM_ATTACHMENT_ID,
                    "fake-screenshot",
                    4096,
                    WORKITEM_ATTACHMENT_CONTENT,
                )
            ],
        ),
        FLOATING_HEADING_ID: WorkItem(
            FLOATING_HEADING_ID, "Floating heading", "heading"
        ),
        FLOATING_GHOST_ID: WorkItem(FLOATING_GHOST_ID, "Ghost type", "not_a_real_type"),
        CHILD_REQ_ID: WorkItem(
            CHILD_REQ_ID, "Child requirement", "systemrequirement", module_id=MODULE_ID
        ),
        UNCOVERED_REQ_ID: WorkItem(
            UNCOVERED_REQ_ID,
            "Uncovered requirement",
            "systemrequirement",
            module_id=MODULE_ID,
        ),
        PARENT_REQ_ID: WorkItem(
            PARENT_REQ_ID,
            "Parent requirement",
            "systemrequirement",
            module_id=PARENT_MODULE_ID,
        ),
        TESTCASE_ID: WorkItem(TESTCASE_ID, "Coverage test case", "systemtestcase"),
    },
    documents={
        DOC: Document(
            name=DOC,
            title="Fake Doc",
            # Intro paragraph, then Section A heading (positional-move anchor)
            # over both requirements.
            body_html="\n".join(
                [
                    f'<p id="{DOC_INTRO_PARAGRAPH_ID}">Fake intro paragraph.</p>',
                    heading_html(DOC_HEADING_ID, level=1),
                    work_item_html(CHILD_REQ_ID),
                    work_item_html(UNCOVERED_REQ_ID),
                ]
            ),
            # Root + one reply; resolve root = resolve whole thread.
            comments=[
                Comment(ROOT_COMMENT_ID, "fake root comment"),
                Comment(
                    REPLY_COMMENT_ID, "fake reply comment", parent_id=ROOT_COMMENT_ID
                ),
            ],
            attachments=[
                Attachment(
                    DOC_ATTACHMENT_ID, "fake-diagram", 17834, DOC_ATTACHMENT_CONTENT
                )
            ],
        ),
        # Heading-less on purpose: list_documents discovery scan surface only
        # docs with heading, so FakeDoc stay sole discoverable spec.
        PARENT_DOC: Document(
            name=PARENT_DOC,
            title="Fake Parent Doc",
            body_html="\n".join(
                [
                    '<p id="pp-1">Fake parent intro.</p>',
                    work_item_html(PARENT_REQ_ID),
                ]
            ),
        ),
    },
    test_runs={
        # TEST_RUN_ID stay data[0] — test_fake_polarion assert on it.
        TEST_RUN_ID: TestRun(
            TEST_RUN_ID,
            "Fake Regression Run",
            type="manual",
            status="open",
            finished_on=TS,
            record_attachments=[
                Attachment(RECORD_ATTACHMENT_ID, "fake-log", 256, b"fake log line\n"),
                Attachment(
                    RECORD_IMAGE_ATTACHMENT_ID,
                    "fake-screenshot",
                    len(RECORD_IMAGE_ATTACHMENT_CONTENT),
                    RECORD_IMAGE_ATTACHMENT_CONTENT,
                ),
            ],
        ),
        # 3 iterations feed EFF-BULK-UPDATE-RECORDS (one bulk PATCH, 3 items).
        TEST_RUN_ID_2: TestRun(TEST_RUN_ID_2, "Fake Smoke Run", iterations=3),
        TEST_RUN_ID_3: TestRun(TEST_RUN_ID_3, "Fake Sanity Run"),
        TEST_RUN_TEMPLATE_ID: TestRun(
            TEST_RUN_TEMPLATE_ID,
            "Fake Run Template",
            type="manual",
            status="open",
            is_template=True,
        ),
    },
    # Forward (outgoing) work-item links: source short id -> [(role, target
    # short id)].
    links={
        CHILD_REQ_ID: [("satisfies", PARENT_REQ_ID), ("verifies", TESTCASE_ID)],
    },
    # (resource, field_id) -> enum option ids (+ which default).
    enums={
        ("workitems", "type"): [
            ("systemrequirement", False),
            ("softwarerequirement", False),
            ("systemtestcase", False),
            ("softwaretestcase", False),
            ("risk", False),
            ("release", False),
            ("workpackage", False),
            ("task", True),
            ("changerequest", False),
            ("issue", False),
            ("testcase", False),
            ("unittestcase", False),
        ],
        ("workitems", "severity"): [
            ("must_have", False),
            ("should_have", True),
            ("nice_to_have", False),
            ("will_not_have", False),
        ],
        ("workitems", "status"): [
            ("open", True),
            ("inProgress", False),
            ("done", False),
            ("reopened", False),
        ],
        ("workitems", "priority"): [
            ("90.0", False),
            ("50.0", True),
            ("10.0", False),
        ],
        ("documents", "type"): [
            ("systemRequirementSpecification", True),
            ("softwareRequirementSpecification", False),
        ],
        ("documents", "status"): [
            ("draft", True),
            ("inReview", False),
            ("approved", False),
        ],
    },
    # Project-level enums served dict-shaped (attributes.options[].id), unlike
    # getAvailableOptions' list. Key context: bare = "~", testrun = "testing".
    project_enums={
        "hyperlink-role": ["ref_int", "ref_ext"],
        "workitem-link-role": ["relates_to", "parent", "satisfies", "verifies"],
        "testing/testrun-type": ["manual", "automated"],
        "testing/testrun-status": ["open", "inProgress", "finished"],
        "testing/test-result": ["passed", "failed", "blocked"],
    },
)
