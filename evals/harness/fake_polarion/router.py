"""Declarative route table: ``(method, path template) -> handler``.

Template segment ``{name}`` match one raw path segment (percent-decoded
after match, so document names with ``/`` survive). Full-path match = no
order-dependent regex chain. Route modules register via decorators on
``ROUTER``; importing ``routes`` package register all (mirror
``tools/__init__.py``).
"""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING
from urllib.parse import unquote

import httpx

from .world import Fault, Json

if TYPE_CHECKING:
    from .world import World

Handler = Callable[["World", "Call"], httpx.Response]


@dataclass(frozen=True)
class Call:
    """One routed request: decoded path ``args`` + parsed JSON ``body``."""

    request: httpx.Request
    args: dict[str, str]
    body: object = None

    @property
    def query(self) -> httpx.QueryParams:
        return self.request.url.params

    def entries(self) -> list[Json]:
        """``data`` list of resource POST / bulk PATCH / DELETE body."""
        data = self.body.get("data") if isinstance(self.body, dict) else None
        if not isinstance(data, list):
            raise Fault(400, "Request body must carry a 'data' list.")
        return [entry for entry in data if isinstance(entry, dict)]

    def resource(self) -> Json:
        """``data`` object of single-resource PATCH body."""
        data = self.body.get("data") if isinstance(self.body, dict) else None
        if not isinstance(data, dict):
            raise Fault(400, "Request body must carry a 'data' object.")
        return data

    def flat(self) -> Json:
        """Action endpoint body (flat object, no ``data`` wrapper)."""
        return self.body if isinstance(self.body, dict) else {}


class Router:
    def __init__(self) -> None:
        self._routes: list[tuple[str, re.Pattern[str], Handler]] = []

    def route(self, method: str, template: str) -> Callable[[Handler], Handler]:
        segments = [
            f"(?P<{seg[1:-1]}>[^/]+)" if seg.startswith("{") else re.escape(seg)
            for seg in template.split("/")
        ]
        pattern = re.compile("/".join(segments))

        def register(handler: Handler) -> Handler:
            self._routes.append((method, pattern, handler))
            return handler

        return register

    def get(self, template: str) -> Callable[[Handler], Handler]:
        return self.route("GET", template)

    def post(self, template: str) -> Callable[[Handler], Handler]:
        return self.route("POST", template)

    def patch(self, template: str) -> Callable[[Handler], Handler]:
        return self.route("PATCH", template)

    def delete(self, template: str) -> Callable[[Handler], Handler]:
        return self.route("DELETE", template)

    def resolve(self, method: str, path: str) -> tuple[Handler, dict[str, str]] | None:
        for route_method, pattern, handler in self._routes:
            if route_method == method and (match := pattern.fullmatch(path)):
                return handler, {k: unquote(v) for k, v in match.groupdict().items()}
        return None


ROUTER = Router()

# Common template prefix; every route scoped to one project.
PROJECT_PATH = "/projects/{project}"
