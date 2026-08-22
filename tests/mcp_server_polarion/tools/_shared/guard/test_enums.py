"""Enum option fetch: parse path, fail-closed on Polarion error (write
blocked, not skipped).
"""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from mcp_server_polarion.core.exceptions import (
    PolarionAuthError,
    PolarionError,
    PolarionNotFoundError,
)
from mcp_server_polarion.tools._shared import cache as cache_mod
from mcp_server_polarion.tools._shared.guard import guard_work_item_enums
from mcp_server_polarion.tools._shared.guard.enums import (
    fetch_enum_option_ids,
    fetch_field_options,
)
from tests.mcp_server_polarion.tools._shared.guard._builders import (
    enum_response,
    project_enum_response,
)


class TestFetchFieldOptions:
    """Direct ``getAvailableOptions`` parsing + caching."""

    async def test_first_call_hits_polarion_and_parses_options(
        self, mock_client: AsyncMock
    ) -> None:
        mock_client.get.return_value = enum_response(["must_have", "should_have"])

        options = await fetch_field_options(
            mock_client, "P", "workitems", "severity", "task"
        )

        assert options == {"must_have": "must_have", "should_have": "should_have"}
        mock_client.get.assert_awaited_once()
        path, kwargs = (
            mock_client.get.call_args.args[0],
            mock_client.get.call_args.kwargs,
        )
        expected = "/projects/P/workitems/fields/severity/actions/getAvailableOptions"
        assert path == expected
        assert kwargs["params"]["type"] == "task"
        assert kwargs["params"]["page[size]"] == 100

    async def test_display_name_kept_when_it_differs_from_id(
        self, mock_client: AsyncMock
    ) -> None:
        # Rendering-layout `label` read the name, so id -> name must not
        # collapse; shared builder always set name == id, hide the mapping.
        mock_client.get.return_value = {
            "data": [{"id": "testcase", "name": "Test Case"}],
            "meta": {"totalCount": 1},
        }

        options = await fetch_field_options(mock_client, "P", "workitems", "type", "~")

        assert options == {"testcase": "Test Case"}

    async def test_second_call_uses_cache(self, mock_client: AsyncMock) -> None:
        mock_client.get.return_value = enum_response(["a", "b"])

        await fetch_field_options(mock_client, "P", "workitems", "severity", "task")
        await fetch_field_options(mock_client, "P", "workitems", "severity", "task")

        assert mock_client.get.await_count == 1

    async def test_cache_expiry_re_fetches(
        self, mock_client: AsyncMock, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        mock_client.get.return_value = enum_response(["a"])
        clock = [1000.0]
        monkeypatch.setattr(cache_mod, "_now", lambda: clock[0])

        await fetch_field_options(mock_client, "P", "workitems", "severity", "task")
        clock[0] += 61.0  # past the 60s TTL
        await fetch_field_options(mock_client, "P", "workitems", "severity", "task")

        assert mock_client.get.await_count == 2

    async def test_polarion_error_blocks_write_and_logs(
        self,
        mock_client: AsyncMock,
        caplog: pytest.LogCaptureFixture,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        # setup_logging set propagate=False — caplog miss package logs;
        # re-enable propagation locally for order independence.
        import logging  # noqa: PLC0415 -- fixture-local import is intentional

        monkeypatch.setattr(logging.getLogger("mcp_server_polarion"), "propagate", True)
        caplog.set_level("WARNING", logger="mcp_server_polarion.tools._shared.guard")
        mock_client.get.side_effect = PolarionError("backend down")

        with pytest.raises(RuntimeError, match="Refusing the write"):
            await fetch_field_options(mock_client, "P", "workitems", "severity", "task")

        assert any("blocking write" in r.message for r in caplog.records)

    async def test_auth_error_raises_permission_error(
        self, mock_client: AsyncMock
    ) -> None:
        mock_client.get.side_effect = PolarionAuthError("forbidden", status_code=403)

        with pytest.raises(PermissionError, match="lacks permission"):
            await fetch_field_options(mock_client, "P", "workitems", "severity", "task")

    async def test_not_found_defers_instead_of_blocking(
        self,
        mock_client: AsyncMock,
        caplog: pytest.LogCaptureFixture,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        # 404 = options endpoint unsupported — guard defer (empty set).
        import logging  # noqa: PLC0415 -- fixture-local import is intentional

        monkeypatch.setattr(logging.getLogger("mcp_server_polarion"), "propagate", True)
        caplog.set_level("WARNING", logger="mcp_server_polarion.tools._shared.guard")
        mock_client.get.side_effect = PolarionNotFoundError(
            "no such endpoint", status_code=404
        )

        options = await fetch_field_options(
            mock_client, "P", "workitems", "severity", "task"
        )

        assert options == {}
        assert any("404" in r.message for r in caplog.records)

    async def test_not_found_result_is_cached(self, mock_client: AsyncMock) -> None:
        # Deferred result cached; missing endpoint not re-probed within TTL.
        mock_client.get.side_effect = PolarionNotFoundError("nope", status_code=404)

        await fetch_field_options(mock_client, "P", "workitems", "severity", "task")
        await fetch_field_options(mock_client, "P", "workitems", "severity", "task")

        assert mock_client.get.await_count == 1

    async def test_guard_defers_when_options_unsupported(
        self, mock_client: AsyncMock
    ) -> None:
        # 404 on options endpoint let enum write through, no raise.
        mock_client.get.side_effect = PolarionNotFoundError("nope", status_code=404)

        await guard_work_item_enums(
            mock_client, "P", "task", severity="anything"
        )  # must not raise

    async def test_unknown_resource_field_returns_empty_mapping(
        self, mock_client: AsyncMock
    ) -> None:
        mock_client.get.return_value = {"data": [], "meta": {"totalCount": 0}}

        options = await fetch_field_options(
            mock_client, "P", "workitems", "weirdField", "task"
        )

        assert options == {}

    async def test_malformed_data_entries_are_skipped(
        self, mock_client: AsyncMock
    ) -> None:
        mock_client.get.return_value = {
            "data": [{"id": "ok"}, "bare-string", {"name": "no-id"}, {"id": ""}],
            "meta": {},
        }

        options = await fetch_field_options(
            mock_client, "P", "workitems", "severity", "task"
        )

        assert options == {"ok": ""}


class TestFetchEnumOptionIds:
    """Single-enumeration GET parsing (dict ``data``) + caching + fail-closed."""

    async def test_first_call_hits_polarion_and_parses_dict_options(
        self, mock_client: AsyncMock
    ) -> None:
        mock_client.get.return_value = project_enum_response(
            "workitem-link-role", ["parent", "relates_to"]
        )

        result = await fetch_enum_option_ids(mock_client, "P", "workitem-link-role")

        assert result == frozenset({"parent", "relates_to"})
        mock_client.get.assert_awaited_once()
        path, kwargs = (
            mock_client.get.call_args.args[0],
            mock_client.get.call_args.kwargs,
        )
        assert path == "/projects/P/enumerations/~/workitem-link-role/~"
        assert kwargs["params"]["fields[enumerations]"] == "@all"

    async def test_second_call_uses_cache(self, mock_client: AsyncMock) -> None:
        mock_client.get.return_value = project_enum_response(
            "hyperlink-role", ["ref_int", "ref_ext"]
        )

        await fetch_enum_option_ids(mock_client, "P", "hyperlink-role")
        await fetch_enum_option_ids(mock_client, "P", "hyperlink-role")

        assert mock_client.get.await_count == 1

    async def test_cache_expiry_re_fetches(
        self, mock_client: AsyncMock, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        mock_client.get.return_value = project_enum_response("hyperlink-role", ["a"])
        clock = [1000.0]
        monkeypatch.setattr(cache_mod, "_now", lambda: clock[0])

        await fetch_enum_option_ids(mock_client, "P", "hyperlink-role")
        clock[0] += cache_mod._GUARD_TTL_SECONDS + 1
        await fetch_enum_option_ids(mock_client, "P", "hyperlink-role")

        assert mock_client.get.await_count == 2

    async def test_polarion_error_blocks_write(self, mock_client: AsyncMock) -> None:
        mock_client.get.side_effect = PolarionError("backend down")

        with pytest.raises(RuntimeError, match="Refusing the write"):
            await fetch_enum_option_ids(mock_client, "P", "workitem-link-role")

    async def test_auth_error_raises_permission_error(
        self, mock_client: AsyncMock
    ) -> None:
        mock_client.get.side_effect = PolarionAuthError("forbidden", status_code=403)

        with pytest.raises(PermissionError, match="lacks permission"):
            await fetch_enum_option_ids(mock_client, "P", "workitem-link-role")

    async def test_not_found_defers_with_empty_set(
        self, mock_client: AsyncMock
    ) -> None:
        mock_client.get.side_effect = PolarionNotFoundError("nope", status_code=404)

        result = await fetch_enum_option_ids(mock_client, "P", "workitem-link-role")

        assert result == frozenset()

    async def test_not_found_result_is_cached(self, mock_client: AsyncMock) -> None:
        mock_client.get.side_effect = PolarionNotFoundError("nope", status_code=404)

        await fetch_enum_option_ids(mock_client, "P", "workitem-link-role")
        await fetch_enum_option_ids(mock_client, "P", "workitem-link-role")

        assert mock_client.get.await_count == 1

    async def test_malformed_data_is_skipped(self, mock_client: AsyncMock) -> None:
        mock_client.get.return_value = {
            "data": {
                "attributes": {
                    "options": ["not-a-dict", {"id": ""}, {"id": "ok"}, {"name": "x"}]
                }
            }
        }

        result = await fetch_enum_option_ids(mock_client, "P", "workitem-link-role")

        assert result == frozenset({"ok"})
