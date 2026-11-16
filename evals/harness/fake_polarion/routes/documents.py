"""Documents: body, parts, copy, comments, attachments (space-scoped)."""

from __future__ import annotations

import httpx

from ...fixtures import PROJECT
from .. import wire
from ..router import PROJECT_PATH, ROUTER, Call
from ..world import DocumentState, Fault, World

_DOCS = PROJECT_PATH + "/spaces/{space}/documents"
_DOC = _DOCS + "/{document}"


def _document(world: World, call: Call) -> DocumentState:
    return world.document(call.args["space"], call.args["document"])


@ROUTER.post(_DOCS)
def create_documents(world: World, call: Call) -> httpx.Response:
    docs = [world.create_document(call.args["space"], e) for e in call.entries()]
    return wire.created("documents", [d.module_id for d in docs])


@ROUTER.get(_DOC)
def get_document(world: World, call: Call) -> httpx.Response:
    return wire.respond({"data": wire.document(_document(world, call))})


@ROUTER.patch(_DOC)
def update_document(world: World, call: Call) -> httpx.Response:
    attributes = call.resource().get("attributes")
    world.update_document(
        _document(world, call), attributes if isinstance(attributes, dict) else {}
    )
    return httpx.Response(204)


@ROUTER.post(_DOC + "/actions/copy")
def copy_document(world: World, call: Call) -> httpx.Response:
    """Live 201: single-dict ``data``, sparse attributes, lands in source
    space when ``targetSpaceId`` unset.
    """
    source = _document(world, call)
    body = call.flat()
    name = str(body.get("targetDocumentName", "") or "")
    if not name:
        raise Fault(400, "Required member 'targetDocumentName' was not found.")
    target = world.copy_document(
        source,
        str(body.get("targetSpaceId", "") or source.space),
        name,
        str(body.get("linkOriginalItemsWithRole", "") or ""),
        bool(body.get("removeOutgoingLinks", False)),
    )
    return wire.respond(
        {
            "data": {
                "type": "documents",
                "id": target.module_id,
                "attributes": {"status": "draft", "moduleFolder": target.space},
                "relationships": {
                    "project": {"data": {"type": "projects", "id": PROJECT}}
                },
            }
        },
        201,
    )


@ROUTER.get(_DOC + "/parts")
def list_parts(world: World, call: Call) -> httpx.Response:
    doc = _document(world, call)
    # Live: anchorless top-level block (no id) break whole /parts read.
    if any(b.kind == "normal" and not b.ref for b in doc.blocks):
        raise Fault(500, "Internal Server Error")
    return wire.respond(wire.parts(world, doc))


@ROUTER.get(_DOC + "/comments")
def list_comments(world: World, call: Call) -> httpx.Response:
    """No meta: live emit totalCount only on overshoot; fake never overshoot."""
    doc = _document(world, call)
    data = wire.comments(doc.comments, doc.module_id, "document_comments")
    return wire.respond(
        {"data": data, "included": wire.author_included() if data else []}
    )


@ROUTER.post(_DOC + "/comments")
def create_comments(world: World, call: Call) -> httpx.Response:
    doc = _document(world, call)
    ids = world.create_comments(doc.comments, call.entries())
    return wire.created("document_comments", [f"{doc.module_id}/{i}" for i in ids])


@ROUTER.patch(_DOC + "/comments/{comment}")
def update_comment(world: World, call: Call) -> httpx.Response:
    attributes = call.resource().get("attributes")
    world.update_comment(
        _document(world, call).comments,
        call.args["comment"],
        attributes if isinstance(attributes, dict) else {},
    )
    return httpx.Response(204)


@ROUTER.get(_DOC + "/attachments")
def list_attachments(world: World, call: Call) -> httpx.Response:
    doc = _document(world, call)
    data = wire.attachments(doc.attachments, doc.module_id, "document_attachments")
    return wire.respond(
        {"data": data, "included": wire.author_included() if data else []}
    )


@ROUTER.post(_DOC + "/attachments")
def create_attachments(world: World, call: Call) -> httpx.Response:
    """Multipart (live 2026-07-20): 415 / 400 resource -> 404 doc -> 400 file
    pairing -> 409 fileName clash.
    """
    entries, files = wire.uploads(call.request)
    doc = _document(world, call)
    ids = world.add_document_attachments(doc, wire.pair_uploads(entries, files))
    path = f"/projects/{PROJECT}/spaces/{doc.space}/documents/{doc.name}/attachments"
    return wire.attachments_created("document_attachments", doc.module_id, path, ids)


@ROUTER.get(_DOC + "/attachments/{attachment}/content")
def attachment_content(world: World, call: Call) -> httpx.Response:
    wire.require_octet_stream(call.request)
    doc = _document(world, call)
    return wire.attachment_content(doc.attachments, call.args["attachment"])
