"""Tests for ``tools/_guard.py``.

Covers the ``fetch_enum_option_ids`` GET + parse path, the fail-closed
behaviour on Polarion error (the write is blocked, not skipped), and the
four write-time guards. The TTL caches the guards read from live in
``tools/_cache.py`` and are exercised in ``test_cache.py``.
"""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from mcp_server_polarion.core.client import PolarionClient
from mcp_server_polarion.core.exceptions import PolarionError, PolarionNotFoundError
from mcp_server_polarion.models import WorkItemLinkSpec
from mcp_server_polarion.tools import _cache as cache_mod
from mcp_server_polarion.tools._cache import (
    record_document_custom_field_keys,
    record_work_item_custom_field_keys,
)
from mcp_server_polarion.tools._guard import (
    fetch_enum_option_ids,
    guard_document_custom_field_keys,
    guard_document_enums,
    guard_work_item_custom_field_keys,
    guard_work_item_enums,
    guard_work_item_link_targets,
    partition_delete_links,
)


@pytest.fixture(autouse=True)
def _reset_caches() -> None:
    """Drop any cache state leaked from prior tests in the session."""
    cache_mod._enum_option_cache.clear()
    cache_mod._work_item_custom_key_cache.clear()
    cache_mod._document_custom_key_cache.clear()


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
        monkeypatch.setattr(cache_mod, "_now", lambda: clock[0])

        await fetch_enum_option_ids(mock_client, "P", "workitems", "severity", "task")
        clock[0] += 61.0  # past the 60s TTL
        await fetch_enum_option_ids(mock_client, "P", "workitems", "severity", "task")

        assert mock_client.get.await_count == 2

    async def test_polarion_error_blocks_write_and_logs(
        self,
        mock_client: AsyncMock,
        caplog: pytest.LogCaptureFixture,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        # ``setup_logging`` sets ``propagate=False`` on the package logger so
        # MCP JSON-RPC over stdout never gets contaminated. caplog hooks the
        # root logger, so once another test ran setup_logging, child warnings
        # never reach caplog. Re-enable propagation locally for order
        # independence.
        import logging  # noqa: PLC0415 -- fixture-local import is intentional

        monkeypatch.setattr(logging.getLogger("mcp_server_polarion"), "propagate", True)
        caplog.set_level("WARNING", logger="mcp_server_polarion._guard")
        mock_client.get.side_effect = PolarionError("backend down")

        with pytest.raises(RuntimeError, match="Refusing the write"):
            await fetch_enum_option_ids(
                mock_client, "P", "workitems", "severity", "task"
            )

        assert any("blocking write" in r.message for r in caplog.records)

    async def test_not_found_defers_instead_of_blocking(
        self,
        mock_client: AsyncMock,
        caplog: pytest.LogCaptureFixture,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        # A 404 means getAvailableOptions is unsupported on this instance, so
        # the guard defers (empty set) rather than block every enum write.
        import logging  # noqa: PLC0415 -- fixture-local import is intentional

        monkeypatch.setattr(logging.getLogger("mcp_server_polarion"), "propagate", True)
        caplog.set_level("WARNING", logger="mcp_server_polarion._guard")
        mock_client.get.side_effect = PolarionNotFoundError(
            "no such endpoint", status_code=404
        )

        ids = await fetch_enum_option_ids(
            mock_client, "P", "workitems", "severity", "task"
        )

        assert ids == frozenset()
        assert any("404" in r.message for r in caplog.records)

    async def test_not_found_result_is_cached(self, mock_client: AsyncMock) -> None:
        # The deferred (empty) result is cached so a missing endpoint is not
        # re-probed on every write within the TTL.
        mock_client.get.side_effect = PolarionNotFoundError("nope", status_code=404)

        await fetch_enum_option_ids(mock_client, "P", "workitems", "severity", "task")
        await fetch_enum_option_ids(mock_client, "P", "workitems", "severity", "task")

        assert mock_client.get.await_count == 1

    async def test_guard_defers_when_options_unsupported(
        self, mock_client: AsyncMock
    ) -> None:
        # End-to-end: a 404 on the options endpoint must NOT raise from the
        # higher-level guard -- the enum-bearing write is allowed through.
        mock_client.get.side_effect = PolarionNotFoundError("nope", status_code=404)

        await guard_work_item_enums(
            mock_client, "P", "task", severity="anything"
        )  # must not raise

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

    async def test_polarion_error_blocks_write(self, mock_client: AsyncMock) -> None:
        mock_client.get.side_effect = PolarionError("backend down")

        with pytest.raises(RuntimeError, match="Refusing the write"):
            await guard_work_item_enums(mock_client, "P", "task", priority="999")

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

    async def test_listed_resolution_passes(self, mock_client: AsyncMock) -> None:
        mock_client.get.return_value = _enum_response(["done", "wontfix"])

        await guard_work_item_enums(
            mock_client, "P", "task", resolution="done"
        )  # must not raise

        params = mock_client.get.call_args.kwargs["params"]
        assert params["type"] == "task"

    async def test_unlisted_resolution_raises(self, mock_client: AsyncMock) -> None:
        mock_client.get.return_value = _enum_response(["done", "wontfix"])

        with pytest.raises(ValueError) as exc:
            await guard_work_item_enums(mock_client, "P", "task", resolution="ghost")

        assert "resolution='ghost'" in str(exc.value)


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


class TestRecordWorkItemCustomKeys:
    """``record_work_item_custom_field_keys`` plus the cache read it feeds."""

    def test_record_merges_across_calls(self) -> None:
        record_work_item_custom_field_keys("P", "task", ["k1"])
        record_work_item_custom_field_keys("P", "task", ["k2", "k1"])

        assert cache_mod._work_item_custom_key_cache.get(("P", "task")) == frozenset(
            {"k1", "k2"}
        )

    def test_record_filters_non_string_and_empty(self) -> None:
        record_work_item_custom_field_keys("P", "task", ["k1", "", "k2"])

        assert cache_mod._work_item_custom_key_cache.get(("P", "task")) == frozenset(
            {"k1", "k2"}
        )

    def test_cache_expiry(self, monkeypatch: pytest.MonkeyPatch) -> None:
        clock = [1000.0]
        monkeypatch.setattr(cache_mod, "_now", lambda: clock[0])
        record_work_item_custom_field_keys("P", "task", ["k1"])

        clock[0] += 61.0
        assert cache_mod._work_item_custom_key_cache.get(("P", "task")) is None


class TestGuardWorkItemCustomFieldKeys:
    """Validation of ``update_work_item.custom_fields`` keys."""

    async def test_no_custom_fields_short_circuits(
        self, mock_client: AsyncMock
    ) -> None:
        await guard_work_item_custom_field_keys(mock_client, "P", "MCPT-1", "task", {})

        mock_client.get.assert_not_awaited()

    async def test_known_key_passes_without_inline_get(
        self, mock_client: AsyncMock
    ) -> None:
        record_work_item_custom_field_keys("P", "task", ["risk_score"])

        await guard_work_item_custom_field_keys(
            mock_client, "P", "MCPT-1", "task", {"risk_score": 5}
        )

        mock_client.get.assert_not_awaited()

    async def test_unknown_key_raises_with_known_set(
        self, mock_client: AsyncMock
    ) -> None:
        record_work_item_custom_field_keys("P", "task", ["risk_score"])

        with pytest.raises(ValueError) as exc:
            await guard_work_item_custom_field_keys(
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

        await guard_work_item_custom_field_keys(
            mock_client, "P", "MCPT-1", "task", {"risk_score": 5}
        )

        mock_client.get.assert_awaited_once()
        assert cache_mod._work_item_custom_key_cache.get(("P", "task")) == frozenset(
            {"risk_score"}
        )

    async def test_cache_miss_inline_fetch_then_unknown_key_raises(
        self, mock_client: AsyncMock
    ) -> None:
        mock_client.get.return_value = {
            "data": {"attributes": {"title": "x", "risk_score": 5}}
        }

        with pytest.raises(ValueError) as exc:
            await guard_work_item_custom_field_keys(
                mock_client, "P", "MCPT-1", "task", {"release_train_id": "RT-42"}
            )

        assert "release_train_id" in str(exc.value)

    async def test_priming_get_error_blocks_write(self, mock_client: AsyncMock) -> None:
        mock_client.get.side_effect = PolarionError("backend down")

        with pytest.raises(RuntimeError, match="Refusing the write"):
            await guard_work_item_custom_field_keys(
                mock_client, "P", "MCPT-1", "task", {"release_train_id": "RT-42"}
            )


class TestGuardDocumentCustomFieldKeys:
    """Validation of ``update_document.custom_fields`` keys."""

    async def test_no_custom_fields_short_circuits(
        self, mock_client: AsyncMock
    ) -> None:
        await guard_document_custom_field_keys(mock_client, "P", "_default", "Doc", {})

        mock_client.get.assert_not_awaited()

    async def test_known_recorded_key_passes_without_inline_get(
        self, mock_client: AsyncMock
    ) -> None:
        record_document_custom_field_keys("P", "_default", "Doc", ["doc_risk"])

        await guard_document_custom_field_keys(
            mock_client, "P", "_default", "Doc", {"doc_risk": 3}
        )

        mock_client.get.assert_not_awaited()

    async def test_cache_miss_fetches_inline_and_primes_cache(
        self, mock_client: AsyncMock
    ) -> None:
        mock_client.get.return_value = {
            "data": {"attributes": {"title": "x", "type": "generic", "doc_risk": 3}}
        }

        await guard_document_custom_field_keys(
            mock_client, "P", "_default", "Doc", {"doc_risk": 3}
        )

        mock_client.get.assert_awaited_once()
        path = mock_client.get.call_args.args[0]
        assert path == "/projects/P/spaces/_default/documents/Doc"
        assert cache_mod._document_custom_key_cache.get(
            ("P", "_default", "Doc")
        ) == frozenset({"doc_risk"})

    async def test_unknown_key_raises_with_known_set(
        self, mock_client: AsyncMock
    ) -> None:
        mock_client.get.return_value = {
            "data": {"attributes": {"title": "x", "doc_risk": 3}}
        }

        with pytest.raises(ValueError) as exc:
            await guard_document_custom_field_keys(
                mock_client, "P", "_default", "Doc", {"ghost_key": 1}
            )

        msg = str(exc.value)
        assert "ghost_key" in msg
        assert "doc_risk" in msg

    async def test_priming_get_error_blocks_write(self, mock_client: AsyncMock) -> None:
        mock_client.get.side_effect = PolarionError("backend down")

        with pytest.raises(RuntimeError, match="Refusing the write"):
            await guard_document_custom_field_keys(
                mock_client, "P", "_default", "Doc", {"ghost_key": 1}
            )


def _workitems_response(project_id: str, short_ids: list[str]) -> dict[str, object]:
    """A JSON:API workitems list response (ids are ``project/short``)."""
    return {
        "data": [{"type": "workitems", "id": f"{project_id}/{i}"} for i in short_ids],
        "meta": {"totalCount": len(short_ids)},
    }


def _link(target: str, *, project: str | None = None) -> WorkItemLinkSpec:
    return WorkItemLinkSpec(
        role="relates_to", target_work_item_id=target, target_project_id=project
    )


class TestGuardWorkItemLinkTargets:
    """Target-existence guard for ``create_work_item_links`` (uncached)."""

    async def test_all_targets_exist_one_get_per_project(
        self, mock_client: AsyncMock
    ) -> None:
        mock_client.get.return_value = _workitems_response("P", ["A", "B"])

        await guard_work_item_link_targets(mock_client, "P", [_link("A"), _link("B")])

        mock_client.get.assert_awaited_once()
        path, kwargs = (
            mock_client.get.call_args.args[0],
            mock_client.get.call_args.kwargs,
        )
        assert path == "/projects/P/workitems"
        assert kwargs["params"]["query"] == "id:(A B)"
        assert kwargs["params"]["fields[workitems]"] == "id"

    async def test_missing_target_raises_value_error(
        self, mock_client: AsyncMock
    ) -> None:
        mock_client.get.return_value = _workitems_response("P", ["A"])

        with pytest.raises(ValueError, match="P/B") as exc:
            await guard_work_item_link_targets(
                mock_client, "P", [_link("A"), _link("B")]
            )

        assert "dangling" in str(exc.value)

    async def test_cross_project_two_gets(self, mock_client: AsyncMock) -> None:
        responses = {
            "P": _workitems_response("P", ["A"]),
            "Q": _workitems_response("Q", ["X"]),
        }

        async def fake_get(path: str, **kwargs: object) -> dict[str, object]:
            project = path.split("/")[2]
            return responses[project]

        mock_client.get.side_effect = fake_get

        await guard_work_item_link_targets(
            mock_client, "P", [_link("A"), _link("X", project="Q")]
        )

        assert mock_client.get.await_count == 2

    async def test_missing_in_cross_project_is_caught(
        self, mock_client: AsyncMock
    ) -> None:
        async def fake_get(path: str, **kwargs: object) -> dict[str, object]:
            project = path.split("/")[2]
            return _workitems_response(project, ["A"] if project == "P" else [])

        mock_client.get.side_effect = fake_get

        with pytest.raises(ValueError, match="Q/X"):
            await guard_work_item_link_targets(
                mock_client, "P", [_link("A"), _link("X", project="Q")]
            )

    async def test_chunks_above_page_size(self, mock_client: AsyncMock) -> None:
        ids = sorted(f"WI-{n}" for n in range(150))

        async def fake_get(path: str, **kwargs: object) -> dict[str, object]:
            query = str(kwargs["params"]["query"])  # type: ignore[index]
            chunk = query.removeprefix("id:(").removesuffix(")").split()
            return _workitems_response("P", chunk)

        mock_client.get.side_effect = fake_get

        await guard_work_item_link_targets(mock_client, "P", [_link(i) for i in ids])

        assert mock_client.get.await_count == 2
        queries = [
            str(call.kwargs["params"]["query"])
            for call in mock_client.get.await_args_list
        ]
        assert [q.count(" ") + 1 for q in queries] == [100, 50]

    async def test_unreachable_backend_blocks_write(
        self, mock_client: AsyncMock
    ) -> None:
        mock_client.get.side_effect = PolarionError("backend down")

        with pytest.raises(RuntimeError, match="Refusing the write"):
            await guard_work_item_link_targets(mock_client, "P", [_link("A")])

    async def test_missing_target_project_raises_value_error(
        self, mock_client: AsyncMock
    ) -> None:
        mock_client.get.side_effect = PolarionNotFoundError("no such project")

        with pytest.raises(ValueError, match="P/A"):
            await guard_work_item_link_targets(mock_client, "P", [_link("A")])


def _linkedworkitems_response(composite_ids: list[str]) -> dict[str, object]:
    """A JSON:API forward-link page; ids are the 5-segment composite form."""
    return {
        "data": [{"type": "linkedworkitems", "id": cid} for cid in composite_ids],
        "meta": {"totalCount": len(composite_ids)},
    }


class TestPartitionDeleteLinks:
    """Pre-read + matched/no-op split for ``delete_work_item_links``."""

    async def test_splits_matched_and_not_found_preserving_order(
        self, mock_client: AsyncMock
    ) -> None:
        mock_client.get.return_value = _linkedworkitems_response(
            ["P/MCPT-1/parent/P/MCPT-2", "P/MCPT-1/relates_to/P/MCPT-9"]
        )

        matched, not_found = await partition_delete_links(
            mock_client,
            "P",
            "MCPT-1",
            [
                "P/MCPT-1/relates_to/P/MCPT-9",
                "P/MCPT-1/verifies/P/MCPT-3",
                "P/MCPT-1/parent/P/MCPT-2",
            ],
        )

        assert matched == [
            "P/MCPT-1/relates_to/P/MCPT-9",
            "P/MCPT-1/parent/P/MCPT-2",
        ]
        assert not_found == ["P/MCPT-1/verifies/P/MCPT-3"]

    async def test_reads_only_id_field(self, mock_client: AsyncMock) -> None:
        mock_client.get.return_value = _linkedworkitems_response([])

        await partition_delete_links(
            mock_client, "P", "MCPT-1", ["P/MCPT-1/parent/P/MCPT-2"]
        )

        path, kwargs = (
            mock_client.get.call_args.args[0],
            mock_client.get.call_args.kwargs,
        )
        assert path == "/projects/P/workitems/MCPT-1/linkedworkitems"
        assert kwargs["params"]["fields[linkedworkitems]"] == "id"

    async def test_paginates_above_page_size(self, mock_client: AsyncMock) -> None:
        full = [f"P/MCPT-1/relates_to/P/WI-{n}" for n in range(100)]
        tail = ["P/MCPT-1/relates_to/P/WI-100"]

        async def fake_get(path: str, **kwargs: object) -> dict[str, object]:
            page = kwargs["params"]["page[number]"]  # type: ignore[index]
            return _linkedworkitems_response(full if page == 1 else tail)

        mock_client.get.side_effect = fake_get

        matched, not_found = await partition_delete_links(
            mock_client,
            "P",
            "MCPT-1",
            ["P/MCPT-1/relates_to/P/WI-100"],
        )

        assert mock_client.get.await_count == 2
        assert matched == ["P/MCPT-1/relates_to/P/WI-100"]
        assert not_found == []

    async def test_source_wi_404_raises_value_error(
        self, mock_client: AsyncMock
    ) -> None:
        mock_client.get.side_effect = PolarionNotFoundError("not found")

        with pytest.raises(ValueError, match="Source work item 'MCPT-1' not found"):
            await partition_delete_links(
                mock_client,
                "P",
                "MCPT-1",
                ["P/MCPT-1/parent/P/MCPT-2"],
            )

    async def test_unreachable_backend_blocks_with_runtime_error(
        self, mock_client: AsyncMock
    ) -> None:
        mock_client.get.side_effect = PolarionError("backend down")

        with pytest.raises(RuntimeError, match="Refusing the write"):
            await partition_delete_links(
                mock_client,
                "P",
                "MCPT-1",
                ["P/MCPT-1/parent/P/MCPT-2"],
            )
