"""Request-layer building blocks for guard submodules: fail-closed GET
translation + page iteration, kept orthogonal — call sites compose only
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
    """Single source of fail-closed mapping (auth → ``PermissionError``,
    else ``RuntimeError``); 404 policy stay at each call site.
    """
    if isinstance(exc, PolarionAuthError):
        return unauthorized_write_block(what, project_id, exc)
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
    ``RuntimeError``). 404 always re-raise — meaning is per-call-site
    (defer, map to ``ValueError``, ...), every caller handle it locally.
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
    """Yield each page as ``(data, response)`` — ``data`` narrowed to list,
    ``response`` kept whole for ``included`` readers; stop on non-list
    ``data`` or short page. No error translation — compose with
    :func:`guarded_pages` for fail-closed semantics. ``page[size]`` forced
    to ``GUARD_PAGE_SIZE`` because short-page stop compare against it.
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
    """:func:`paged_responses` with fail-closed translation around fetches.
    Unlike :func:`guarded_get`, 404 fold into unreachable block: paged
    sampling has no per-site 404 semantics to preserve.
    """
    try:
        async for page in paged_responses(client, path, base_params):
            yield page
    except PolarionError as exc:
        raise _translate_error(exc, what, project_id) from exc
