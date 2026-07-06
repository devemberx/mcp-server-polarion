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


async def guarded_get(  # noqa: PLR0913
    client: PolarionClient,
    path: str,
    params: dict[str, str | int],
    *,
    what: str,
    project_id: str,
    propagate_not_found: bool = False,
) -> dict[str, object]:
    """GET with fail-closed translation (auth → ``PermissionError``, else
    ``RuntimeError``). 404 semantics are per-call-site: the default folds into
    the unreachable block; ``propagate_not_found=True`` re-raises it for sites
    that defer or map it themselves.
    """
    try:
        return await client.get(path, params=params)
    except PolarionAuthError as exc:
        raise unauthorized_write_block(what, project_id) from exc
    except PolarionNotFoundError as exc:
        if propagate_not_found:
            raise
        raise unreachable_write_block(what, project_id, exc) from exc
    except PolarionError as exc:
        raise unreachable_write_block(what, project_id, exc) from exc


async def paged_responses(
    client: PolarionClient,
    path: str,
    base_params: dict[str, str | int],
) -> AsyncIterator[dict[str, object]]:
    """Yield each page's full response (consumers read ``included`` too);
    stop on non-list ``data`` or a short page. No error translation — callers
    wrap the ``async for`` when they need it.
    """
    page_number = 1
    while True:
        response = await client.get(
            path, params={**base_params, "page[number]": page_number}
        )
        data = response.get("data", [])
        if not isinstance(data, list):
            return
        yield response
        if len(data) < GUARD_PAGE_SIZE:
            return
        page_number += 1
