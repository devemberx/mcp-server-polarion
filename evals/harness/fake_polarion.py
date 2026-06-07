"""In-process fake Polarion backend for the eval harness.

Mirrors the *structure* of the real ``MCP_Test_Project`` (id shapes, space
layout, enum id sets, comment-thread links, work-item field set) but the
*content* is entirely synthetic — no real titles, bodies, comment text or
author ids — so no production data can leak into eval logs.

Installed as a single catch-all respx route scoped to the Polarion host.
Requests to any other host (the LLM provider) fall through to the network
because the router is created with ``assert_all_mocked=False``. Every
mutating request (POST / PATCH / DELETE) is recorded but has no side effect.
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

_TS = "2026-01-01T00:00:00.000Z"


@dataclass
class _WorkItem:
    short_id: str
    title: str
    type: str
    status: str = "open"
    priority: str = "50.0"
    severity: str = "should_have"
    module: bool = False
    outline_number: str = ""
    # Keys MUST stay outside ``STANDARD_WORK_ITEM_ATTRIBUTES`` so the merge
    # into the resource attributes dict doesn't shadow real attributes.
    custom_fields: dict[str, str] = field(default_factory=dict)


# Structure mirrored from MCP_Test_Project; every string is synthetic.
_WORK_ITEMS: dict[str, _WorkItem] = {
    DOC_HEADING_ID: _WorkItem(
        DOC_HEADING_ID, "Section A", "heading", module=True, outline_number="1"
    ),
    FLOATING_TASK_ID: _WorkItem(
        FLOATING_TASK_ID,
        "Floating task",
        "task",
        custom_fields={"acceptance_criteria_id": "AC-1"},
    ),
    FLOATING_HEADING_ID: _WorkItem(FLOATING_HEADING_ID, "Floating heading", "heading"),
    FLOATING_GHOST_ID: _WorkItem(FLOATING_GHOST_ID, "Ghost type", "not_a_real_type"),
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


@dataclass
class FakePolarion:
    """Seeded, structure-faithful fake Polarion served over respx."""

    mutations: list[dict[str, Any]] = field(default_factory=list)

    def _work_item_resource(self, wi: _WorkItem) -> dict[str, Any]:
        relationships: dict[str, Any] = {
            "assignee": {"data": []},
            "author": {"data": {"type": "users", "id": f"{PROJECT}/{AUTHOR}"}},
        }
        if wi.module:
            relationships["module"] = {"data": {"type": "documents", "id": MODULE_ID}}
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
                "hyperlinks": [],
                **wi.custom_fields,
            },
            "relationships": relationships,
        }

    def _document_resource(self) -> dict[str, Any]:
        return {
            "type": "documents",
            "id": MODULE_ID,
            "attributes": {
                "title": "Fake Doc",
                "type": "systemRequirementSpecification",
                "status": "draft",
                "moduleName": DOC,
                "moduleFolder": SPACE,
                "homePageContent": {
                    "type": "text/html",
                    "value": '<h1 id="h-1">Fake Doc</h1>',
                },
            },
        }

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

        single_wi = re.search(r"/workitems/([^/]+)$", path)
        if single_wi and "/fields/" not in path:
            wi = _WORK_ITEMS.get(single_wi.group(1))
            if wi is None:
                return httpx.Response(404, json={"errors": [{"status": "404"}]})
            return httpx.Response(200, json={"data": self._work_item_resource(wi)})

        # Always empty: no traceability seeded.
        if path.endswith("/linkedworkitems"):
            return httpx.Response(200, json={"data": [], "meta": {"totalCount": 0}})

        # Work item list / discovery (query=type:heading narrows to headings).
        if path.endswith("/workitems"):
            query = params.get("query", "")
            if query == "type:heading":
                items = [w for w in _WORK_ITEMS.values() if w.type == "heading"]
            else:
                items = list(_WORK_ITEMS.values())
            data = [self._work_item_resource(w) for w in items]
            return httpx.Response(
                200, json={"data": data, "meta": {"totalCount": len(data)}}
            )

        # Empty: no case depends on part structure.
        if path.endswith("/parts"):
            return httpx.Response(200, json={"data": [], "meta": {"totalCount": 0}})

        if path.endswith("/comments"):
            data = self._comment_resources()
            return httpx.Response(
                200, json={"data": data, "meta": {"totalCount": len(data)}}
            )

        # Exact match on FakeDoc: a broad "/documents/" would claim every name
        # as existing, masking bugs in cases probing alternate names.
        if path.endswith(f"/spaces/{SPACE}/documents/{DOC}"):
            return httpx.Response(200, json={"data": self._document_resource()})

        return httpx.Response(404, json={"errors": [{"status": "404", "path": path}]})

    def _handle_mutation(self, request: httpx.Request, path: str) -> httpx.Response:
        body: Any = None
        if request.content:
            try:
                body = json.loads(request.content)
            except json.JSONDecodeError:
                body = None
        self.mutations.append({"method": request.method, "path": path, "json": body})

        # Resource-creating POSTs must echo an id (tool layer requires it);
        # action POSTs and PATCH / DELETE fall through to 204.
        if request.method == "POST":
            if path.endswith("/workitems"):
                return httpx.Response(
                    201,
                    json={
                        "data": [{"type": "workitems", "id": f"{PROJECT}/MCPT-9001"}]
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
                            {"type": "linkedworkitems", "id": f"{PROJECT}/MCPT-9001"}
                        ]
                    },
                )
        return httpx.Response(204)

    def install(self, router: respx.MockRouter) -> None:
        """Register the catch-all Polarion route on *router*."""
        router.route(url__regex=rf"{re.escape(POLARION_HOST)}/.*").mock(
            side_effect=self._dispatch
        )
