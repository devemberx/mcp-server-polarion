"""Tests for ``tools/_enum_guard.py``.

Covers the two TTL caches (enum-option ids, observed custom-field keys),
the ``fetch_enum_option_ids`` GET + parse path, soft-fail on Polarion error,
and the two write-time guards (``guard_work_item_enums``,
``guard_update_custom_field_keys``).
"""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from mcp_server_polarion.core.client import PolarionClient
from mcp_server_polarion.core.exceptions import PolarionError
from mcp_server_polarion.tools import _enum_guard as guard_mod
from mcp_server_polarion.tools._enum_guard import (
    fetch_enum_option_ids,
    guard_document_enums,
    guard_update_custom_field_keys,
    guard_work_item_enums,
    record_custom_keys_from_get,
)


@pytest.fixture(autouse=True)
def _reset_caches() -> None:
    """Drop any cache state leaked from prior tests in the session."""
    guard_mod._reset_caches_for_tests()


@pytest.fixture
def mock_client() -> AsyncMock:
    client = AsyncMock(spec=PolarionClient)
    client.get = AsyncMock()
    return client


def _enum_response(ids: list[str]) -> dict[str, object]:
    return {
        "data": [{"id": i, "name": i} for i in ids],
        "meta": {"totalCount": len(ids)},
    }


class TestFetchEnumOptionIds:
    """Direct ``getAvailableOptions`` parsing + caching."""

    async def test_first_call_hits_polarion_and_parses_ids(
        self, mock_client: AsyncMock
    ) -> None:
        mock_client.get.return_value = _enum_response(["must_have", "should_have"])

        ids = await fetch_enum_option_ids(
            mock_client, "P", "workitems", "severity", "task"
        )

        assert ids == frozenset({"must_have", "should_have"})
        mock_client.get.assert_awaited_once()
        path, kwargs = (
            mock_client.get.call_args.args[0],
            mock_client.get.call_args.kwargs,
        )
        expected = "/projects/P/workitems/fields/severity/actions/getAvailableOptions"
        assert path == expected
        assert kwargs["params"]["type"] == "task"
        assert kwargs["params"]["page[size]"] == 100

    async def test_second_call_uses_cache(self, mock_client: AsyncMock) -> None:
        mock_client.get.return_value = _enum_response(["a", "b"])

        await fetch_enum_option_ids(mock_client, "P", "workitems", "severity", "task")
        await fetch_enum_option_ids(mock_client, "P", "workitems", "severity", "task")

        assert mock_client.get.await_count == 1

    async def test_cache_expiry_re_fetches(
        self, mock_client: AsyncMock, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        mock_client.get.return_value = _enum_response(["a"])
        clock = [1000.0]
        monkeypatch.setattr(guard_mod, "_now", lambda: clock[0])

        await fetch_enum_option_ids(mock_client, "P", "workitems", "severity", "task")
        clock[0] += 61.0  # past the 60s TTL
        await fetch_enum_option_ids(mock_client, "P", "workitems", "severity", "task")

        assert mock_client.get.await_count == 2

    async def test_polarion_error_returns_none_and_logs(
        self,
        mock_client: AsyncMock,
        caplog: pytest.LogCaptureFixture,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        # ``setup_logging`` (in the production package) sets
        # ``propagate=False`` on ``mcp_server_polarion`` so MCP JSON-RPC
        # over stdout never gets contaminated. caplog hooks the root
        # logger, so once another test has run setup_logging, child
        # warnings never reach caplog. Re-enable propagation locally so
        # this test is order-independent.
        import logging  # noqa: PLC0415 -- fixture-local import is intentional

        monkeypatch.setattr(logging.getLogger("mcp_server_polarion"), "propagate", True)
        caplog.set_level("WARNING", logger="mcp_server_polarion._enum_guard")
        mock_client.get.side_effect = PolarionError("backend down")

        result = await fetch_enum_option_ids(
            mock_client, "P", "workitems", "severity", "task"
        )

        assert result is None
        assert any("enum guard skipped" in r.message for r in caplog.records)

    async def test_unknown_resource_field_returns_empty_set(
        self, mock_client: AsyncMock
    ) -> None:
        mock_client.get.return_value = {"data": [], "meta": {"totalCount": 0}}

        ids = await fetch_enum_option_ids(
            mock_client, "P", "workitems", "weirdField", "task"
        )

        assert ids == frozenset()

    async def test_malformed_data_entries_are_skipped(
        self, mock_client: AsyncMock
    ) -> None:
        mock_client.get.return_value = {
            "data": [{"id": "ok"}, "bare-string", {"name": "no-id"}, {"id": ""}],
            "meta": {},
        }

        ids = await fetch_enum_option_ids(
            mock_client, "P", "workitems", "severity", "task"
        )

        assert ids == frozenset({"ok"})


class TestGuardWorkItemEnums:
    """Validation of each work-item enum argument."""

    async def test_listed_value_passes(self, mock_client: AsyncMock) -> None:
        mock_client.get.return_value = _enum_response(["must_have", "should_have"])

        await guard_work_item_enums(
            mock_client, "P", "task", severity="must_have"
        )  # must not raise

    async def test_unlisted_value_raises_value_error_with_options(
        self, mock_client: AsyncMock
    ) -> None:
        mock_client.get.return_value = _enum_response(["must_have", "should_have"])

        with pytest.raises(ValueError) as exc:
            await guard_work_item_enums(mock_client, "P", "task", severity="ghost")

        msg = str(exc.value)
        assert "severity='ghost'" in msg
        assert "must_have" in msg and "should_have" in msg

    async def test_none_args_skip_all_checks(self, mock_client: AsyncMock) -> None:
        await guard_work_item_enums(mock_client, "P", "task")

        mock_client.get.assert_not_awaited()

    async def test_empty_string_args_skip_checks(self, mock_client: AsyncMock) -> None:
        await guard_work_item_enums(mock_client, "P", "task", status="", severity="")

        mock_client.get.assert_not_awaited()

    async def test_soft_fail_on_polarion_error_lets_write_proceed(
        self, mock_client: AsyncMock
    ) -> None:
        mock_client.get.side_effect = PolarionError("backend down")

        await guard_work_item_enums(
            mock_client, "P", "task", priority="999"
        )  # must not raise

    async def test_type_uses_tilde_axis(self, mock_client: AsyncMock) -> None:
        mock_client.get.return_value = _enum_response(["task", "requirement"])

        await guard_work_item_enums(mock_client, "P", "task", type="task")

        params = mock_client.get.call_args.kwargs["params"]
        assert params["type"] == "~"

    async def test_status_uses_work_item_type_axis(
        self, mock_client: AsyncMock
    ) -> None:
        mock_client.get.return_value = _enum_response(["open", "done"])

        await guard_work_item_enums(mock_client, "P", "task", status="open")

        params = mock_client.get.call_args.kwargs["params"]
        assert params["type"] == "task"


class TestGuardDocumentEnums:
    """Validation of document type / status."""

    async def test_listed_value_passes(self, mock_client: AsyncMock) -> None:
        mock_client.get.return_value = _enum_response(
            ["systemRequirementSpecification"]
        )

        await guard_document_enums(
            mock_client,
            "P",
            "systemRequirementSpecification",
            type="systemRequirementSpecification",
        )

    async def test_unlisted_value_raises(self, mock_client: AsyncMock) -> None:
        mock_client.get.return_value = _enum_response(
            ["systemRequirementSpecification"]
        )

        with pytest.raises(ValueError) as exc:
            await guard_document_enums(
                mock_client,
                "P",
                "systemRequirementSpecification",
                type="productRequirementSpecification",
            )

        assert "productRequirementSpecification" in str(exc.value)


class TestObservedCustomKeysCache:
    """``record_custom_keys_from_get`` plus the read path used by the guard."""

    def test_record_merges_across_calls(self) -> None:
        record_custom_keys_from_get("P", "task", ["k1"])
        record_custom_keys_from_get("P", "task", ["k2", "k1"])

        assert guard_mod._get_cached_custom_keys("P", "task") == frozenset({"k1", "k2"})

    def test_record_filters_non_string_and_empty(self) -> None:
        record_custom_keys_from_get("P", "task", ["k1", "", "k2"])

        assert guard_mod._get_cached_custom_keys("P", "task") == frozenset({"k1", "k2"})

    def test_cache_expiry(self, monkeypatch: pytest.MonkeyPatch) -> None:
        clock = [1000.0]
        monkeypatch.setattr(guard_mod, "_now", lambda: clock[0])
        record_custom_keys_from_get("P", "task", ["k1"])

        clock[0] += 61.0
        assert guard_mod._get_cached_custom_keys("P", "task") is None


class TestGuardUpdateCustomFieldKeys:
    """Validation of ``update_work_item.custom_fields`` keys."""

    async def test_no_custom_fields_short_circuits(
        self, mock_client: AsyncMock
    ) -> None:
        await guard_update_custom_field_keys(mock_client, "P", "MCPT-1", "task", {})

        mock_client.get.assert_not_awaited()

    async def test_known_key_passes_without_inline_get(
        self, mock_client: AsyncMock
    ) -> None:
        record_custom_keys_from_get("P", "task", ["risk_score"])

        await guard_update_custom_field_keys(
            mock_client, "P", "MCPT-1", "task", {"risk_score": 5}
        )

        mock_client.get.assert_not_awaited()

    async def test_unknown_key_raises_with_known_set(
        self, mock_client: AsyncMock
    ) -> None:
        record_custom_keys_from_get("P", "task", ["risk_score"])

        with pytest.raises(ValueError) as exc:
            await guard_update_custom_field_keys(
                mock_client, "P", "MCPT-1", "task", {"release_train_id": "RT-42"}
            )

        msg = str(exc.value)
        assert "release_train_id" in msg
        assert "risk_score" in msg

    async def test_cache_miss_fetches_inline_and_primes_cache(
        self, mock_client: AsyncMock
    ) -> None:
        mock_client.get.return_value = {
            "data": {
                "attributes": {
                    "title": "x",
                    "type": "task",
                    "status": "open",
                    "risk_score": 5,  # custom (not in STANDARD_WORK_ITEM_ATTRIBUTES)
                }
            }
        }

        await guard_update_custom_field_keys(
            mock_client, "P", "MCPT-1", "task", {"risk_score": 5}
        )

        mock_client.get.assert_awaited_once()
        # Cache primed for follow-up.
        assert guard_mod._get_cached_custom_keys("P", "task") == frozenset(
            {"risk_score"}
        )

    async def test_cache_miss_inline_fetch_then_unknown_key_raises(
        self, mock_client: AsyncMock
    ) -> None:
        mock_client.get.return_value = {
            "data": {"attributes": {"title": "x", "risk_score": 5}}
        }

        with pytest.raises(ValueError) as exc:
            await guard_update_custom_field_keys(
                mock_client, "P", "MCPT-1", "task", {"release_train_id": "RT-42"}
            )

        assert "release_train_id" in str(exc.value)

    async def test_soft_fail_on_priming_get_polarion_error(
        self, mock_client: AsyncMock
    ) -> None:
        mock_client.get.side_effect = PolarionError("backend down")

        # No raise: guard skipped, write proceeds.
        await guard_update_custom_field_keys(
            mock_client, "P", "MCPT-1", "task", {"release_train_id": "RT-42"}
        )
