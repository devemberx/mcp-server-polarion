"""In-process fake Polarion: real project *structure*, synthetic *content* (no
production data in eval logs), live write semantics -- writes land in one
``World`` per instance (= per eval run) and later reads see them, so
verify-after-write agents observe their own effects. One catch-all respx
route on Polarion host. LLM traffic bypass respx: litellm aiohttp transport
replace httpcore, respx patch httpcore only. Seeds from ``fixtures``;
``seeds`` injectable for per-case alternates.

Layers: ``world`` = state + write rules (no HTTP), ``body`` = document body
<-> blocks, ``wire`` = JSON:API shapes, ``routes/`` = thin handlers on
``router``. New endpoint = one handler; new rule = one ``World`` method.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any

import httpx
import respx

from ..fixtures import API_PREFIX, POLARION_HOST, PROJECT, SEEDS, Seeds
from . import routes  # noqa: F401 -- import register handlers
from .router import ROUTER, Call
from .wire import error
from .world import Fault, World

_MUTATING = frozenset({"POST", "PATCH", "DELETE"})


@dataclass
class FakePolarion:
    """Seeded, stateful, structure-faithful fake Polarion served over respx."""

    seeds: Seeds = SEEDS
    # Every write request, applied or rejected -- debugging + tests.
    mutations: list[dict[str, Any]] = field(default_factory=list)
    world: World = field(init=False)

    def __post_init__(self) -> None:
        self.world = World.from_seeds(self.seeds)

    def _dispatch(self, request: httpx.Request) -> httpx.Response:
        # Raw path keep %2F inside one segment; router decode per segment.
        raw = request.url.raw_path.split(b"?", 1)[0].decode("ascii")
        path = raw.removeprefix(API_PREFIX)
        body = self._record(request) if request.method in _MUTATING else None
        resolved = ROUTER.resolve(request.method, path)
        if resolved is None:
            if request.method in _MUTATING:
                # Unmodeled write must never fake success.
                return error(
                    501, f"fake_polarion: no route for {request.method} {path}"
                )
            return httpx.Response(
                404, json={"errors": [{"status": "404", "path": path}]}
            )
        handler, args = resolved
        if args.get("project", PROJECT) != PROJECT:
            return error(404, f"Project '{args['project']}' was not found.")
        try:
            return handler(self.world, Call(request, args, body))
        except Fault as fault:
            return error(fault.status, fault.detail)

    def _record(self, request: httpx.Request) -> object:
        # Multipart request content = lazy stream; materialize before access.
        request.read()
        body: object = None
        if request.content:
            try:
                body = json.loads(request.content)
            except (json.JSONDecodeError, UnicodeDecodeError):
                # UnicodeDecodeError = binary multipart body; leak froze eval
                # runs (respx __cause__ cycle -> rich extract infinite loop).
                body = None
        path = request.url.path.removeprefix(API_PREFIX)
        self.mutations.append({"method": request.method, "path": path, "json": body})
        return body

    def install(self, router: respx.MockRouter) -> None:
        """Register catch-all Polarion route on *router*."""
        router.route(url__regex=rf"{re.escape(POLARION_HOST)}/.*").mock(
            side_effect=self._dispatch
        )
