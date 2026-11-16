"""Projects + enumerations (read-only seed tables)."""

from __future__ import annotations

import httpx

from ...fixtures import PROJECT
from .. import wire
from ..router import PROJECT_PATH, ROUTER, Call
from ..world import Fault, World


@ROUTER.get("/projects")
def list_projects(world: World, call: Call) -> httpx.Response:
    return wire.respond(
        {
            "data": [
                {
                    "type": "projects",
                    "id": PROJECT,
                    "attributes": {"name": "Fake Project", "active": True},
                }
            ],
            "meta": {"totalCount": 1},
        }
    )


@ROUTER.get(PROJECT_PATH + "/{resource}/fields/{field}/actions/getAvailableOptions")
def available_options(world: World, call: Call) -> httpx.Response:
    if call.args["resource"] not in ("workitems", "documents"):
        raise Fault(404, "Not Found")
    key = (call.args["resource"], call.args["field"])
    return wire.respond(wire.enum_options(world.enums.get(key, [])))


@ROUTER.get(PROJECT_PATH + "/enumerations/{context}/{name}/~")
def project_enum(world: World, call: Call) -> httpx.Response:
    """Context-qualified names ("testing/testrun-type") = separate seed keys,
    so wildcard-context probes for them 404 like live. ``data`` = dict here,
    unlike getAvailableOptions' list.
    """
    context, name = call.args["context"], call.args["name"]
    options = world.project_enums.get(name if context == "~" else f"{context}/{name}")
    if options is None:
        raise Fault(404, f"Enumeration '{context}/{name}' was not found.")
    return wire.respond(
        {
            "data": {
                "type": "enumerations",
                "id": f"{context}/{name}/~",
                "attributes": {"options": [{"id": o} for o in options]},
            }
        }
    )
