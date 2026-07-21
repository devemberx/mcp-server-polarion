"""In-process fake Polarion: real project *structure*, synthetic *content* (no
production data in eval logs). One catch-all respx route on Polarion host;
other hosts (LLM provider) fall through (``assert_all_mocked=False``).
Mutations recorded, no side effects. Seed data live in ``fixtures``; ``seeds``
injectable for per-case alternates without touching global.
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
    DOC_ATTACHMENT_CONTENT,
    MODULE_ID,
    POLARION_HOST,
    PROJECT,
    SEEDS,
    SPACE,
    TEST_RUN_ID,
    TESTCASE_ID,
    TS,
    Attachment,
    Comment,
    Seeds,
    TestRun,
    WorkItem,
)


def _error_response(status: int, detail: str) -> httpx.Response:
    """JSON:API error body, live wire shape."""
    return httpx.Response(
        status,
        json={"errors": [{"status": str(status), "detail": detail}]},
    )


@dataclass
class FakePolarion:
    """Seeded, structure-faithful fake Polarion served over respx."""

    seeds: Seeds = SEEDS
    mutations: list[dict[str, Any]] = field(default_factory=list)
    # Work item attachments created via POST -- kept off Seeds (frozen,
    # shared module singleton) so uploads don't leak into other tests'
    # default-seeded FakePolarion instances. Keyed by work item short id.
    created_wi_attachments: dict[str, list[Attachment]] = field(default_factory=dict)

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

    def _test_run_resource(self, tr: TestRun) -> dict[str, Any]:
        return {
            "type": "testruns",
            "id": f"{PROJECT}/{tr.short_id}",
            "attributes": {
                "title": tr.title,
                "type": tr.type,
                "status": tr.status,
                "finishedOn": tr.finished_on,
                "updated": TS,
                "isTemplate": tr.is_template,
            },
            "relationships": {
                "author": {"data": {"type": "users", "id": f"{PROJECT}/{AUTHOR}"}},
            },
        }

    def _test_record_resource(self, tr: TestRun, iteration: int = 0) -> dict[str, Any]:
        # Failed TESTCASE_ID executions, one per iteration -- enough for
        # trigger + result-filter + bulk-update behavior.
        return {
            "type": "testrecords",
            "id": f"{PROJECT}/{tr.short_id}/{PROJECT}/{TESTCASE_ID}/{iteration}",
            "attributes": {
                "executed": TS,
                "duration": 1.5,
                "result": "failed",
                "iteration": iteration,
            },
            "relationships": {
                "testCase": {
                    "data": {"type": "workitems", "id": f"{PROJECT}/{TESTCASE_ID}"}
                },
                "executedBy": {"data": {"type": "users", "id": f"{PROJECT}/{AUTHOR}"}},
            },
        }

    def _test_record_detail_resource(
        self, tr: TestRun, iteration: int
    ) -> dict[str, Any]:
        # Single-GET add comment + testCaseRevision beyond list shape.
        resource = self._test_record_resource(tr, iteration)
        resource["attributes"]["comment"] = {
            "type": "text/html",
            "value": "<p>Fake execution comment.</p>",
        }
        resource["attributes"]["testCaseRevision"] = "3"
        return resource

    def _document_resource(self, name: str) -> dict[str, Any]:
        # Direct index, not .get: only reached once dispatch confirm name seeded.
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

    def _discovery_document_resource(self, name: str) -> dict[str, Any]:
        """Module-form ``documents`` resource for list_documents discovery scan
        (id = full module id; ``_discover_documents`` split it for space/name).
        """
        doc = self.seeds.documents[name]
        author_ref = {"data": {"type": "users", "id": f"{PROJECT}/{AUTHOR}"}}
        return {
            "type": "documents",
            "id": f"{PROJECT}/{SPACE}/{name}",
            "attributes": {"type": doc.type, "status": doc.status, "updated": TS},
            "relationships": {"author": author_ref, "updatedBy": author_ref},
        }

    def _document_discovery_response(self) -> dict[str, Any]:
        """list_documents scan: one heading per document carrying its
        ``module``, module documents in ``included``. Only docs with seeded
        heading surface (mirror production GROUP-BY-heading discovery).
        """
        headings = [
            wi
            for wi in self.seeds.work_items.values()
            if wi.type == "heading" and wi.module_id
        ]
        data = [self._work_item_resource(wi) for wi in headings]
        names: list[str] = []
        for wi in headings:
            name = wi.module_id.rsplit("/", maxsplit=1)[-1]
            if name in self.seeds.documents and name not in names:
                names.append(name)
        included = [self._discovery_document_resource(n) for n in names]
        return {"data": data, "included": included, "meta": {"totalCount": len(data)}}

    def _document_parts_response(self, name: str) -> dict[str, Any]:
        """Parts chained via ``nextPart``; ``include=workItem`` resources
        supply titles so ``read_document_parts`` return populated ``items``.
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
        """Targets ship as ``include=workItem`` resources — parser derive
        targets from ``relationships.workItem``, never composite id.
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

    def _comment_resources(
        self, comments: list[Comment], base: str, comment_type: str
    ) -> list[dict[str, Any]]:
        """Shared by document (4-segment ``base``) + work-item (3-segment)
        comments. Children derive from ``parent_id``. ``title`` only when
        set -- document comments leave it absent.
        """
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
            attributes: dict[str, Any] = {
                "created": TS,
                "resolved": comment.resolved,
                "text": {"type": "text/html", "value": comment.text},
            }
            if comment.title:
                attributes["title"] = comment.title
            resources.append(
                {
                    "type": comment_type,
                    "id": f"{base}/{comment.comment_id}",
                    "attributes": attributes,
                    "relationships": {
                        "author": {"data": {"id": f"{PROJECT}/{AUTHOR}"}},
                        "parentComment": parent,
                        "childComments": {"data": children},
                    },
                }
            )
        return resources

    def _attachment_resources(
        self, attachments: list[Attachment], base: str, resource_type: str
    ) -> list[dict[str, Any]]:
        """``attributes.id`` = bare token body HTML reference; resource id
        prefix it with base (4-segment document or 3-segment work item).
        Polarion serve no ``created`` and no mime type here.
        """
        return [
            {
                "type": resource_type,
                "id": f"{base}/{attachment.attachment_id}",
                "attributes": {
                    "id": attachment.attachment_id,
                    "fileName": attachment.attachment_id,
                    "title": attachment.title,
                    "updated": TS,
                    "length": attachment.length,
                },
                # Author only: sparse fieldset drop project rel (verified 2026-07-18).
                "relationships": {
                    "author": {"data": {"id": f"{PROJECT}/{AUTHOR}"}},
                },
            }
            for attachment in attachments
        ]

    def _author_included(self) -> list[dict[str, Any]]:
        """``included`` users entry resolve shared author id to name;
        production request ``include=author&fields[users]=name`` on these read.
        """
        return [
            {
                "type": "users",
                "id": f"{PROJECT}/{AUTHOR}",
                "attributes": {"name": "Fake Author"},
            }
        ]

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

        # Context-qualified names ("testing/testrun-type") = separate seed
        # keys, so wildcard-context probes for them 404 like live Polarion.
        project_enum = re.search(r"/enumerations/([^/]+)/([^/]+)/~$", path)
        if project_enum:
            context, name = project_enum.groups()
            key = name if context == "~" else f"{context}/{name}"
            options = self.seeds.project_enums.get(key)
            if options is None:
                return httpx.Response(404, json={"errors": [{"status": "404"}]})
            return httpx.Response(
                200,
                json={
                    "data": {
                        "type": "enumerations",
                        "id": f"{context}/{name}/~",
                        "attributes": {"options": [{"id": o} for o in options]},
                    }
                },
            )

        single_wi = re.search(r"/workitems/([^/]+)$", path)
        if single_wi and "/fields/" not in path:
            wi = self.seeds.work_items.get(single_wi.group(1))
            if wi is None:
                return httpx.Response(404, json={"errors": [{"status": "404"}]})
            return httpx.Response(
                200,
                json={
                    "data": self._work_item_resource(wi),
                    "included": self._author_included(),
                },
            )

        linked = re.search(r"/workitems/([^/]+)/linkedworkitems$", path)
        if linked:
            return httpx.Response(
                200, json=self._linked_work_items_response(linked.group(1))
            )

        wi_comments = re.search(r"/workitems/([^/]+)/comments$", path)
        if wi_comments:
            wi = self.seeds.work_items.get(wi_comments.group(1))
            data = self._comment_resources(
                wi.comments if wi else [],
                f"{PROJECT}/{wi_comments.group(1)}",
                "workitem_comments",
            )
            return httpx.Response(
                200,
                json={
                    "data": data,
                    "included": self._author_included() if data else [],
                    "meta": {"totalCount": len(data)},
                },
            )

        wi_attachments = re.search(r"/workitems/([^/]+)/attachments$", path)
        if wi_attachments:
            wi_id = wi_attachments.group(1)
            wi = self.seeds.work_items.get(wi_id)
            if wi is None:
                return httpx.Response(404, json={"errors": [{"status": "404"}]})
            attachments = [
                *wi.attachments,
                *self.created_wi_attachments.get(wi_id, []),
            ]
            data = self._attachment_resources(
                attachments,
                f"{PROJECT}/{wi_id}",
                "workitem_attachments",
            )
            return httpx.Response(
                200,
                json={
                    "data": data,
                    "included": self._author_included() if data else [],
                    # Live: totalCount serve every multi-page page; fake
                    # always emit it -- diverges from doc route's omit.
                    "meta": {"totalCount": len(data)},
                },
            )

        # query=linkedWorkItems:{wi} = back-link fallback (sources -> target).
        if path.endswith("/workitems"):
            # fields[documents] param = list_documents discovery scan, not plain list.
            if params.get("fields[documents]"):
                return httpx.Response(200, json=self._document_discovery_response())
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

        # List route regex end at /testrecords$ — never claim this longer path.
        single_record = re.search(
            r"/testruns/([^/]+)/testrecords/([^/]+)/([^/]+)/(\d+)$", path
        )
        if single_record:
            run_id, case_project, case_id, iteration = single_record.groups()
            tr = self.seeds.test_runs.get(run_id)
            if (
                tr is None
                or tr.is_template
                or case_project != PROJECT
                or case_id != TESTCASE_ID
                # Same range as list route -- seeds serve tr.iterations records.
                or int(iteration) >= tr.iterations
            ):
                return httpx.Response(404, json={"errors": [{"status": "404"}]})
            return httpx.Response(
                200,
                json={
                    "data": self._test_record_detail_resource(tr, int(iteration)),
                    "included": self._author_included(),
                },
            )

        # testResultId filter server-side. No meta block -- live endpoint
        # omit totalCount (verified 2026-07-12).
        records = re.search(r"/testruns/([^/]+)/testrecords$", path)
        if records:
            tr = self.seeds.test_runs.get(records.group(1))
            if tr is None:
                return httpx.Response(404, json={"errors": [{"status": "404"}]})
            # Blueprints never executed -- empty page for templates.
            data = (
                []
                if tr.is_template
                else [self._test_record_resource(tr, i) for i in range(tr.iterations)]
            )
            wanted = params.get("testResultId", "")
            if wanted:
                data = [r for r in data if r["attributes"]["result"] == wanted]
            return httpx.Response(
                200,
                json={
                    "data": data,
                    "included": self._author_included() if data else [],
                },
            )

        # isTemplate served only on templates — mirror live omission on instances.
        single_tr = re.search(r"/testruns/([^/]+)$", path)
        if single_tr:
            tr = self.seeds.test_runs.get(single_tr.group(1))
            if tr is None:
                return httpx.Response(404, json={"errors": [{"status": "404"}]})
            resource = self._test_run_resource(tr)
            attributes = resource["attributes"]
            attributes["id"] = tr.short_id
            if not tr.is_template:
                del attributes["isTemplate"]
            return httpx.Response(
                200,
                json={"data": resource, "included": self._author_included()},
            )

        if path.endswith("/testruns"):
            want_templates = params.get("templates", "").lower() == "true"
            runs = [
                tr
                for tr in self.seeds.test_runs.values()
                if tr.is_template == want_templates
            ]
            data = [self._test_run_resource(tr) for tr in runs]
            included = self._author_included() if data else []
            return httpx.Response(
                200,
                json={
                    "data": data,
                    "included": included,
                    "meta": {"totalCount": len(data)},
                },
            )

        parts = re.search(r"/documents/([^/]+)/parts$", path)
        if parts:
            return httpx.Response(
                200, json=self._document_parts_response(parts.group(1))
            )

        # Doc sub-resource routes: space-scoped, unseeded 404, no meta -- live
        # emit totalCount only on overshoot; fake never overshoot (verified 2026-07-18).
        doc_comments = re.search(rf"/spaces/{SPACE}/documents/([^/]+)/comments$", path)
        if doc_comments:
            name = doc_comments.group(1)
            doc = self.seeds.documents.get(name)
            if doc is None:
                return httpx.Response(404, json={"errors": [{"status": "404"}]})
            data = self._comment_resources(
                doc.comments,
                f"{PROJECT}/{SPACE}/{name}",
                "document_comments",
            )
            return httpx.Response(
                200,
                json={
                    "data": data,
                    "included": self._author_included() if data else [],
                },
            )

        doc_attachments = re.search(
            rf"/spaces/{SPACE}/documents/([^/]+)/attachments$", path
        )
        if doc_attachments:
            name = doc_attachments.group(1)
            doc = self.seeds.documents.get(name)
            if doc is None:
                return httpx.Response(404, json={"errors": [{"status": "404"}]})
            data = self._attachment_resources(
                doc.attachments, f"{PROJECT}/{SPACE}/{name}", "document_attachments"
            )
            return httpx.Response(
                200,
                json={
                    "data": data,
                    "included": self._author_included() if data else [],
                },
            )

        # Attachment content: 404 unseeded (#194 principle), NOT list route's
        # empty-200. Real bytes so decode/size-cap tests work; 406 without
        # octet-stream Accept mirror vendor. Space anchor: wrong space 404
        # live-verified, unanchored regex mask space arg-threading bugs.
        attachment_content = re.search(
            rf"/spaces/{SPACE}/documents/([^/]+)/attachments/([^/]+)/content$", path
        )
        if attachment_content:
            name, attachment_id = attachment_content.groups()
            if "application/octet-stream" not in request.headers.get("accept", ""):
                return httpx.Response(406, json={"errors": [{"status": "406"}]})
            doc = self.seeds.documents.get(name)
            known_ids = {a.attachment_id for a in doc.attachments} if doc else set()
            if doc is None or attachment_id not in known_ids:
                return httpx.Response(404, json={"errors": [{"status": "404"}]})
            return httpx.Response(
                200,
                content=DOC_ATTACHMENT_CONTENT,
                headers={"Content-Type": "application/octet-stream"},
            )

        # Exact match on seeded doc: broad "/documents/" would claim every
        # name exist, masking bugs in cases probing other names.
        doc_match = re.search(rf"/spaces/{SPACE}/documents/([^/]+)$", path)
        if doc_match and doc_match.group(1) in self.seeds.documents:
            return httpx.Response(
                200, json={"data": self._document_resource(doc_match.group(1))}
            )

        return httpx.Response(404, json={"errors": [{"status": "404", "path": path}]})

    def _handle_mutation(self, request: httpx.Request, path: str) -> httpx.Response:
        # Multipart request content = lazy stream; materialize before access.
        request.read()
        body: Any = None
        if request.content:
            try:
                body = json.loads(request.content)
            except json.JSONDecodeError:
                body = None
        self.mutations.append({"method": request.method, "path": path, "json": body})

        doc_attachments = re.search(
            rf"/spaces/{SPACE}/documents/([^/]+)/attachments$", path
        )
        if doc_attachments and request.method == "POST":
            return self._post_document_attachments(request, doc_attachments.group(1))

        wi_attachments = re.search(r"/workitems/([^/]+)/attachments$", path)
        if wi_attachments and request.method == "POST":
            return self._post_work_item_attachments(request, wi_attachments.group(1))

        # Resource-creating POSTs must echo one id per submitted entry (tool
        # layer raise on count mismatch, so bulk cases need N ids); action
        # POSTs + PATCH/DELETE fall through to 204.
        submitted = 1
        if isinstance(body, dict):
            data = body.get("data")
            if isinstance(data, list) and data:
                submitted = len(data)
        if request.method == "POST":
            # moveFromDocument 400 on free-floating item; 204 only when in doc.
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
            if path.endswith("/actions/copy"):
                # Copy action 201: data = single dict (not list), sparse body.
                target = (
                    str(body.get("targetDocumentName") or "")
                    if isinstance(body, dict)
                    else ""
                )
                return httpx.Response(
                    201,
                    json={
                        "data": {
                            "type": "documents",
                            "id": f"{PROJECT}/{SPACE}/{target or DOC + 'Copy'}",
                            "attributes": {"status": "draft"},
                        }
                    },
                )
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
            if path.endswith("/testrecords"):
                # Live-verified: server compose 5-segment id run/testCase/iter;
                # unknown testCase 400, no client-set record id. Iteration 0
                # enough — tool reject batch duplicate testCase client-side.
                run_match = re.search(r"/testruns/([^/]+)/testrecords$", path)
                run_id = run_match.group(1) if run_match else TEST_RUN_ID
                record_ids: list[str] = []
                entries = (
                    body["data"]
                    if isinstance(body, dict) and isinstance(body.get("data"), list)
                    else []
                )
                for entry in entries:
                    rels = (
                        entry.get("relationships") if isinstance(entry, dict) else None
                    )
                    tc_rel = rels.get("testCase") if isinstance(rels, dict) else None
                    tc_data = tc_rel.get("data") if isinstance(tc_rel, dict) else None
                    tc_id = (
                        str(tc_data.get("id") or "")
                        if isinstance(tc_data, dict)
                        else ""
                    )
                    tc_short = tc_id.rsplit("/", 1)[-1]
                    if tc_short not in self.seeds.work_items:
                        return httpx.Response(
                            400,
                            json={
                                "errors": [
                                    {
                                        "status": "400",
                                        "title": "Bad Request",
                                        "detail": "Test Case is missing, or the "
                                        "one specified is invalid.",
                                    }
                                ]
                            },
                        )
                    record_ids.append(f"{PROJECT}/{run_id}/{tc_id}/0")
                return httpx.Response(
                    201,
                    json={
                        "data": [
                            {"type": "testrecords", "id": rid} for rid in record_ids
                        ]
                    },
                )
            if path.endswith("/testruns"):
                # Polarion honor client-supplied id verbatim; echo it back.
                ids: list[str] = []
                if isinstance(body, dict) and isinstance(body.get("data"), list):
                    for i, entry in enumerate(body["data"]):
                        attrs = (
                            entry.get("attributes") if isinstance(entry, dict) else None
                        )
                        rid = attrs.get("id") if isinstance(attrs, dict) else None
                        ids.append(str(rid) if rid else f"TR-{9001 + i}")
                else:
                    ids = ["TR-9001"]
                return httpx.Response(
                    201,
                    json={
                        "data": [
                            {"type": "testruns", "id": f"{PROJECT}/{rid}"}
                            for rid in ids
                        ]
                    },
                )
            if path.endswith("/documents"):
                return httpx.Response(
                    201,
                    json={"data": [{"type": "documents", "id": MODULE_ID}]},
                )
            if path.endswith("/comments"):
                wi_post = re.search(r"/workitems/([^/]+)/comments$", path)
                if wi_post:
                    created = {
                        "type": "workitem_comments",
                        "id": f"{PROJECT}/{wi_post.group(1)}/99",
                    }
                else:
                    created = {
                        "type": "document_comments",
                        "id": f"{PROJECT}/{SPACE}/{DOC}/99",
                    }
                return httpx.Response(201, json={"data": [created]})
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
        if request.method == "PATCH":
            testrecords = re.search(r"/testruns/([^/]+)/testrecords$", path)
            if testrecords:
                return self._patch_testrecords(testrecords.group(1), body)
        return httpx.Response(204)

    def _post_document_attachments(
        self, request: httpx.Request, doc_name: str
    ) -> httpx.Response:
        """Multipart upload route, live contract 2026-07-20: ``resource`` =
        plain form field, ordered ``files`` parts; 201 = list of
        type/id/links entries in input order; dup fileName 409 atomic.
        """
        content_type = request.headers.get("content-type", "")
        if not content_type.startswith("multipart/form-data"):
            return _error_response(415, "Unsupported Media Type")

        # Minimal multipart split — no boundary match = zero parts = 400 below.
        boundary_match = re.search(r'boundary="?([^";]+)"?', content_type)
        marker = f"--{boundary_match.group(1)}".encode() if boundary_match else b"\x00"
        resource_raw: bytes | None = None
        file_part_count = 0
        for segment in request.content.split(marker):
            head, separator, payload = segment.partition(b"\r\n\r\n")
            if not separator:
                continue  # preamble / closing "--" segment
            disposition = head.decode("utf-8", errors="replace")
            if 'name="resource"' in disposition:
                resource_raw = payload.rstrip(b"\r\n")
            elif 'name="files"' in disposition:
                file_part_count += 1

        if resource_raw is None:
            return _error_response(400, "Resource data not found in request.")
        try:
            entries = json.loads(resource_raw).get("data", [])
        except json.JSONDecodeError:
            return _error_response(400, "Resource data not found in request.")

        doc = self.seeds.documents.get(doc_name)
        if doc is None:
            return httpx.Response(404, json={"errors": [{"status": "404"}]})

        file_names = [
            entry.get("attributes", {}).get("fileName", "") for entry in entries
        ]
        if not all(file_names) or len(file_names) != file_part_count:
            return _error_response(400, "File data not found for entry.")
        existing = {a.attachment_id for a in doc.attachments}
        if existing & set(file_names):
            return _error_response(409, "A resource with the same ID already exists.")

        base = f"{POLARION_HOST}{API_PREFIX}/projects/{PROJECT}/spaces/{SPACE}"
        return httpx.Response(
            201,
            json={
                "data": [
                    {
                        "type": "document_attachments",
                        "id": f"{PROJECT}/{SPACE}/{doc_name}/{name}",
                        "links": {
                            "self": f"{base}/documents/{doc_name}/attachments/{name}",
                            "content": (
                                f"{base}/documents/{doc_name}/attachments/{name}/content"
                            ),
                        },
                    }
                    for name in file_names
                ]
            },
        )

    def _post_work_item_attachments(
        self, request: httpx.Request, work_item_id: str
    ) -> httpx.Response:
        """Multipart upload route mirroring ``_post_document_attachments``,
        minus dup-409: server never conflicts on fileName here, assigns a
        fresh counter-prefixed id per file instead (live-verified
        2026-07-21). Counter continue from seeded + previously created
        count, tracked in ``created_wi_attachments`` (off Seeds, so uploads
        stay instance-local).
        """
        content_type = request.headers.get("content-type", "")
        if not content_type.startswith("multipart/form-data"):
            return _error_response(415, "Unsupported Media Type")

        boundary_match = re.search(r'boundary="?([^";]+)"?', content_type)
        marker = f"--{boundary_match.group(1)}".encode() if boundary_match else b"\x00"
        resource_raw: bytes | None = None
        file_part_count = 0
        for segment in request.content.split(marker):
            head, separator, payload = segment.partition(b"\r\n\r\n")
            if not separator:
                continue  # preamble / closing "--" segment
            disposition = head.decode("utf-8", errors="replace")
            if 'name="resource"' in disposition:
                resource_raw = payload.rstrip(b"\r\n")
            elif 'name="files"' in disposition:
                file_part_count += 1

        if resource_raw is None:
            return _error_response(400, "Resource data not found in request.")
        try:
            entries = json.loads(resource_raw).get("data", [])
        except json.JSONDecodeError:
            return _error_response(400, "Resource data not found in request.")

        wi = self.seeds.work_items.get(work_item_id)
        if wi is None:
            return httpx.Response(404, json={"errors": [{"status": "404"}]})

        file_names = [
            entry.get("attributes", {}).get("fileName", "") for entry in entries
        ]
        if not all(file_names) or len(file_names) != file_part_count:
            return _error_response(400, "File data not found for entry.")

        created = self.created_wi_attachments.setdefault(work_item_id, [])
        base_count = len(wi.attachments) + len(created)
        attachment_ids = [
            f"{base_count + i + 1}-{name}" for i, name in enumerate(file_names)
        ]
        created.extend(Attachment(aid, "", 0) for aid in attachment_ids)

        base = f"{POLARION_HOST}{API_PREFIX}/projects/{PROJECT}"
        return httpx.Response(
            201,
            json={
                "data": [
                    {
                        "type": "workitem_attachments",
                        "id": f"{PROJECT}/{work_item_id}/{aid}",
                        "links": {
                            "self": (
                                f"{base}/workitems/{work_item_id}/attachments/{aid}"
                            ),
                            "content": (
                                f"{base}/workitems/{work_item_id}"
                                f"/attachments/{aid}/content"
                            ),
                        },
                    }
                    for aid in attachment_ids
                ]
            },
        )

    def _patch_testrecords(self, run_id: str, body: Any) -> httpx.Response:
        """Every submitted id must be the path run's seeded, non-template
        record -- other run's record or unknown id 400 the whole batch
        (live-verified atomic).
        """
        valid_ids = {
            f"{PROJECT}/{tr.short_id}/{PROJECT}/{TESTCASE_ID}/{i}"
            for tr in self.seeds.test_runs.values()
            if not tr.is_template and tr.short_id == run_id
            for i in range(tr.iterations)
        }
        entries = body.get("data") if isinstance(body, dict) else None
        entries = entries if isinstance(entries, list) else []
        for entry in entries:
            record_id = entry.get("id") if isinstance(entry, dict) else None
            if record_id not in valid_ids:
                return httpx.Response(
                    400,
                    json={
                        "errors": [
                            {
                                "status": "400",
                                "title": "Bad Request",
                                "detail": f"Test Record '{record_id}' was not found.",
                            }
                        ]
                    },
                )
        return httpx.Response(204)

    def install(self, router: respx.MockRouter) -> None:
        """Register catch-all Polarion route on *router*."""
        router.route(url__regex=rf"{re.escape(POLARION_HOST)}/.*").mock(
            side_effect=self._dispatch
        )
