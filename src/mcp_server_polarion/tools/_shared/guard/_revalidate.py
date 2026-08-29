"""Stale-cache revalidation shared by guards that reject against a cached
option/key set: cached value never reject on its own, one refetch settle it.

Admin-added option or key land in Polarion mid-TTL; without this the guard
block a legitimate write until expiry, and the error's discovery-tool hint
cannot free it (that tool bypass the guard cache).
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable


async def resolve_with_refetch[T](
    *,
    get_cached: Callable[[], T | None],
    fetch: Callable[[], Awaitable[T]],
    accepts: Callable[[T], bool],
) -> T:
    """Value to judge against: cached one when *accepts* pass, else a fresh
    fetch. Freshly fetched value never refetch — caller reject on it.

    Stale entry left in place: *fetch* overwrite it on success, and a failed
    one keep it readable for the next caller it still satisfy.
    """
    value = get_cached()
    if value is not None and accepts(value):
        return value
    return await fetch()
