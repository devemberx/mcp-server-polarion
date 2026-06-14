"""In-process fake Polarion: mirrors the real project's *structure* with fully
synthetic *content* (no production data in eval logs). One catch-all respx
route on the Polarion host; other hosts (LLM provider) fall through
(``assert_all_mocked=False``). Mutations recorded, no side effects.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any

import httpx
import respx

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

# Reply comment id (parent == root "1") used by T1-REPLY-RESOLVE.
ROOT_COMMENT_ID = "1"
REPLY_COMMENT_ID = "2"

# Pre-existing hyperlink on the floating task; T1-HYPERLINK-PRESERVE asserts
# an update keeps it (Polarion REPLACES the whole list).
FLOATING_TASK_HYPERLINK_URI = "https://specs.example.com/fake-spec"

# Anchored intro paragraph in the doc body; T1-ROUNDTRIP-SOURCE edits it.
DOC_INTRO_PARAGRAPH_ID = "p-1"

# Second document + requirement traceability seeds (Tier-3 orchestration).
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

_TS = "2026-01-01T00:00:00.000Z"


@dataclass
class _WorkItem:
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


# Structure mirrored from MCP_Test_Project; every string is synthetic.
_WORK_ITEMS: dict[str, _WorkItem] = {
    DOC_HEADING_ID: _WorkItem(
        DOC_HEADING_ID, "Section A", "heading", module_id=MODULE_ID, outline_number="1"
    ),
    FLOATING_TASK_ID: _WorkItem(
        FLOATING_TASK_ID,
        "Floating task",
        "task",
        hyperlinks=[{"role": "ref_ext", "uri": FLOATING_TASK_HYPERLINK_URI}],
        custom_fields={"acceptance_criteria_id": "AC-1"},
    ),
    FLOATING_HEADING_ID: _WorkItem(FLOATING_HEADING_ID, "Floating heading", "heading"),
    FLOATING_GHOST_ID: _WorkItem(FLOATING_GHOST_ID, "Ghost type", "not_a_real_type"),
    CHILD_REQ_ID: _WorkItem(
        CHILD_REQ_ID, "Child requirement", "systemrequirement", module_id=MODULE_ID
    ),
    UNCOVERED_REQ_ID: _WorkItem(
        UNCOVERED_REQ_ID,
        "Uncovered requirement",
        "systemrequirement",
        module_id=MODULE_ID,
    ),
    PARENT_REQ_ID: _WorkItem(
        PARENT_REQ_ID,
        "Parent requirement",
        "systemrequirement",
        module_id=PARENT_MODULE_ID,
    ),
    TESTCASE_ID: _WorkItem(TESTCASE_ID, "Coverage test case", "systemtestcase"),
}

# Forward (outgoing) work-item links: source short id -> [(role, target short id)].
# CHILD_REQ has a parent + a test case; UNCOVERED_REQ deliberately has none.
_LINKS: dict[str, list[tuple[str, str]]] = {
    CHILD_REQ_ID: [("satisfies", PARENT_REQ_ID), ("verifies", TESTCASE_ID)],
}

# (resource, field_id) -> enum option ids (+ which is the default).
_ENUMS: dict[tuple[str, str], list[tuple[str, bool]]] = {
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
}

# Project-level enums (``/enumerations/~/{name}/~``) -- dict-shaped ``data``
# with ``attributes.options[].id``, unlike getAvailableOptions' list.
_PROJECT_ENUMS: dict[str, list[str]] = {
    "hyperlink-role": ["ref_int", "ref_ext"],
    "workitem-link-role": ["relates_to", "parent", "satisfies", "verifies"],
}


@dataclass
class FakePolarion:
    """Seeded, structure-faithful fake Polarion served over respx."""

    mutations: list[dict[str, Any]] = field(default_factory=list)

    def _work_item_resource(self, wi: _WorkItem) -> dict[str, Any]:
        relationships: dict[str, Any] = {
            "assignee": {"data": []},
            "author": {"data": {"type": "users", "id": f"{PROJECT}/{AUTHOR}"}},
        }
        if wi.module_id:
            relationships["module"] = {
                "data": {"type": "documents", "id": wi.module_id}
            }
        return {
            "type": "workitems",
            "id": f"{PROJECT}/{wi.short_id}",
            "attributes": {
                "title": wi.title,
                "type": wi.type,
                "status": wi.status,
                "priority": wi.priority,
                "severity": wi.severity,
                "resolution": "",
                "outlineNumber": wi.outline_number,
                "created": _TS,
                "updated": _TS,
                "description": {"type": "text/html", "value": ""},
                "hyperlinks": list(wi.hyperlinks),
                **wi.custom_fields,
            },
            "relationships": relationships,
        }

    def _document_resource(self, name: str) -> dict[str, Any]:
        if name == PARENT_DOC:
            title, body = "Fake Parent Doc", '<h1 id="ph-1">Fake Parent Doc</h1>'
        else:
            title = "Fake Doc"
            body = (
                '<h1 id="h-1">Fake Doc</h1>'
                f'<p id="{DOC_INTRO_PARAGRAPH_ID}">Fake intro paragraph.</p>'
            )
        return {
            "type": "documents",
            "id": f"{PROJECT}/{SPACE}/{name}",
            "attributes": {
                "title": title,
                "type": "systemRequirementSpecification",
                "status": "draft",
                "moduleName": name,
                "moduleFolder": SPACE,
                "homePageContent": {"type": "text/html", "value": body},
            },
        }

    def _document_parts_response(self) -> dict[str, Any]:
        """FakeDoc's parts: a Section A heading (the move anchor) + one work-item
        part, chained via ``nextPart``. ``include=workItem`` resources supply
        titles so ``read_document_parts`` returns populated ``items``.
        """
        base = f"{PROJECT}/{SPACE}/{DOC}"
        heading_part_id = f"{base}/{SECTION_A_PART_ID}"
        wi_part_id = f"{base}/workitem_{CHILD_REQ_ID}"
        data = [
            {
                "type": "document_parts",
                "id": heading_part_id,
                "attributes": {"type": "heading", "level": 1},
                "relationships": {
                    "workItem": {
                        "data": {
                            "type": "workitems",
                            "id": f"{PROJECT}/{DOC_HEADING_ID}",
                        }
                    },
                    "nextPart": {"data": {"type": "document_parts", "id": wi_part_id}},
                },
            },
            {
                "type": "document_parts",
                "id": wi_part_id,
                "attributes": {"type": "workitem"},
                "relationships": {
                    "workItem": {
                        "data": {"type": "workitems", "id": f"{PROJECT}/{CHILD_REQ_ID}"}
                    },
                },
            },
        ]
        included = [
            self._work_item_resource(_WORK_ITEMS[DOC_HEADING_ID]),
            self._work_item_resource(_WORK_ITEMS[CHILD_REQ_ID]),
        ]
        return {"data": data, "included": included, "meta": {"totalCount": len(data)}}

    def _linked_work_items_response(self, source_id: str) -> dict[str, Any]:
        """Forward links for ``source_id`` from ``_LINKS``; targets supplied as
        ``include=workItem`` resources (the parser derives targets from
        ``relationships.workItem``, never the composite id).
        """
        data: list[dict[str, Any]] = []
        included: list[dict[str, Any]] = []
        for role, target in _LINKS.get(source_id, []):
            target_full = f"{PROJECT}/{target}"
            data.append(
                {
                    "type": "linkedworkitems",
                    "id": f"{PROJECT}/{source_id}/{role}/{PROJECT}/{target}",
                    "attributes": {"role": role, "suspect": False},
                    "relationships": {
                        "workItem": {"data": {"type": "workitems", "id": target_full}}
                    },
                }
            )
            target_wi = _WORK_ITEMS.get(target)
            if target_wi is not None:
                included.append(self._work_item_resource(target_wi))
        return {"data": data, "included": included, "meta": {"totalCount": len(data)}}

    def _comment_resources(self) -> list[dict[str, Any]]:
        base = f"{PROJECT}/{SPACE}/{DOC}"
        return [
            {
                "type": "document_comments",
                "id": f"{base}/{ROOT_COMMENT_ID}",
                "attributes": {
                    "created": _TS,
                    "resolved": False,
                    "text": {"type": "text/html", "value": "fake root comment"},
                },
                "relationships": {
                    "author": {"data": {"id": f"{PROJECT}/{AUTHOR}"}},
                    "parentComment": {"data": None},
                    "childComments": {"data": [{"id": f"{base}/{REPLY_COMMENT_ID}"}]},
                },
            },
            {
                "type": "document_comments",
                "id": f"{base}/{REPLY_COMMENT_ID}",
                "attributes": {
                    "created": _TS,
                    "resolved": False,
                    "text": {"type": "text/html", "value": "fake reply comment"},
                },
                "relationships": {
                    "author": {"data": {"id": f"{PROJECT}/{AUTHOR}"}},
                    "parentComment": {"data": {"id": f"{base}/{ROOT_COMMENT_ID}"}},
                    "childComments": {"data": []},
                },
            },
        ]

    def _enum_response(self, resource: str, field_id: str) -> dict[str, Any]:
        options = _ENUMS.get((resource, field_id), [])
        data = [
            {
                "id": opt_id,
                "name": opt_id,
                "description": "",
                "default": is_default,
                "hidden": False,
                "terminal": False,
            }
            for opt_id, is_default in options
        ]
        return {"data": data, "meta": {"totalCount": len(data)}}

    def _dispatch(self, request: httpx.Request) -> httpx.Response:
        method = request.method
        path = request.url.path
        if path.startswith(API_PREFIX):
            path = path[len(API_PREFIX) :]

        if method in ("POST", "PATCH", "DELETE"):
            return self._handle_mutation(request, path)
        return self._handle_read(request, path)

    def _handle_read(self, request: httpx.Request, path: str) -> httpx.Response:
        params = request.url.params

        if path == "/projects":
            return httpx.Response(
                200,
                json={
                    "data": [
                        {
                            "type": "projects",
                            "id": PROJECT,
                            "attributes": {"name": "Fake Project", "active": True},
                        }
                    ],
                    "meta": {"totalCount": 1},
                },
            )

        enum = re.search(
            r"/(workitems|documents)/fields/([^/]+)/actions/getAvailableOptions$",
            path,
        )
        if enum:
            return httpx.Response(
                200, json=self._enum_response(enum.group(1), enum.group(2))
            )

        project_enum = re.search(r"/enumerations/~/([^/]+)/~$", path)
        if project_enum:
            name = project_enum.group(1)
            options = _PROJECT_ENUMS.get(name)
            if options is None:
                return httpx.Response(404, json={"errors": [{"status": "404"}]})
            return httpx.Response(
                200,
                json={
                    "data": {
                        "type": "enumerations",
                        "id": f"~/{name}/~",
                        "attributes": {"options": [{"id": o} for o in options]},
                    }
                },
            )

        single_wi = re.search(r"/workitems/([^/]+)$", path)
        if single_wi and "/fields/" not in path:
            wi = _WORK_ITEMS.get(single_wi.group(1))
            if wi is None:
                return httpx.Response(404, json={"errors": [{"status": "404"}]})
            return httpx.Response(200, json={"data": self._work_item_resource(wi)})

        # Forward links from a single source work item (empty if none seeded).
        linked = re.search(r"/workitems/([^/]+)/linkedworkitems$", path)
        if linked:
            return httpx.Response(
                200, json=self._linked_work_items_response(linked.group(1))
            )

        # Work item list / discovery: query=type:heading narrows to headings;
        # query=linkedWorkItems:{wi} is the back-link fallback (sources -> target).
        if path.endswith("/workitems"):
            query = params.get("query", "")
            if query == "type:heading":
                items = [w for w in _WORK_ITEMS.values() if w.type == "heading"]
            elif query.startswith("linkedWorkItems:"):
                target = query.split(":", 1)[1].strip().rsplit("/", maxsplit=1)[-1]
                items = [
                    w
                    for w in _WORK_ITEMS.values()
                    if any(t == target for _, t in _LINKS.get(w.short_id, []))
                ]
            else:
                items = list(_WORK_ITEMS.values())
            data = [self._work_item_resource(w) for w in items]
            return httpx.Response(
                200, json={"data": data, "meta": {"totalCount": len(data)}}
            )

        # Parts seeded only for FakeDoc (Section A anchor); others stay empty.
        parts = re.search(r"/documents/([^/]+)/parts$", path)
        if parts:
            if parts.group(1) == DOC:
                return httpx.Response(200, json=self._document_parts_response())
            return httpx.Response(200, json={"data": [], "meta": {"totalCount": 0}})

        if path.endswith("/comments"):
            data = self._comment_resources()
            return httpx.Response(
                200, json={"data": data, "meta": {"totalCount": len(data)}}
            )

        # Exact match on the two seeded docs: a broad "/documents/" would claim
        # every name as existing, masking bugs in cases probing alternate names.
        doc_match = re.search(rf"/spaces/{SPACE}/documents/([^/]+)$", path)
        if doc_match and doc_match.group(1) in (DOC, PARENT_DOC):
            return httpx.Response(
                200, json={"data": self._document_resource(doc_match.group(1))}
            )

        return httpx.Response(404, json={"errors": [{"status": "404", "path": path}]})

    def _handle_mutation(self, request: httpx.Request, path: str) -> httpx.Response:
        body: Any = None
        if request.content:
            try:
                body = json.loads(request.content)
            except json.JSONDecodeError:
                body = None
        self.mutations.append({"method": request.method, "path": path, "json": body})

        # Resource-creating POSTs must echo one id per submitted entry (the
        # tool layer raises on a count mismatch, so bulk cases need N ids);
        # action POSTs and PATCH / DELETE fall through to 204.
        submitted = 1
        if isinstance(body, dict):
            data = body.get("data")
            if isinstance(data, list) and data:
                submitted = len(data)
        if request.method == "POST":
            if path.endswith("/workitems"):
                return httpx.Response(
                    201,
                    json={
                        "data": [
                            {"type": "workitems", "id": f"{PROJECT}/MCPT-{9001 + i}"}
                            for i in range(submitted)
                        ]
                    },
                )
            if path.endswith("/documents"):
                return httpx.Response(
                    201,
                    json={"data": [{"type": "documents", "id": MODULE_ID}]},
                )
            if path.endswith("/comments"):
                return httpx.Response(
                    201,
                    json={
                        "data": [
                            {
                                "type": "document_comments",
                                "id": f"{PROJECT}/{SPACE}/{DOC}/99",
                            }
                        ]
                    },
                )
            if path.endswith("/linkedworkitems"):
                return httpx.Response(
                    201,
                    json={
                        "data": [
                            {
                                "type": "linkedworkitems",
                                "id": f"{PROJECT}/MCPT-{9001 + i}",
                            }
                            for i in range(submitted)
                        ]
                    },
                )
        return httpx.Response(204)

    def install(self, router: respx.MockRouter) -> None:
        """Register the catch-all Polarion route on *router*."""
        router.route(url__regex=rf"{re.escape(POLARION_HOST)}/.*").mock(
            side_effect=self._dispatch
        )
