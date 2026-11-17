"""JSON:API wire shapes of world state + request decoding. Live response
quirks (meta presence, sparse bodies, per-resource id shapes) live here,
never in ``world``.
"""

from __future__ import annotations

import json
import re

import httpx

from mcp_server_polarion.tools._shared.pagination import DEFAULT_PAGE_SIZE

from ..fixtures import (
    API_PREFIX,
    AUTHOR,
    POLARION_HOST,
    PROJECT,
    TS,
    Attachment,
    Comment,
)
from . import body
from .world import (
    DocumentState,
    Fault,
    Json,
    Link,
    TestRecord,
    TestRunState,
    WorkItemState,
    World,
    record_id,
)

_AUTHOR_REF: Json = {"data": {"type": "users", "id": f"{PROJECT}/{AUTHOR}"}}

# Test record attributes single GET serve but list omit.
_RECORD_DETAIL_ONLY = ("comment", "testCaseRevision")


def respond(payload: Json, status: int = 200) -> httpx.Response:
    return httpx.Response(status, json=payload)


def error(status: int, detail: str = "") -> httpx.Response:
    entry: Json = {"status": str(status)}
    if detail:
        entry["detail"] = detail
    return httpx.Response(status, json={"errors": [entry]})


def created(resource_type: str, ids: list[str]) -> httpx.Response:
    """Resource POST 201: one ``{type, id}`` per submitted entry, input order."""
    return respond({"data": [{"type": resource_type, "id": i} for i in ids]}, 201)


def attachments_created(
    resource_type: str, id_prefix: str, path: str, ids: list[str]
) -> httpx.Response:
    """Multipart upload 201: ``{type, id, links}`` per file, input order."""
    url = f"{POLARION_HOST}{API_PREFIX}{path}"
    return respond(
        {
            "data": [
                {
                    "type": resource_type,
                    "id": f"{id_prefix}/{i}",
                    "links": {"self": f"{url}/{i}", "content": f"{url}/{i}/content"},
                }
                for i in ids
            ]
        },
        201,
    )


def author_included() -> list[Json]:
    """``included`` user resolving shared author id to name (production read
    ask ``include=author&fields[users]=name``).
    """
    return [
        {
            "type": "users",
            "id": f"{PROJECT}/{AUTHOR}",
            "attributes": {"name": "Fake Author"},
        }
    ]


def work_item(wi: WorkItemState) -> Json:
    relationships: Json = {
        "assignee": {"data": [{"type": "users", "id": a} for a in wi.assignees]},
        "author": _AUTHOR_REF,
    }
    if wi.module:
        relationships["module"] = {"data": {"type": "documents", "id": wi.module}}
    return {
        "type": "workitems",
        "id": f"{PROJECT}/{wi.id}",
        "attributes": dict(wi.attributes),
        "relationships": relationships,
    }


def document(doc: DocumentState) -> Json:
    return {
        "type": "documents",
        "id": doc.module_id,
        "attributes": {
            **doc.attributes,
            "moduleName": doc.name,
            "moduleFolder": doc.space,
            "homePageContent": {"type": "text/html", "value": body.render(doc.blocks)},
        },
    }


def discovery(world: World) -> Json:
    """list_documents scan: heading work items carrying ``module`` + module
    documents in ``included``. Docs without heading never surface (mirror
    production GROUP-BY-heading discovery).
    """
    headings = [
        wi
        for wi in world.work_items.values()
        if wi.attributes.get("type") == "heading" and wi.module
    ]
    included: list[Json] = []
    for wi in headings:
        doc = world.find_document(wi.module)
        if doc is not None and all(i["id"] != doc.module_id for i in included):
            included.append(
                {
                    "type": "documents",
                    "id": doc.module_id,
                    "attributes": {
                        "type": doc.attributes.get("type"),
                        "status": doc.attributes.get("status"),
                        "updated": wi.attributes.get("updated"),
                    },
                    "relationships": {"author": _AUTHOR_REF, "updatedBy": _AUTHOR_REF},
                }
            )
    data = [work_item(wi) for wi in headings]
    return {"data": data, "included": included, "meta": {"totalCount": len(data)}}


def parts(world: World, doc: DocumentState, query: httpx.QueryParams) -> Json:
    """Live shape: ``content`` = block markup, heading + ``headingText`` /
    ``level``, workitem + ``level``/``layout`` 0, ``previousPart`` /
    ``nextPart`` chain over whole body (absent at ends), page members in
    ``included``, paged with sparse ``meta``.
    """
    data: list[Json] = []
    included: list[Json] = []
    blocks = doc.blocks
    for i in window(list(range(len(blocks))), query):
        block = blocks[i]
        attributes: Json = {
            "id": block.part_id,
            "content": block.render(),
            "type": block.kind,
        }
        relationships: Json = {}
        if block.kind != "normal":
            member = world.work_items[block.ref]
            relationships["workItem"] = {
                "data": {"type": "workitems", "id": f"{PROJECT}/{member.id}"}
            }
            included.append(work_item(member))
            if block.kind == "heading":
                attributes["headingText"] = member.attributes.get("title", "")
                attributes["level"] = block.level
            else:
                attributes["level"] = 0
                attributes["layout"] = 0
        if i > 0:
            relationships["previousPart"] = _part_ref(doc, blocks[i - 1].part_id)
        if i + 1 < len(blocks):
            relationships["nextPart"] = _part_ref(doc, blocks[i + 1].part_id)
        data.append(
            {
                "type": "document_parts",
                "id": f"{doc.module_id}/{block.part_id}",
                "attributes": attributes,
                "relationships": relationships,
            }
        )
    return sparse_meta({"data": data, "included": included}, len(blocks), query)


def links(world: World, source_id: str, query: httpx.QueryParams) -> Json:
    """Targets ship as ``include=workItem`` -- parser derive target from
    ``relationships.workItem``, never composite id.
    """
    data: list[Json] = []
    included: list[Json] = []
    every = world.links_of(source_id)
    for link in window(every, query):
        data.append(_link(source_id, link))
        target = world.work_items.get(link.target)
        if target is not None:
            included.append(work_item(target))
    return {"data": data, "included": included, "meta": {"totalCount": len(every)}}


def comments(thread: list[Comment], base: str, comment_type: str) -> list[Json]:
    """Document (4-segment ``base``) + work item (3-segment) threads.
    Children derive from ``parent_id``; ``title`` only when set.
    """
    resources: list[Json] = []
    for comment in thread:
        children = [
            {"id": f"{base}/{c.comment_id}"}
            for c in thread
            if c.parent_id == comment.comment_id
        ]
        parent = (
            {"data": {"id": f"{base}/{comment.parent_id}"}}
            if comment.parent_id
            else {"data": None}
        )
        attributes: Json = {
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


def attachments(items: list[Attachment], base: str, resource_type: str) -> list[Json]:
    """``attributes.id`` = bare token body HTML reference; resource id prefix
    it with ``base``. Polarion serve no ``created``, no mime type, and (sparse
    fieldset) author relationship only (verified 2026-07-18).
    """
    return [
        {
            "type": resource_type,
            "id": f"{base}/{a.attachment_id}",
            "attributes": {
                "id": a.attachment_id,
                "fileName": a.attachment_id,
                "title": a.title,
                "updated": TS,
                "length": a.length,
            },
            "relationships": {"author": {"data": {"id": f"{PROJECT}/{AUTHOR}"}}},
        }
        for a in items
    ]


def window[T](items: list[T], query: httpx.QueryParams) -> list[T]:
    """``page[size]`` / ``page[number]`` slice of *items* (live default 100)."""
    size = int(query.get("page[size]", str(DEFAULT_PAGE_SIZE)))
    number = int(query.get("page[number]", "1"))
    return items[(number - 1) * size : number * size]


def sparse_meta(page: Json, total: int, query: httpx.QueryParams) -> Json:
    """Add ``meta.totalCount`` per live rule of work item / test record
    attachments + document parts: every page once collection span >1 page,
    plus overshoot past non-empty collection; single page / empty omit it.
    """
    size = int(query.get("page[size]", str(DEFAULT_PAGE_SIZE)))
    overshoot = int(query.get("page[number]", "1")) > 1 and not page["data"]
    if total > size or (overshoot and total):
        page["meta"] = {"totalCount": total}
    return page


def attachment_page(resources: list[Json], query: httpx.QueryParams) -> Json:
    """Work item / test record attachment page."""
    data = window(resources, query)
    page: Json = {"data": data, "included": author_included() if data else []}
    return sparse_meta(page, len(resources), query)


def attachment_content(items: list[Attachment], wanted: str) -> httpx.Response:
    """Raw bytes; 406 without octet-stream Accept (checked by caller first),
    404 unknown id (#194: content route 404, never list route empty-200).
    """
    for item in items:
        if item.attachment_id == wanted:
            return httpx.Response(
                200,
                content=item.content,
                headers={"Content-Type": "application/octet-stream"},
            )
    raise Fault(404, f"Attachment '{wanted}' was not found.")


def require_octet_stream(request: httpx.Request) -> None:
    if "application/octet-stream" not in request.headers.get("accept", ""):
        raise Fault(406, "Not Acceptable")


def test_run(run: TestRunState, *, detail: bool = False) -> Json:
    """Single GET add ``id``; ``isTemplate`` served only on templates there
    (live omit it on instances).
    """
    attributes = dict(run.attributes)
    if detail:
        attributes["id"] = run.id
        if not run.is_template:
            attributes.pop("isTemplate", None)
    return {
        "type": "testruns",
        "id": f"{PROJECT}/{run.id}",
        "attributes": attributes,
        "relationships": {"author": _AUTHOR_REF},
    }


def test_record(run_id: str, record: TestRecord, *, detail: bool = False) -> Json:
    """Record POSTed without any attribute (no ``result``) ship no
    ``attributes`` block -- ``iteration`` included (live); id still carry it.
    """
    relationships: Json = {
        "testCase": {
            "data": {"type": "workitems", "id": f"{PROJECT}/{record.test_case}"}
        },
        "executedBy": _AUTHOR_REF,
    }
    if record.defect:
        relationships["defect"] = {
            "data": {"type": "workitems", "id": f"{PROJECT}/{record.defect}"}
        }
    resource: Json = {
        "type": "testrecords",
        "id": record_id(run_id, record),
        "relationships": relationships,
    }
    if record.attributes:
        attributes = {**record.attributes, "iteration": record.iteration}
        if not detail:
            for key in _RECORD_DETAIL_ONLY:
                attributes.pop(key, None)
        resource["attributes"] = attributes
    return resource


def enum_options(options: list[tuple[str, bool]]) -> Json:
    data = [
        {
            "id": option_id,
            "name": option_id,
            "description": "",
            "default": is_default,
            "hidden": False,
            "terminal": False,
        }
        for option_id, is_default in options
    ]
    return {"data": data, "meta": {"totalCount": len(data)}}


def uploads(request: httpx.Request) -> tuple[list[Json], list[bytes]]:
    """Multipart attachment POST: ``resource`` JSON:API entries + ordered
    ``files`` payloads. Wrong content type = 415, missing / bad resource = 400.
    """
    content_type = request.headers.get("content-type", "")
    if not content_type.startswith("multipart/form-data"):
        raise Fault(415, "Unsupported Media Type")
    boundary = re.search(r'boundary="?([^";]+)"?', content_type)
    # No boundary = zero parts = missing resource below.
    marker = f"--{boundary.group(1)}".encode() if boundary else b"\x00"
    resource_raw: bytes | None = None
    files: list[bytes] = []
    for segment in request.content.split(marker):
        head, separator, payload = segment.partition(b"\r\n\r\n")
        if not separator:
            continue  # preamble / closing "--" segment
        payload = payload.removesuffix(b"\r\n")
        disposition = head.decode("utf-8", errors="replace")
        if 'name="resource"' in disposition:
            resource_raw = payload
        elif 'name="files"' in disposition:
            files.append(payload)
    if resource_raw is None:
        raise Fault(400, "Resource data not found in request.")
    try:
        entries = json.loads(resource_raw).get("data", [])
    except json.JSONDecodeError:
        raise Fault(400, "Resource data not found in request.") from None
    return [e for e in entries if isinstance(e, dict)], files


def pair_uploads(entries: list[Json], files: list[bytes]) -> list[Attachment]:
    """Entry i <-> file part i; empty fileName or count mismatch = 400."""
    names = [str(_attributes(e).get("fileName", "")) for e in entries]
    if not all(names) or len(names) != len(files):
        raise Fault(400, "File data not found for entry.")
    return [
        # Live: title settable at POST, served on explicit fields.
        Attachment(name, str(_attributes(e).get("title", "")), len(data), data)
        for name, e, data in zip(names, entries, files, strict=True)
    ]


def _attributes(entry: Json) -> Json:
    value = entry.get("attributes")
    return value if isinstance(value, dict) else {}


def _link(source_id: str, link: Link) -> Json:
    attributes: Json = {"role": link.role, "suspect": link.suspect}
    if link.revision:
        attributes["revision"] = link.revision
    return {
        "type": "linkedworkitems",
        "id": f"{PROJECT}/{source_id}/{link.role}/{PROJECT}/{link.target}",
        "attributes": attributes,
        "relationships": {
            "workItem": {
                "data": {"type": "workitems", "id": f"{PROJECT}/{link.target}"}
            }
        },
    }


def _part_ref(doc: DocumentState, part_id: str) -> Json:
    return {"data": {"type": "document_parts", "id": f"{doc.module_id}/{part_id}"}}
