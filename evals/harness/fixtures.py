"""Synthetic seed data + identifiers for the in-process fake Polarion. Every
string is invented (no production data in eval logs) but the *structure*
mirrors MCP_Test_Project. Eval cases import these ids; ``fake_polarion``
serves resources built from ``SEEDS``.
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

# Heading work items that carry a `module` relationship — the only ones
# `list_documents`' discovery scan (query=type:heading) surfaces.
DOC_HEADING_ID = "MCPT-100"

# Free-floating (space_id == "") seeds used by the move/heading cases.
FLOATING_TASK_ID = "MCPT-200"
FLOATING_HEADING_ID = "MCPT-201"
FLOATING_GHOST_ID = "MCPT-202"

# Reply comment id (parent == root "1") used by SAFE-REPLY-RESOLVE.
ROOT_COMMENT_ID = "1"
REPLY_COMMENT_ID = "2"

# Pre-existing hyperlink on the floating task; SAFE-HYPERLINK-PRESERVE asserts
# an update keeps it (Polarion REPLACES the whole list).
FLOATING_TASK_HYPERLINK_URI = "https://specs.example.com/fake-spec"

# Anchored intro paragraph in the doc body; SAFE-ROUNDTRIP-SOURCE edits it.
DOC_INTRO_PARAGRAPH_ID = "p-1"

# Second document + requirement traceability seeds (orchestration cases).
PARENT_DOC = "FakeParentDoc"
PARENT_MODULE_ID = f"{PROJECT}/{SPACE}/{PARENT_DOC}"
CHILD_REQ_ID = (
    "MCPT-300"  # in FakeDoc; satisfies PARENT_REQ_ID, verified by TESTCASE_ID
)
PARENT_REQ_ID = "MCPT-400"  # in FakeParentDoc
UNCOVERED_REQ_ID = "MCPT-301"  # in FakeDoc; no test-case link (coverage-gap signal)
TESTCASE_ID = "MCPT-500"  # test case linked from CHILD_REQ_ID

# Section A heading part id served by read_document_parts; anchors positional moves.
SECTION_A_PART_ID = f"heading_{DOC_HEADING_ID}"

TS = "2026-01-01T00:00:00.000Z"


@dataclass
class WorkItem:
    short_id: str
    title: str
    type: str
    status: str = "open"
    priority: str = "50.0"
    severity: str = "should_have"
    module_id: str = ""  # full module id (PROJECT/SPACE/DOC) if in a document, else ""
    outline_number: str = ""
    hyperlinks: list[dict[str, str]] = field(default_factory=list)
    # Keys MUST stay outside ``STANDARD_WORK_ITEM_ATTRIBUTES`` so the merge
    # into the resource attributes dict doesn't shadow real attributes.
    custom_fields: dict[str, str] = field(default_factory=dict)


@dataclass
class DocumentPart:
    """One entry in a document's ordered part chain. ``part_id`` suffix derives
    as ``{kind}_{work_item_id}``; ``nextPart`` links are derived from order.
    """

    kind: Literal["heading", "workitem"]
    work_item_id: str
    level: int = 1  # heading level; ignored for workitem parts


@dataclass
class Comment:
    """A document comment. ``parent_id is None`` marks a thread root; child
    links are derived from the set (no redundant child-id lists).
    """

    comment_id: str
    text: str
    resolved: bool = False
    parent_id: str | None = None


@dataclass
class Document:
    name: str
    title: str
    body_html: str
    type: str = "systemRequirementSpecification"
    status: str = "draft"
    parts: list[DocumentPart] = field(default_factory=list)
    comments: list[Comment] = field(default_factory=list)


@dataclass(frozen=True)
class Seeds:
    """Read-only seed tables. ``frozen`` blocks attribute rebind, not dict
    mutation — sufficient since nothing mutates these (writes record into
    ``FakePolarion.mutations`` instead). Add an entity by adding a table entry;
    ``FakePolarion`` serves it without per-entity branching.
    """

    work_items: dict[str, WorkItem]
    documents: dict[str, Document]
    links: dict[str, list[tuple[str, str]]]
    enums: dict[tuple[str, str], list[tuple[str, bool]]]
    project_enums: dict[str, list[str]]


SEEDS = Seeds(
    # Structure mirrored from MCP_Test_Project; every string is synthetic.
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
            # Root + one reply; resolving the root resolves the whole thread.
            comments=[
                Comment(ROOT_COMMENT_ID, "fake root comment"),
                Comment(
                    REPLY_COMMENT_ID, "fake reply comment", parent_id=ROOT_COMMENT_ID
                ),
            ],
        ),
        PARENT_DOC: Document(
            name=PARENT_DOC,
            title="Fake Parent Doc",
            body_html='<h1 id="ph-1">Fake Parent Doc</h1>',
        ),
    },
    # Forward (outgoing) work-item links: source short id -> [(role, target short
    # id)]. CHILD_REQ has a parent + a test case; UNCOVERED_REQ deliberately none.
    links={
        CHILD_REQ_ID: [("satisfies", PARENT_REQ_ID), ("verifies", TESTCASE_ID)],
    },
    # (resource, field_id) -> enum option ids (+ which is the default).
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
    # Project-level enums (``/enumerations/~/{name}/~``) -- dict-shaped ``data``
    # with ``attributes.options[].id``, unlike getAvailableOptions' list.
    project_enums={
        "hyperlink-role": ["ref_int", "ref_ext"],
        "workitem-link-role": ["relates_to", "parent", "satisfies", "verifies"],
    },
)
