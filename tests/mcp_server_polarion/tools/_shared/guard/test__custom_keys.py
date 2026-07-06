"""Custom-field key validation: the shared check engine's control flow plus
unknown-key rejection and key extraction from JSON:API data lists.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from mcp_server_polarion.tools._shared.guard._custom_keys import (
    check_custom_keys,
    custom_keys_from_data_list,
    reject_unknown_custom_keys,
)


class TestCheckCustomKeys:
    async def _run(
        self,
        custom_fields: dict[str, object],
        *,
        get_cached: MagicMock,
        invalidate: MagicMock,
        fetch: AsyncMock,
    ) -> None:
        await check_custom_keys(
            custom_fields,
            get_cached=get_cached,
            invalidate=invalidate,
            fetch=fetch,
            scope="scope 'S'",
            discovery_tool="a sample",
            empty_schema_error="empty schema for S",
        )

    async def test_cached_schema_passes_without_fetch(self) -> None:
        get_cached = MagicMock(return_value=frozenset({"risk"}))
        invalidate = MagicMock()
        fetch = AsyncMock()

        await self._run(
            {"risk": 1}, get_cached=get_cached, invalidate=invalidate, fetch=fetch
        )

        fetch.assert_not_awaited()
        invalidate.assert_not_called()

    async def test_cache_miss_fetches_once_and_passes(self) -> None:
        get_cached = MagicMock(return_value=None)
        invalidate = MagicMock()
        fetch = AsyncMock(return_value=frozenset({"risk"}))

        await self._run(
            {"risk": 1}, get_cached=get_cached, invalidate=invalidate, fetch=fetch
        )

        fetch.assert_awaited_once()
        invalidate.assert_not_called()

    async def test_unknown_key_against_cached_schema_refetches_once(self) -> None:
        # Key admin-added since caching: stale cache says unknown, fresh says known.
        get_cached = MagicMock(return_value=frozenset({"risk"}))
        invalidate = MagicMock()
        fetch = AsyncMock(return_value=frozenset({"risk", "newKey"}))

        await self._run(
            {"newKey": 1}, get_cached=get_cached, invalidate=invalidate, fetch=fetch
        )

        invalidate.assert_called_once()
        fetch.assert_awaited_once()

    async def test_unknown_key_against_fresh_fetch_rejects_without_retry(self) -> None:
        get_cached = MagicMock(return_value=None)
        invalidate = MagicMock()
        fetch = AsyncMock(return_value=frozenset({"risk"}))

        with pytest.raises(ValueError, match=r"\['ghost'\]"):
            await self._run(
                {"ghost": 1}, get_cached=get_cached, invalidate=invalidate, fetch=fetch
            )

        fetch.assert_awaited_once()
        invalidate.assert_not_called()

    async def test_empty_schema_fails_closed_with_supplied_error(self) -> None:
        get_cached = MagicMock(return_value=None)
        invalidate = MagicMock()
        fetch = AsyncMock(return_value=frozenset())

        with pytest.raises(RuntimeError, match="empty schema for S"):
            await self._run(
                {"ghost": 1}, get_cached=get_cached, invalidate=invalidate, fetch=fetch
            )


class TestRejectUnknownCustomKeys:
    def test_all_known_keys_pass(self) -> None:
        reject_unknown_custom_keys(
            {"risk": "high", "freeText": "x"},
            frozenset({"risk", "freeText"}),
            scope="project 'P' type 'task'",
            discovery_tool="get_work_item",
        )

    def test_empty_custom_fields_pass(self) -> None:
        reject_unknown_custom_keys(
            {},
            frozenset(),
            scope="project 'P' type 'task'",
            discovery_tool="get_work_item",
        )

    def test_unknown_keys_raise_sorted_with_scope_and_tool(self) -> None:
        with pytest.raises(ValueError, match=r"\['aaa', 'zzz'\]") as exc_info:
            reject_unknown_custom_keys(
                {"zzz": 1, "risk": "high", "aaa": 2},
                frozenset({"risk"}),
                scope="project 'P' type 'task'",
                discovery_tool="get_work_item",
            )

        message = str(exc_info.value)
        assert "get_work_item" in message
        assert "project 'P' type 'task'" in message
        assert "ghost" in message


class TestCustomKeysFromDataList:
    def test_collects_non_allowlisted_attribute_keys(self) -> None:
        response: dict[str, object] = {
            "data": [
                {"attributes": {"id": "P/W-1", "risk": "high"}},
                {"attributes": {"id": "P/W-2", "freeText": "x"}},
            ]
        }

        keys = custom_keys_from_data_list(response, frozenset({"id"}))

        assert keys == frozenset({"risk", "freeText"})

    def test_missing_data_returns_empty(self) -> None:
        assert custom_keys_from_data_list({}, frozenset()) == frozenset()

    def test_non_list_data_returns_empty(self) -> None:
        response: dict[str, object] = {"data": {"attributes": {"risk": "high"}}}

        assert custom_keys_from_data_list(response, frozenset()) == frozenset()

    def test_non_dict_entries_and_attributes_skipped(self) -> None:
        response: dict[str, object] = {
            "data": ["stray", {"attributes": "stray"}, {"attributes": {"risk": 1}}]
        }

        assert custom_keys_from_data_list(response, frozenset()) == frozenset({"risk"})
