"""Request-layer building blocks shared by guard submodules: fail-closed GET
translation and page iteration, kept orthogonal so call sites compose only
what their error semantics need.
"""

from __future__ import annotations

from collections.abc import AsyncIterator

from mcp_server_polarion.core.client import PolarionClient
from mcp_server_polarion.core.exceptions import (
    PolarionAuthError,
    PolarionError,
    PolarionNotFoundError,
)
from mcp_server_polarion.tools._shared.guard._errors import (
    unauthorized_write_block,
    unreachable_write_block,
)

GUARD_PAGE_SIZE: int = 100


def _translate_error(
    exc: PolarionError, what: str, project_id: str
) -> PermissionError | RuntimeError:
    """Single source of the fail-closed mapping (auth → ``PermissionError``,
    else ``RuntimeError``); 404 policy stays at each call site.
    """
    if isinstance(exc, PolarionAuthError):
        return unauthorized_write_block(what, project_id)
    return unreachable_write_block(what, project_id, exc)


async def guarded_get(
    client: PolarionClient,
    path: str,
    params: dict[str, str | int],
    *,
    what: str,
    project_id: str,
) -> dict[str, object]:
    """GET with fail-closed translation (auth → ``PermissionError``, else
    ``RuntimeError``). 404 always re-raises — its meaning is per-call-site
    (defer, map to ``ValueError``, ...), so every caller handles it locally.
    """
    try:
        return await client.get(path, params=params)
    except PolarionNotFoundError:
        raise
    except PolarionError as exc:
        raise _translate_error(exc, what, project_id) from exc


async def paged_responses(
    client: PolarionClient,
    path: str,
    base_params: dict[str, str | int],
) -> AsyncIterator[tuple[list[object], dict[str, object]]]:
    """Yield each page as ``(data, response)`` — ``data`` already narrowed to
    a list, ``response`` kept whole for ``included`` readers; stop on non-list
    ``data`` or a short page. No error translation — compose with
    :func:`guarded_pages` for fail-closed semantics. ``page[size]`` is forced
    to ``GUARD_PAGE_SIZE`` here because the short-page stop compares against
    it.
    """
    page_number = 1
    while True:
        response = await client.get(
            path,
            params={
                **base_params,
                "page[size]": GUARD_PAGE_SIZE,
                "page[number]": page_number,
            },
        )
        data = response.get("data", [])
        if not isinstance(data, list):
            return
        yield data, response
        if len(data) < GUARD_PAGE_SIZE:
            return
        page_number += 1


async def guarded_pages(
    client: PolarionClient,
    path: str,
    base_params: dict[str, str | int],
    *,
    what: str,
    project_id: str,
) -> AsyncIterator[tuple[list[object], dict[str, object]]]:
    """:func:`paged_responses` with fail-closed translation around the
    fetches. Unlike :func:`guarded_get`, 404 folds into the unreachable block:
    paged sampling has no per-site 404 semantics to preserve.
    """
    try:
        async for page in paged_responses(client, path, base_params):
            yield page
    except PolarionError as exc:
        raise _translate_error(exc, what, project_id) from exc
