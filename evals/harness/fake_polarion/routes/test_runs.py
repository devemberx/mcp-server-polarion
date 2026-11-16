"""Test runs, test records, record attachments."""

from __future__ import annotations

import httpx

from ...fixtures import PROJECT
from .. import wire
from ..router import PROJECT_PATH, ROUTER, Call
from ..world import TestRecord, World

_RUNS = PROJECT_PATH + "/testruns"
_RUN = _RUNS + "/{run}"
_RECORDS = _RUN + "/testrecords"
_RECORD = _RECORDS + "/{case_project}/{case}/{iteration}"


def _record(world: World, call: Call) -> TestRecord:
    a = call.args
    return world.record(a["run"], a["case_project"], a["case"], a["iteration"])


@ROUTER.get(_RUNS)
def list_test_runs(world: World, call: Call) -> httpx.Response:
    want_templates = call.query.get("templates", "").lower() == "true"
    runs = [r for r in world.test_runs.values() if r.is_template == want_templates]
    data = [wire.test_run(r) for r in runs]
    return wire.respond(
        {
            "data": data,
            "included": wire.author_included() if data else [],
            "meta": {"totalCount": len(data)},
        }
    )


@ROUTER.post(_RUNS)
def create_test_runs(world: World, call: Call) -> httpx.Response:
    ids = world.create_test_runs(call.entries())
    return wire.created("testruns", [f"{PROJECT}/{i}" for i in ids])


@ROUTER.patch(_RUNS)
def update_test_runs(world: World, call: Call) -> httpx.Response:
    world.update_test_runs(call.entries())
    return httpx.Response(204)


@ROUTER.get(_RUN)
def get_test_run(world: World, call: Call) -> httpx.Response:
    run = world.test_run(call.args["run"])
    return wire.respond(
        {"data": wire.test_run(run, detail=True), "included": wire.author_included()}
    )


@ROUTER.get(_RECORDS)
def list_test_records(world: World, call: Call) -> httpx.Response:
    """``testResultId`` filter server-side; no meta (live omit totalCount,
    verified 2026-07-12).
    """
    run = world.test_run(call.args["run"])
    data = [wire.test_record(run.id, rec) for rec in run.records]
    if wanted := call.query.get("testResultId", ""):
        data = [r for r in data if r["attributes"].get("result") == wanted]
    return wire.respond(
        {"data": data, "included": wire.author_included() if data else []}
    )


@ROUTER.post(_RECORDS)
def create_test_records(world: World, call: Call) -> httpx.Response:
    ids = world.create_test_records(call.args["run"], call.entries())
    return wire.created("testrecords", ids)


@ROUTER.patch(_RECORDS)
def update_test_records(world: World, call: Call) -> httpx.Response:
    world.update_test_records(call.args["run"], call.entries())
    return httpx.Response(204)


@ROUTER.get(_RECORD)
def get_test_record(world: World, call: Call) -> httpx.Response:
    record = _record(world, call)
    return wire.respond(
        {
            "data": wire.test_record(call.args["run"], record, detail=True),
            "included": wire.author_included(),
        }
    )


@ROUTER.get(_RECORD + "/attachments")
def list_record_attachments(world: World, call: Call) -> httpx.Response:
    record = _record(world, call)
    base = (
        f"{PROJECT}/{call.args['run']}/{PROJECT}/{record.test_case}/{record.iteration}"
    )
    resources = wire.attachments(record.attachments, base, "testrecord_attachments")
    return wire.respond(wire.attachment_page(resources, call.query))


@ROUTER.post(_RECORD + "/attachments")
def create_record_attachments(world: World, call: Call) -> httpx.Response:
    """Multipart mirror work item sibling, but id rewritten to
    ``{testCaseId}_{fileName}`` + 409 on clash (live 2026-07-21).
    """
    entries, files = wire.uploads(call.request)
    record = _record(world, call)
    ids = world.add_record_attachments(record, wire.pair_uploads(entries, files))
    coordinate = f"{PROJECT}/{record.test_case}/{record.iteration}"
    run_path = f"/projects/{PROJECT}/testruns/{call.args['run']}"
    path = f"{run_path}/testrecords/{coordinate}/attachments"
    return wire.attachments_created(
        "testrecord_attachments",
        f"{PROJECT}/{call.args['run']}/{coordinate}",
        path,
        ids,
    )


@ROUTER.get(_RECORD + "/attachments/{attachment}/content")
def record_attachment_content(world: World, call: Call) -> httpx.Response:
    wire.require_octet_stream(call.request)
    return wire.attachment_content(
        _record(world, call).attachments, call.args["attachment"]
    )
