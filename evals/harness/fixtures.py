"""Synthetic seed data + identifiers for in-process fake Polarion. Every
string invented (no production data in eval logs) but *structure* mirror
MCP_Test_Project. Eval cases import these ids; ``fake_polarion`` serve
resources built from ``SEEDS``.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal

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

# Minimal 1x1 transparent PNG; content route serve verbatim for
# DOC_ATTACHMENT_ID -- byte validity matters (real PNG signature), pixel
# content does not.
DOC_ATTACHMENT_CONTENT = (
    b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01"
    b"\x08\x04\x00\x00\x00\xb5\x1c\x0c\x02\x00\x00\x00\x0bIDATx\xdacd`\x00"
    b"\x00\x00\x06\x00\x020\x81\xd0/\x00\x00\x00\x00IEND\xaeB`\x82"
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

TS = "2026-01-01T00:00:00.000Z"


@dataclass
class WorkItem:
    short_id: str
    title: str
    type: str
    status: str = "open"
    priority: str = "50.0"
    severity: str = "should_have"
    module_id: str = ""  # full module id (PROJECT/SPACE/DOC) if in document, else ""
    outline_number: str = ""
    hyperlinks: list[dict[str, str]] = field(default_factory=list)
    # Keys MUST stay outside ``STANDARD_WORK_ITEM_ATTRIBUTES`` — else merge
    # into resource attributes dict shadow real attributes.
    custom_fields: dict[str, str] = field(default_factory=dict)
    comments: list[Comment] = field(default_factory=list)


@dataclass
class DocumentPart:
    """One entry in document's ordered part chain. ``part_id`` suffix derive
    as ``{kind}_{work_item_id}``; ``nextPart`` links derived from order.
    """

    kind: Literal["heading", "workitem"]
    work_item_id: str
    level: int = 1  # heading level; ignored for workitem parts


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


@dataclass
class Attachment:
    """Document attachment. ``attachment_id`` carry server-assigned numeric
    prefix and is the token body HTML reference as ``attachment:{id}``.
    Polarion serve no mime type and no created timestamp here.
    """

    attachment_id: str
    title: str
    length: int


@dataclass
class Document:
    name: str
    title: str
    body_html: str
    type: str = "systemRequirementSpecification"
    status: str = "draft"
    parts: list[DocumentPart] = field(default_factory=list)
    comments: list[Comment] = field(default_factory=list)
    attachments: list[Attachment] = field(default_factory=list)


@dataclass(frozen=True)
class Seeds:
    """Read-only seed tables. ``frozen`` block attribute rebind, not dict
    mutation — sufficient since nothing mutate these (writes record into
    ``FakePolarion.mutations`` instead). Add entity by adding table entry;
    ``FakePolarion`` serve it without per-entity branching.
    """

    work_items: dict[str, WorkItem]
    documents: dict[str, Document]
    test_runs: dict[str, TestRun]
    links: dict[str, list[tuple[str, str]]]
    enums: dict[tuple[str, str], list[tuple[str, bool]]]
    project_enums: dict[str, list[str]]


SEEDS = Seeds(
    # Structure mirror MCP_Test_Project; every string synthetic.
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
            body_html=(
                '<h1 id="h-1">Fake Doc</h1>'
                f'<p id="{DOC_INTRO_PARAGRAPH_ID}">Fake intro paragraph.</p>'
            ),
            # Section A heading (positional-move anchor) -> one work-item part.
            parts=[
                DocumentPart("heading", DOC_HEADING_ID, level=1),
                DocumentPart("workitem", CHILD_REQ_ID),
            ],
            # Root + one reply; resolve root = resolve whole thread.
            comments=[
                Comment(ROOT_COMMENT_ID, "fake root comment"),
                Comment(
                    REPLY_COMMENT_ID, "fake reply comment", parent_id=ROOT_COMMENT_ID
                ),
            ],
            attachments=[Attachment(DOC_ATTACHMENT_ID, "fake-diagram", 17834)],
        ),
        PARENT_DOC: Document(
            name=PARENT_DOC,
            title="Fake Parent Doc",
            body_html='<h1 id="ph-1">Fake Parent Doc</h1>',
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
    # short id)]. CHILD_REQ has parent + test case; UNCOVERED_REQ deliberately
    # none.
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
