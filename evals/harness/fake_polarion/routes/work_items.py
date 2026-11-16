"""Work items + their links, comments, attachments, document moves."""

from __future__ import annotations

import re

import httpx

from ...fixtures import PROJECT
from .. import wire
from ..router import PROJECT_PATH, ROUTER, Call
from ..world import World

_ITEMS = PROJECT_PATH + "/workitems"
_ITEM = _ITEMS + "/{work_item}"


def _title_query_terms(query: str) -> list[str]:
    """Lowercased phrases of ``title:"x"`` / ``title:(a OR b)`` clause;
    ``[]`` when nothing extractable (caller fall back to all).
    """
    _, _, tail = query.partition("title:")
    tail = tail.strip().strip('()"')
    terms = (t.strip('"').strip().lower() for t in re.split(r"\s+OR\s+", tail))
    return [t for t in terms if t]


@ROUTER.get(_ITEMS)
def list_work_items(world: World, call: Call) -> httpx.Response:
    """Lucene subset: ``type:heading``, ``linkedWorkItems:{wi}`` (back-link
    fallback), ``title:`` (live filter; unseeded title = empty, so agent
    cannot mine ids). Anything else (incl. ``SQL:(...)``) = all items.
    """
    # fields[documents] = list_documents discovery scan, not plain list.
    if call.query.get("fields[documents]"):
        return wire.respond(wire.discovery(world))
    query = call.query.get("query", "")
    items = list(world.work_items.values())
    if query == "type:heading":
        items = [w for w in items if w.attributes.get("type") == "heading"]
    elif query.startswith("linkedWorkItems:"):
        target = query.split(":", 1)[1].strip().rsplit("/", 1)[-1]
        items = world.linking_to(target)
    elif "title:" in query and (terms := _title_query_terms(query)):
        items = [
            w
            for w in items
            if any(t in str(w.attributes.get("title", "")).lower() for t in terms)
        ]
    data = [wire.work_item(w) for w in items]
    return wire.respond({"data": data, "meta": {"totalCount": len(data)}})


@ROUTER.post(_ITEMS)
def create_work_items(world: World, call: Call) -> httpx.Response:
    ids = world.create_work_items(call.entries())
    return wire.created("workitems", [f"{PROJECT}/{i}" for i in ids])


@ROUTER.patch(_ITEMS)
def update_work_items(world: World, call: Call) -> httpx.Response:
    world.update_work_items(call.entries(), call.query.get("changeTypeTo", ""))
    return httpx.Response(204)


@ROUTER.get(_ITEM)
def get_work_item(world: World, call: Call) -> httpx.Response:
    wi = world.work_item(call.args["work_item"])
    return wire.respond(
        {"data": wire.work_item(wi), "included": wire.author_included()}
    )


@ROUTER.post(_ITEM + "/actions/moveToDocument")
def move_to_document(world: World, call: Call) -> httpx.Response:
    body = call.flat()
    world.move_to_document(
        call.args["work_item"],
        str(body.get("targetDocument", "")),
        str(body.get("previousPart", "") or ""),
        str(body.get("nextPart", "") or ""),
    )
    return httpx.Response(204)


@ROUTER.post(_ITEM + "/actions/moveFromDocument")
def move_from_document(world: World, call: Call) -> httpx.Response:
    world.move_from_document(call.args["work_item"])
    return httpx.Response(204)


@ROUTER.get(_ITEM + "/linkedworkitems")
def list_links(world: World, call: Call) -> httpx.Response:
    return wire.respond(wire.links(world, call.args["work_item"]))


@ROUTER.post(_ITEM + "/linkedworkitems")
def create_links(world: World, call: Call) -> httpx.Response:
    ids = world.create_links(call.args["work_item"], call.entries())
    return wire.created("linkedworkitems", ids)


@ROUTER.delete(_ITEM + "/linkedworkitems")
def delete_links(world: World, call: Call) -> httpx.Response:
    ids = [str(entry.get("id", "")) for entry in call.entries()]
    world.delete_links(call.args["work_item"], ids)
    return httpx.Response(204)


@ROUTER.patch(_ITEM + "/linkedworkitems/{role}/{target_project}/{target}")
def update_link(world: World, call: Call) -> httpx.Response:
    attributes = call.resource().get("attributes")
    world.update_link(
        call.args["work_item"],
        call.args["role"],
        call.args["target"],
        attributes if isinstance(attributes, dict) else {},
    )
    return httpx.Response(204)


@ROUTER.get(_ITEM + "/comments")
def list_comments(world: World, call: Call) -> httpx.Response:
    wi_id = call.args["work_item"]
    wi = world.work_item(wi_id)
    data = wire.comments(wi.comments, f"{PROJECT}/{wi_id}", "workitem_comments")
    return wire.respond(
        {
            "data": data,
            "included": wire.author_included() if data else [],
            "meta": {"totalCount": len(data)},
        }
    )


@ROUTER.post(_ITEM + "/comments")
def create_comments(world: World, call: Call) -> httpx.Response:
    wi_id = call.args["work_item"]
    ids = world.create_comments(world.work_item(wi_id).comments, call.entries())
    return wire.created("workitem_comments", [f"{PROJECT}/{wi_id}/{i}" for i in ids])


@ROUTER.patch(_ITEM + "/comments/{comment}")
def update_comment(world: World, call: Call) -> httpx.Response:
    attributes = call.resource().get("attributes")
    world.update_comment(
        world.work_item(call.args["work_item"]).comments,
        call.args["comment"],
        attributes if isinstance(attributes, dict) else {},
    )
    return httpx.Response(204)


@ROUTER.get(_ITEM + "/attachments")
def list_attachments(world: World, call: Call) -> httpx.Response:
    wi_id = call.args["work_item"]
    resources = wire.attachments(
        world.work_item(wi_id).attachments, f"{PROJECT}/{wi_id}", "workitem_attachments"
    )
    return wire.respond(wire.attachment_page(resources, call.query))


@ROUTER.post(_ITEM + "/attachments")
def create_attachments(world: World, call: Call) -> httpx.Response:
    """Multipart; order (live 2026-07-21): 415 / 400 resource -> 404 item ->
    400 file pairing. Never 409 -- server counter-prefix every id.
    """
    entries, files = wire.uploads(call.request)
    wi_id = call.args["work_item"]
    wi = world.work_item(wi_id)
    ids = world.add_work_item_attachments(wi, wire.pair_uploads(entries, files))
    path = f"/projects/{PROJECT}/workitems/{wi_id}/attachments"
    return wire.attachments_created(
        "workitem_attachments", f"{PROJECT}/{wi_id}", path, ids
    )


@ROUTER.get(_ITEM + "/attachments/{attachment}/content")
def attachment_content(world: World, call: Call) -> httpx.Response:
    wire.require_octet_stream(call.request)
    wi = world.work_item(call.args["work_item"])
    return wire.attachment_content(wi.attachments, call.args["attachment"])
