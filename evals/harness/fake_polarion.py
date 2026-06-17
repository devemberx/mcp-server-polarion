"""In-process fake Polarion: real project *structure*, synthetic *content* (no
production data in eval logs). One catch-all respx route on the Polarion host;
other hosts (LLM provider) fall through (``assert_all_mocked=False``). Mutations
recorded, no side effects. Seed data lives in ``fixtures``; ``seeds`` is
injectable for per-case alternates without touching the global.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any

import httpx
import respx

from .fixtures import (
    API_PREFIX,
    AUTHOR,
    DOC,
    MODULE_ID,
    POLARION_HOST,
    PROJECT,
    SEEDS,
    SPACE,
    TS,
    Seeds,
    WorkItem,
)


@dataclass
class FakePolarion:
    """Seeded, structure-faithful fake Polarion served over respx."""

    seeds: Seeds = SEEDS
    mutations: list[dict[str, Any]] = field(default_factory=list)

    def _work_item_resource(self, wi: WorkItem) -> dict[str, Any]:
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
                "created": TS,
                "updated": TS,
                "description": {"type": "text/html", "value": ""},
                "hyperlinks": list(wi.hyperlinks),
                **wi.custom_fields,
            },
            "relationships": relationships,
        }

    def _document_resource(self, name: str) -> dict[str, Any]:
        # Direct index, not .get: only reached once dispatch confirms name seeded.
        doc = self.seeds.documents[name]
        return {
            "type": "documents",
            "id": f"{PROJECT}/{SPACE}/{name}",
            "attributes": {
                "title": doc.title,
                "type": doc.type,
                "status": doc.status,
                "moduleName": name,
                "moduleFolder": SPACE,
                "homePageContent": {"type": "text/html", "value": doc.body_html},
            },
        }

    def _document_parts_response(self, name: str) -> dict[str, Any]:
        """A document's ``parts`` from its seed: each part chained to the next via
        ``nextPart``; ``include=workItem`` resources supply titles so
        ``read_document_parts`` returns populated ``items``. Empty for docs with
        no seeded parts.
        """
        doc = self.seeds.documents.get(name)
        parts = doc.parts if doc else []
        base = f"{PROJECT}/{SPACE}/{name}"
        data: list[dict[str, Any]] = []
        included: list[dict[str, Any]] = []
        for i, part in enumerate(parts):
            relationships: dict[str, Any] = {
                "workItem": {
                    "data": {
                        "type": "workitems",
                        "id": f"{PROJECT}/{part.work_item_id}",
                    }
                }
            }
            if i + 1 < len(parts):
                nxt = parts[i + 1]
                next_id = f"{base}/{nxt.kind}_{nxt.work_item_id}"
                relationships["nextPart"] = {
                    "data": {"type": "document_parts", "id": next_id}
                }
            if part.kind == "heading":
                attributes = {"type": "heading", "level": part.level}
            else:
                attributes = {"type": part.kind}
            data.append(
                {
                    "type": "document_parts",
                    "id": f"{base}/{part.kind}_{part.work_item_id}",
                    "attributes": attributes,
                    "relationships": relationships,
                }
            )
            included.append(
                self._work_item_resource(self.seeds.work_items[part.work_item_id])
            )
        return {"data": data, "included": included, "meta": {"totalCount": len(data)}}

    def _linked_work_items_response(self, source_id: str) -> dict[str, Any]:
        """Forward links for ``source_id`` from ``seeds.links``; targets supplied
        as ``include=workItem`` resources (the parser derives targets from
        ``relationships.workItem``, never the composite id).
        """
        data: list[dict[str, Any]] = []
        included: list[dict[str, Any]] = []
        for role, target in self.seeds.links.get(source_id, []):
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
            target_wi = self.seeds.work_items.get(target)
            if target_wi is not None:
                included.append(self._work_item_resource(target_wi))
        return {"data": data, "included": included, "meta": {"totalCount": len(data)}}

    def _comment_resources(self, name: str) -> list[dict[str, Any]]:
        """A document's comment thread from its seed; child links derived from
        ``parent_id`` (no redundant child-id lists in the seed).
        """
        doc = self.seeds.documents.get(name)
        comments = doc.comments if doc else []
        base = f"{PROJECT}/{SPACE}/{name}"
        resources: list[dict[str, Any]] = []
        for comment in comments:
            children = [
                {"id": f"{base}/{c.comment_id}"}
                for c in comments
                if c.parent_id == comment.comment_id
            ]
            parent = (
                {"data": {"id": f"{base}/{comment.parent_id}"}}
                if comment.parent_id
                else {"data": None}
            )
            resources.append(
                {
                    "type": "document_comments",
                    "id": f"{base}/{comment.comment_id}",
                    "attributes": {
                        "created": TS,
                        "resolved": comment.resolved,
                        "text": {"type": "text/html", "value": comment.text},
                    },
                    "relationships": {
                        "author": {"data": {"id": f"{PROJECT}/{AUTHOR}"}},
                        "parentComment": parent,
                        "childComments": {"data": children},
                    },
                }
            )
        return resources

    def _enum_response(self, resource: str, field_id: str) -> dict[str, Any]:
        options = self.seeds.enums.get((resource, field_id), [])
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
            options = self.seeds.project_enums.get(name)
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
            wi = self.seeds.work_items.get(single_wi.group(1))
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
                items = [
                    w for w in self.seeds.work_items.values() if w.type == "heading"
                ]
            elif query.startswith("linkedWorkItems:"):
                target = query.split(":", 1)[1].strip().rsplit("/", maxsplit=1)[-1]
                items = [
                    w
                    for w in self.seeds.work_items.values()
                    if any(t == target for _, t in self.seeds.links.get(w.short_id, []))
                ]
            else:
                items = list(self.seeds.work_items.values())
            data = [self._work_item_resource(w) for w in items]
            return httpx.Response(
                200, json={"data": data, "meta": {"totalCount": len(data)}}
            )

        # Parts derive from the document's seed; unseeded/absent docs stay empty.
        parts = re.search(r"/documents/([^/]+)/parts$", path)
        if parts:
            return httpx.Response(
                200, json=self._document_parts_response(parts.group(1))
            )

        comments = re.search(r"/documents/([^/]+)/comments$", path)
        if comments:
            data = self._comment_resources(comments.group(1))
            return httpx.Response(
                200, json={"data": data, "meta": {"totalCount": len(data)}}
            )

        # Exact match on a seeded doc: a broad "/documents/" would claim every
        # name as existing, masking bugs in cases probing other names.
        doc_match = re.search(rf"/spaces/{SPACE}/documents/([^/]+)$", path)
        if doc_match and doc_match.group(1) in self.seeds.documents:
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
            # moveFromDocument 400s on a free-floating item; 204 only when in a doc.
            if path.endswith("/actions/moveFromDocument"):
                m = re.search(r"/workitems/([^/]+)/actions/moveFromDocument$", path)
                wi = self.seeds.work_items.get(m.group(1)) if m else None
                if wi is None or not wi.module_id:
                    return httpx.Response(
                        400,
                        json={
                            "errors": [
                                {
                                    "status": "400",
                                    "title": "Bad Request",
                                    "detail": "Work Item is not in Document.",
                                }
                            ]
                        },
                    )
                return httpx.Response(204)
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
