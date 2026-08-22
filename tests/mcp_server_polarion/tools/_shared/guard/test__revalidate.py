"""Stale-cache revalidation engine: one refetch before a cached value is
allowed to reject.
"""

from __future__ import annotations

from collections.abc import Callable
from unittest.mock import AsyncMock, MagicMock

from mcp_server_polarion.tools._shared.guard._revalidate import resolve_with_refetch


class TestResolveWithRefetch:
    async def _run(
        self,
        *,
        get_cached: MagicMock,
        invalidate: MagicMock,
        fetch: AsyncMock,
        accepts: Callable[[frozenset[str]], bool],
    ) -> frozenset[str]:
        return await resolve_with_refetch(
            get_cached=get_cached,
            invalidate=invalidate,
            fetch=fetch,
            accepts=accepts,
        )

    async def test_accepted_cached_value_skips_fetch(self) -> None:
        get_cached = MagicMock(return_value=frozenset({"open"}))
        invalidate = MagicMock()
        fetch = AsyncMock()

        result = await self._run(
            get_cached=get_cached,
            invalidate=invalidate,
            fetch=fetch,
            accepts=lambda options: "open" in options,
        )

        assert result == frozenset({"open"})
        fetch.assert_not_awaited()
        invalidate.assert_not_called()

    async def test_cache_miss_fetches_once(self) -> None:
        get_cached = MagicMock(return_value=None)
        invalidate = MagicMock()
        fetch = AsyncMock(return_value=frozenset({"open"}))

        result = await self._run(
            get_cached=get_cached,
            invalidate=invalidate,
            fetch=fetch,
            accepts=lambda options: "open" in options,
        )

        assert result == frozenset({"open"})
        fetch.assert_awaited_once()
        invalidate.assert_not_called()

    async def test_rejected_cached_value_invalidates_and_refetches(self) -> None:
        # Option admin-added since caching: stale set reject, fresh set accept.
        get_cached = MagicMock(return_value=frozenset({"open"}))
        invalidate = MagicMock()
        fetch = AsyncMock(return_value=frozenset({"open", "blocked"}))

        result = await self._run(
            get_cached=get_cached,
            invalidate=invalidate,
            fetch=fetch,
            accepts=lambda options: "blocked" in options,
        )

        assert result == frozenset({"open", "blocked"})
        invalidate.assert_called_once()
        fetch.assert_awaited_once()

    async def test_fresh_fetch_never_refetched(self) -> None:
        get_cached = MagicMock(return_value=None)
        invalidate = MagicMock()
        fetch = AsyncMock(return_value=frozenset({"open"}))

        result = await self._run(
            get_cached=get_cached,
            invalidate=invalidate,
            fetch=fetch,
            accepts=lambda options: "blocked" in options,
        )

        assert result == frozenset({"open"})
        fetch.assert_awaited_once()
        invalidate.assert_not_called()

    async def test_refetched_value_returned_even_when_still_rejected(self) -> None:
        get_cached = MagicMock(return_value=frozenset({"stale"}))
        invalidate = MagicMock()
        fetch = AsyncMock(return_value=frozenset({"open"}))

        result = await self._run(
            get_cached=get_cached,
            invalidate=invalidate,
            fetch=fetch,
            accepts=lambda options: "blocked" in options,
        )

        # Caller render rejection from fresh set, never stale one.
        assert result == frozenset({"open"})
        fetch.assert_awaited_once()
