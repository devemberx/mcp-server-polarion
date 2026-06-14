"""Guard tests: fetch/parse path, fail-closed on Polarion error (write
blocked, not skipped), and the write-time guards. Caches tested in
``test_cache.py``.
"""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from mcp_server_polarion.core.client import PolarionClient
from mcp_server_polarion.core.exceptions import (
    PolarionAuthError,
    PolarionError,
    PolarionNotFoundError,
)
from mcp_server_polarion.models import WorkItemLinkSpec
from mcp_server_polarion.tools._shared import cache as cache_mod
from mcp_server_polarion.tools._shared.cache import (
    store_document_type_custom_keys,
    store_work_item_custom_keys,
)
from mcp_server_polarion.tools._shared.guard import (
    _GUARD_PAGE_SIZE,
    _check_document_custom_keys,
    _check_work_item_custom_keys,
    fetch_enum_option_ids,
    fetch_project_enum_option_ids,
    guard_document_custom_fields,
    guard_document_enums,
    guard_hyperlink_roles,
    guard_work_item_custom_fields,
    guard_work_item_enums,
    guard_work_item_link_roles,
    guard_work_item_link_targets,
    partition_delete_links,
)


@pytest.fixture(autouse=True)
def _reset_caches() -> None:
    """Drop any cache state leaked from prior tests in the session."""
    cache_mod._enum_option_cache.clear()
    cache_mod._project_enum_cache.clear()
    cache_mod._work_item_custom_key_cache.clear()
    cache_mod._document_type_custom_key_cache.clear()


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
        # setup_logging sets propagate=False, so caplog misses package logs;
        # re-enable propagation locally for order independence.
        import logging  # noqa: PLC0415 -- fixture-local import is intentional

        monkeypatch.setattr(logging.getLogger("mcp_server_polarion"), "propagate", True)
        caplog.set_level("WARNING", logger="mcp_server_polarion.tools._shared.guard")
        mock_client.get.side_effect = PolarionError("backend down")

        with pytest.raises(RuntimeError, match="Refusing the write"):
            await fetch_enum_option_ids(
                mock_client, "P", "workitems", "severity", "task"
            )

        assert any("blocking write" in r.message for r in caplog.records)

    async def test_auth_error_raises_permission_error(
        self, mock_client: AsyncMock
    ) -> None:
        mock_client.get.side_effect = PolarionAuthError("forbidden", status_code=403)

        with pytest.raises(PermissionError, match="lacks permission"):
            await fetch_enum_option_ids(
                mock_client, "P", "workitems", "severity", "task"
            )

    async def test_not_found_defers_instead_of_blocking(
        self,
        mock_client: AsyncMock,
        caplog: pytest.LogCaptureFixture,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        # 404 = options endpoint unsupported, so the guard defers (empty set).
        import logging  # noqa: PLC0415 -- fixture-local import is intentional

        monkeypatch.setattr(logging.getLogger("mcp_server_polarion"), "propagate", True)
        caplog.set_level("WARNING", logger="mcp_server_polarion.tools._shared.guard")
        mock_client.get.side_effect = PolarionNotFoundError(
            "no such endpoint", status_code=404
        )

        ids = await fetch_enum_option_ids(
            mock_client, "P", "workitems", "severity", "task"
        )

        assert ids == frozenset()
        assert any("404" in r.message for r in caplog.records)

    async def test_not_found_result_is_cached(self, mock_client: AsyncMock) -> None:
        # Deferred result is cached; a missing endpoint isn't re-probed in the TTL.
        mock_client.get.side_effect = PolarionNotFoundError("nope", status_code=404)

        await fetch_enum_option_ids(mock_client, "P", "workitems", "severity", "task")
        await fetch_enum_option_ids(mock_client, "P", "workitems", "severity", "task")

        assert mock_client.get.await_count == 1

    async def test_guard_defers_when_options_unsupported(
        self, mock_client: AsyncMock
    ) -> None:
        # A 404 on the options endpoint lets the enum write through, no raise.
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


def _wi_list(*attrs: dict[str, object]) -> dict[str, object]:
    """JSON:API list response of work items with the given ``attributes`` dicts."""
    return {
        "data": [
            {"type": "workitems", "id": f"MCPT-{i}", "attributes": a}
            for i, a in enumerate(attrs)
        ]
    }


class TestGuardWorkItemCustomFieldKeys:
    """Validation of ``custom_fields`` keys via the MIN-per-key type sample."""

    async def test_no_custom_fields_short_circuits(
        self, mock_client: AsyncMock
    ) -> None:
        await guard_work_item_custom_fields(mock_client, "P", "task", {})

        mock_client.get.assert_not_awaited()

    async def test_cached_schema_passes_without_sample(
        self, mock_client: AsyncMock
    ) -> None:
        store_work_item_custom_keys("P", "task", frozenset({"risk_score"}))

        await _check_work_item_custom_keys(mock_client, "P", "task", {"risk_score": 5})

        mock_client.get.assert_not_awaited()

    async def test_sql_sample_primes_schema_and_passes(
        self, mock_client: AsyncMock
    ) -> None:
        mock_client.get.return_value = _wi_list(
            {"title": "a", "type": "task", "risk_score": 5},
            {"title": "b", "type": "task", "release_train_id": "RT-1"},
        )

        await _check_work_item_custom_keys(
            mock_client, "P", "task", {"risk_score": 9, "release_train_id": "RT-9"}
        )

        mock_client.get.assert_awaited_once()
        # Primary path issues the MIN-per-key SQL with @all, not a per-item GET.
        params = mock_client.get.await_args.kwargs["params"]
        assert params["query"].startswith("SQL:(SELECT")
        assert "GROUP BY cf.c_name" in params["query"]
        assert params["fields[workitems]"] == "@all"
        assert cache_mod._work_item_custom_key_cache.get(("P", "task")) == frozenset(
            {"risk_score", "release_train_id"}
        )

    async def test_paginates_beyond_first_page_of_keys(
        self, mock_client: AsyncMock
    ) -> None:
        # A type with >100 distinct keys spans pages; the union must span them too,
        # else a key on page 2+ would be false-rejected.
        page1 = _wi_list(
            *(
                {"title": "x", "type": "task", f"k{i}": 1}
                for i in range(_GUARD_PAGE_SIZE)
            )
        )
        page2 = _wi_list({"title": "y", "type": "task", "late_key": 9})
        mock_client.get.side_effect = [page1, page2]

        await _check_work_item_custom_keys(mock_client, "P", "task", {"late_key": 9})

        # Full page 1 (==100) forces page 2; short page 2 stops the loop.
        assert mock_client.get.await_count == 2
        schema = cache_mod._work_item_custom_key_cache.get(("P", "task"))
        assert schema is not None
        assert "late_key" in schema
        assert "k0" in schema
        assert len(schema) == _GUARD_PAGE_SIZE + 1

    async def test_unknown_key_against_fresh_sample_rejects_without_retry(
        self, mock_client: AsyncMock
    ) -> None:
        # Cold cache: the sample is already current, so an unknown key is rejected
        # straight away -- no redundant second fetch.
        mock_client.get.return_value = _wi_list(
            {"title": "a", "type": "task", "risk_score": 5}
        )

        with pytest.raises(ValueError) as exc:
            await _check_work_item_custom_keys(
                mock_client, "P", "task", {"release_train_id": "RT-42"}
            )

        msg = str(exc.value)
        assert "release_train_id" in msg
        assert "risk_score" in msg
        mock_client.get.assert_awaited_once()

    async def test_empty_sample_fails_closed(self, mock_client: AsyncMock) -> None:
        mock_client.get.return_value = {"data": []}

        with pytest.raises(RuntimeError, match="Refusing the write"):
            await _check_work_item_custom_keys(
                mock_client, "P", "task", {"risk_score": 5}
            )

    async def test_sql_rejection_fails_closed(self, mock_client: AsyncMock) -> None:
        # No Lucene fallback: a rejected SQL sample blocks the write rather than
        # validating against an incomplete schema.
        mock_client.get.side_effect = PolarionError("SQL not supported")

        with pytest.raises(RuntimeError, match="Refusing the write"):
            await _check_work_item_custom_keys(
                mock_client, "P", "task", {"risk_score": 9}
            )

        mock_client.get.assert_awaited_once()

    async def test_cached_schema_unknown_key_refetches_then_passes(
        self, mock_client: AsyncMock
    ) -> None:
        # A key unknown against a *cached* (possibly stale) schema triggers one
        # fresh re-fetch; the admin-added field now present, the write passes.
        store_work_item_custom_keys("P", "task", frozenset({"risk_score"}))
        mock_client.get.return_value = _wi_list(
            {"title": "a", "type": "task", "risk_score": 5},
            {"title": "b", "type": "task", "release_train_id": "RT-1"},
        )

        await _check_work_item_custom_keys(
            mock_client, "P", "task", {"release_train_id": "RT-9"}
        )

        mock_client.get.assert_awaited_once()

    async def test_sample_error_blocks_write(self, mock_client: AsyncMock) -> None:
        # The SQL sample fails -> fail-closed.
        mock_client.get.side_effect = PolarionError("backend down")

        with pytest.raises(RuntimeError, match="Refusing the write"):
            await _check_work_item_custom_keys(
                mock_client, "P", "task", {"release_train_id": "RT-42"}
            )

    async def test_sample_auth_error_raises_permission_error(
        self, mock_client: AsyncMock
    ) -> None:
        mock_client.get.side_effect = PolarionAuthError("forbidden", status_code=403)

        with pytest.raises(PermissionError, match="lacks permission"):
            await _check_work_item_custom_keys(
                mock_client, "P", "task", {"release_train_id": "RT-42"}
            )


def _docs_list(*docs: tuple[str, dict[str, object]]) -> dict[str, object]:
    """Heading + ``include=module`` sample: one module per document in ``included``.

    ``data`` rows are bare heading placeholders that only drive pagination; the
    document type + customs ride in the ``included`` module resources.
    """
    return {
        "data": [{"type": "workitems"} for _ in docs],
        "included": [
            {
                "type": "documents",
                "id": f"P/_default/D{i}",
                "attributes": {"title": "t", "type": dtype, **customs},
            }
            for i, (dtype, customs) in enumerate(docs)
        ],
        "meta": {"totalCount": len(docs)},
    }


class TestGuardDocumentCustomFieldKeys:
    """Validation of ``custom_fields`` keys via the project-wide document sample."""

    async def test_no_custom_fields_short_circuits(
        self, mock_client: AsyncMock
    ) -> None:
        await guard_document_custom_fields(mock_client, "P", "generic", {})

        mock_client.get.assert_not_awaited()

    async def test_cached_schema_passes_without_sample(
        self, mock_client: AsyncMock
    ) -> None:
        store_document_type_custom_keys("P", "generic", frozenset({"doc_risk"}))

        await _check_document_custom_keys(mock_client, "P", "generic", {"doc_risk": 3})

        mock_client.get.assert_not_awaited()

    async def test_sample_primes_schema_and_passes(
        self, mock_client: AsyncMock
    ) -> None:
        # Customs are grouped per type across the whole project in one GET.
        mock_client.get.return_value = _docs_list(
            ("generic", {"doc_risk": 3}),
            ("generic", {"owner": "x"}),
            ("systemReqSpecification", {"version": "1.0"}),
        )

        await _check_document_custom_keys(
            mock_client, "P", "generic", {"doc_risk": 9, "owner": "y"}
        )

        mock_client.get.assert_awaited_once()
        params = mock_client.get.call_args.kwargs["params"]
        path = mock_client.get.call_args.args[0]
        assert path == "/projects/P/workitems"
        # Heading-discovery SQL + include=module surfaces each doc's type+customs.
        assert params["query"].startswith("SQL:(")
        assert params["include"] == "module"
        assert params["fields[documents]"] == "@all"
        # Every type's schema is stored from the one fetch.
        assert cache_mod._document_type_custom_key_cache.get(
            ("P", "systemReqSpecification")
        ) == frozenset({"version"})

    async def test_unknown_key_against_fresh_sample_rejects_without_retry(
        self, mock_client: AsyncMock
    ) -> None:
        mock_client.get.return_value = _docs_list(("generic", {"doc_risk": 3}))

        with pytest.raises(ValueError) as exc:
            await _check_document_custom_keys(
                mock_client, "P", "generic", {"ghost_key": 1}
            )

        msg = str(exc.value)
        assert "ghost_key" in msg
        assert "doc_risk" in msg
        mock_client.get.assert_awaited_once()

    async def test_cached_schema_unknown_key_refetches_then_passes(
        self, mock_client: AsyncMock
    ) -> None:
        store_document_type_custom_keys("P", "generic", frozenset({"doc_risk"}))
        mock_client.get.return_value = _docs_list(
            ("generic", {"doc_risk": 3, "new_field": 1})
        )

        await _check_document_custom_keys(mock_client, "P", "generic", {"new_field": 1})

        mock_client.get.assert_awaited_once()

    async def test_empty_sample_fails_closed(self, mock_client: AsyncMock) -> None:
        # No document of this type has any custom -> schema empty -> block.
        mock_client.get.return_value = _docs_list(("systemReqSpecification", {"v": 1}))

        with pytest.raises(RuntimeError, match="Refusing the write"):
            await _check_document_custom_keys(
                mock_client, "P", "generic", {"doc_risk": 3}
            )

    async def test_sample_error_blocks_write(self, mock_client: AsyncMock) -> None:
        mock_client.get.side_effect = PolarionError("backend down")

        with pytest.raises(RuntimeError, match="Refusing the write"):
            await _check_document_custom_keys(
                mock_client, "P", "generic", {"ghost_key": 1}
            )

    async def test_sample_auth_error_raises_permission_error(
        self, mock_client: AsyncMock
    ) -> None:
        mock_client.get.side_effect = PolarionAuthError("forbidden", status_code=403)

        with pytest.raises(PermissionError, match="lacks permission"):
            await _check_document_custom_keys(
                mock_client, "P", "generic", {"ghost_key": 1}
            )


class TestGuardWorkItemCustomFieldEnums:
    """Enum-value stage of ``guard_work_item_custom_fields``.

    The key stage is covered by ``TestGuardWorkItemCustomFieldKeys``; schemas
    are primed here so each test exercises only the enum-value checks.
    """

    @pytest.fixture(autouse=True)
    def _prime_key_schemas(self, _reset_caches: None) -> None:
        store_work_item_custom_keys("P", "softwarerequirement", frozenset({"asil"}))
        store_work_item_custom_keys(
            "P", "task", frozenset({"a", "asil", "f", "ftti", "other", "platform"})
        )

    async def test_unknown_key_rejected_before_enum_probe(
        self, mock_client: AsyncMock
    ) -> None:
        # Key stage runs first: a ghost key never reaches getAvailableOptions,
        # so it cannot plant a long-lived 404 entry in the enum cache.
        mock_client.get.return_value = _wi_list(
            {"title": "a", "type": "task", "asil": "1"}
        )

        with pytest.raises(ValueError, match="ghost_key"):
            await guard_work_item_custom_fields(
                mock_client, "P", "task", {"ghost_key": "x"}
            )

        for call in mock_client.get.call_args_list:
            assert "getAvailableOptions" not in call.args[0]

    async def test_valid_option_id_passes(self, mock_client: AsyncMock) -> None:
        mock_client.get.return_value = _enum_response(["1", "2", "3", "4"])

        await guard_work_item_custom_fields(
            mock_client, "P", "softwarerequirement", {"asil": "4"}
        )  # must not raise

    async def test_unknown_option_id_raises_with_options(
        self, mock_client: AsyncMock
    ) -> None:
        mock_client.get.return_value = _enum_response(["1", "2", "3", "4"])

        with pytest.raises(ValueError, match=r"'asil'.*'9'.*\['1', '2', '3', '4'\]"):
            await guard_work_item_custom_fields(
                mock_client, "P", "softwarerequirement", {"asil": "9"}
            )

    async def test_non_enum_field_defers_on_404(self, mock_client: AsyncMock) -> None:
        # Polarion: "Field 'X' is not an Enumeration field." -- nothing to check.
        mock_client.get.side_effect = PolarionNotFoundError("not enum", status_code=404)

        await guard_work_item_custom_fields(
            mock_client, "P", "task", {"ftti": 1000}
        )  # must not raise

    async def test_non_string_value_on_enum_field_raises(
        self, mock_client: AsyncMock
    ) -> None:
        # Option ids are strings; the int 4 would ghost even though '4' is valid.
        mock_client.get.return_value = _enum_response(["1", "2", "3", "4"])

        with pytest.raises(ValueError, match="int 4"):
            await guard_work_item_custom_fields(
                mock_client, "P", "softwarerequirement", {"asil": 4}
            )

    async def test_dict_value_on_enum_field_raises(
        self, mock_client: AsyncMock
    ) -> None:
        mock_client.get.return_value = _enum_response(["1", "2"])

        with pytest.raises(ValueError, match="dict"):
            await guard_work_item_custom_fields(
                mock_client, "P", "task", {"asil": {"id": "1"}}
            )

    async def test_list_of_valid_options_passes(self, mock_client: AsyncMock) -> None:
        mock_client.get.return_value = _enum_response(["windows", "linux", "osx"])

        await guard_work_item_custom_fields(
            mock_client, "P", "task", {"platform": ["windows", "linux"]}
        )  # must not raise

    async def test_list_with_unknown_option_raises(
        self, mock_client: AsyncMock
    ) -> None:
        mock_client.get.return_value = _enum_response(["windows", "linux"])

        with pytest.raises(ValueError, match="'beos'"):
            await guard_work_item_custom_fields(
                mock_client, "P", "task", {"platform": ["windows", "beos"]}
            )

    async def test_list_with_non_string_element_raises(
        self, mock_client: AsyncMock
    ) -> None:
        mock_client.get.return_value = _enum_response(["1", "2"])

        with pytest.raises(ValueError, match="int 2"):
            await guard_work_item_custom_fields(
                mock_client, "P", "task", {"asil": ["1", 2]}
            )

    async def test_empty_values_skip_probe_entirely(
        self, mock_client: AsyncMock
    ) -> None:
        # Payload builders drop empties; nothing reaches Polarion to ghost,
        # so the guard must not even spend the probe GET.
        await guard_work_item_custom_fields(
            mock_client, "P", "task", {"asil": "", "other": None, "platform": []}
        )

        mock_client.get.assert_not_awaited()

    async def test_options_fetched_once_per_key_within_ttl(
        self, mock_client: AsyncMock
    ) -> None:
        mock_client.get.return_value = _enum_response(["1", "2"])

        await guard_work_item_custom_fields(mock_client, "P", "task", {"a": "1"})
        await guard_work_item_custom_fields(mock_client, "P", "task", {"a": "2"})

        assert mock_client.get.await_count == 1

    async def test_not_found_outlives_guard_ttl(
        self, mock_client: AsyncMock, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # 404 entries get the long not_found TTL; positive sets keep 60s.
        mock_client.get.side_effect = PolarionNotFoundError("not enum", status_code=404)
        clock = [1000.0]
        monkeypatch.setattr(cache_mod, "_now", lambda: clock[0])
        # The fixture primed the key schema under the real monotonic clock; on
        # a freshly booted host its expiry can precede 1000.0. Re-prime under
        # the patched clock so only the enum cache's expiry is measured.
        store_work_item_custom_keys("P", "task", frozenset({"f"}))

        await guard_work_item_custom_fields(mock_client, "P", "task", {"f": "x"})
        clock[0] += 61.0  # past _GUARD_TTL_SECONDS, within not_found TTL
        # The key schema shares the 60s TTL; re-prime so only the enum
        # cache's expiry is measured.
        store_work_item_custom_keys("P", "task", frozenset({"f"}))
        await guard_work_item_custom_fields(mock_client, "P", "task", {"f": "x"})
        assert mock_client.get.await_count == 1

        clock[0] += 600.0  # past _ENUM_NOT_FOUND_TTL_SECONDS
        store_work_item_custom_keys("P", "task", frozenset({"f"}))
        await guard_work_item_custom_fields(mock_client, "P", "task", {"f": "x"})
        assert mock_client.get.await_count == 2

    async def test_polarion_error_blocks_write(self, mock_client: AsyncMock) -> None:
        mock_client.get.side_effect = PolarionError("backend down")

        with pytest.raises(RuntimeError, match="Refusing the write"):
            await guard_work_item_custom_fields(mock_client, "P", "task", {"asil": "1"})

    async def test_auth_error_raises_permission_error(
        self, mock_client: AsyncMock
    ) -> None:
        mock_client.get.side_effect = PolarionAuthError("forbidden", status_code=403)

        with pytest.raises(PermissionError, match="lacks permission"):
            await guard_work_item_custom_fields(mock_client, "P", "task", {"asil": "1"})


class TestGuardDocumentCustomFieldEnums:
    """Document-axis mirror; the shared enum core is exercised above."""

    @pytest.fixture(autouse=True)
    def _prime_key_schemas(self, _reset_caches: None) -> None:
        store_document_type_custom_keys(
            "P", "generic", frozenset({"docRisk", "freeText"})
        )

    async def test_valid_option_id_passes(self, mock_client: AsyncMock) -> None:
        mock_client.get.return_value = _enum_response(["high", "moderate", "low"])

        await guard_document_custom_fields(
            mock_client, "P", "generic", {"docRisk": "low"}
        )  # must not raise

    async def test_unknown_option_id_raises(self, mock_client: AsyncMock) -> None:
        mock_client.get.return_value = _enum_response(["high", "moderate", "low"])

        with pytest.raises(ValueError, match=r"'docRisk'.*'severe'"):
            await guard_document_custom_fields(
                mock_client, "P", "generic", {"docRisk": "severe"}
            )

    async def test_queries_documents_fields_endpoint(
        self, mock_client: AsyncMock
    ) -> None:
        mock_client.get.return_value = _enum_response(["high"])

        await guard_document_custom_fields(
            mock_client, "P", "generic", {"docRisk": "high"}
        )

        path = mock_client.get.call_args.args[0]
        expected = "/projects/P/documents/fields/docRisk/actions/getAvailableOptions"
        assert path == expected
        assert mock_client.get.call_args.kwargs["params"]["type"] == "generic"

    async def test_non_enum_field_defers_on_404(self, mock_client: AsyncMock) -> None:
        mock_client.get.side_effect = PolarionNotFoundError("not enum", status_code=404)

        await guard_document_custom_fields(
            mock_client, "P", "generic", {"freeText": "anything"}
        )  # must not raise


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

    async def test_auth_error_raises_permission_error(
        self, mock_client: AsyncMock
    ) -> None:
        mock_client.get.side_effect = PolarionAuthError("forbidden", status_code=403)

        with pytest.raises(PermissionError, match="lacks permission"):
            await guard_work_item_link_targets(mock_client, "P", [_link("A")])

    async def test_missing_target_project_raises_value_error(
        self, mock_client: AsyncMock
    ) -> None:
        mock_client.get.side_effect = PolarionNotFoundError("no such project")

        with pytest.raises(ValueError, match="P/A"):
            await guard_work_item_link_targets(mock_client, "P", [_link("A")])


def _project_enum_response(enum_name: str, ids: list[str]) -> dict[str, object]:
    """A single-enumeration response: ``data`` is a dict, options nested under it."""
    return {
        "data": {
            "type": "enumerations",
            "id": enum_name,
            "attributes": {"options": [{"id": i, "name": i} for i in ids]},
        }
    }


class TestFetchProjectEnumOptionIds:
    """Single-enumeration GET parsing (dict ``data``) + caching + fail-closed."""

    async def test_first_call_hits_polarion_and_parses_dict_options(
        self, mock_client: AsyncMock
    ) -> None:
        mock_client.get.return_value = _project_enum_response(
            "workitem-link-role", ["parent", "relates_to"]
        )

        result = await fetch_project_enum_option_ids(
            mock_client, "P", "workitem-link-role"
        )

        assert result == frozenset({"parent", "relates_to"})
        mock_client.get.assert_awaited_once()
        path, kwargs = (
            mock_client.get.call_args.args[0],
            mock_client.get.call_args.kwargs,
        )
        assert path == "/projects/P/enumerations/~/workitem-link-role/~"
        assert kwargs["params"]["fields[enumerations]"] == "@all"

    async def test_second_call_uses_cache(self, mock_client: AsyncMock) -> None:
        mock_client.get.return_value = _project_enum_response(
            "hyperlink-role", ["ref_int", "ref_ext"]
        )

        await fetch_project_enum_option_ids(mock_client, "P", "hyperlink-role")
        await fetch_project_enum_option_ids(mock_client, "P", "hyperlink-role")

        assert mock_client.get.await_count == 1

    async def test_cache_expiry_re_fetches(
        self, mock_client: AsyncMock, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        mock_client.get.return_value = _project_enum_response("hyperlink-role", ["a"])
        clock = [1000.0]
        monkeypatch.setattr(cache_mod, "_now", lambda: clock[0])

        await fetch_project_enum_option_ids(mock_client, "P", "hyperlink-role")
        clock[0] += cache_mod._GUARD_TTL_SECONDS + 1
        await fetch_project_enum_option_ids(mock_client, "P", "hyperlink-role")

        assert mock_client.get.await_count == 2

    async def test_polarion_error_blocks_write(self, mock_client: AsyncMock) -> None:
        mock_client.get.side_effect = PolarionError("backend down")

        with pytest.raises(RuntimeError, match="Refusing the write"):
            await fetch_project_enum_option_ids(mock_client, "P", "workitem-link-role")

    async def test_auth_error_raises_permission_error(
        self, mock_client: AsyncMock
    ) -> None:
        mock_client.get.side_effect = PolarionAuthError("forbidden", status_code=403)

        with pytest.raises(PermissionError, match="lacks permission"):
            await fetch_project_enum_option_ids(mock_client, "P", "workitem-link-role")

    async def test_not_found_defers_with_empty_set(
        self, mock_client: AsyncMock
    ) -> None:
        mock_client.get.side_effect = PolarionNotFoundError("nope", status_code=404)

        result = await fetch_project_enum_option_ids(
            mock_client, "P", "workitem-link-role"
        )

        assert result == frozenset()

    async def test_not_found_result_is_cached(self, mock_client: AsyncMock) -> None:
        mock_client.get.side_effect = PolarionNotFoundError("nope", status_code=404)

        await fetch_project_enum_option_ids(mock_client, "P", "workitem-link-role")
        await fetch_project_enum_option_ids(mock_client, "P", "workitem-link-role")

        assert mock_client.get.await_count == 1

    async def test_malformed_data_is_skipped(self, mock_client: AsyncMock) -> None:
        mock_client.get.return_value = {
            "data": {
                "attributes": {
                    "options": ["not-a-dict", {"id": ""}, {"id": "ok"}, {"name": "x"}]
                }
            }
        }

        result = await fetch_project_enum_option_ids(
            mock_client, "P", "workitem-link-role"
        )

        assert result == frozenset({"ok"})


class TestGuardWorkItemLinkRoles:
    """Link-role guard for ``create_work_item_links``."""

    async def test_valid_role_passes(self, mock_client: AsyncMock) -> None:
        mock_client.get.return_value = _project_enum_response(
            "workitem-link-role", ["parent", "relates_to"]
        )

        await guard_work_item_link_roles(mock_client, "P", ["relates_to", "parent"])

    async def test_unknown_role_raises_with_options(
        self, mock_client: AsyncMock
    ) -> None:
        mock_client.get.return_value = _project_enum_response(
            "workitem-link-role", ["parent", "relates_to"]
        )

        with pytest.raises(ValueError, match="ghost_role") as exc:
            await guard_work_item_link_roles(mock_client, "P", ["ghost_role"])

        assert "relates_to" in str(exc.value)

    async def test_dedup_one_get_for_repeated_roles(
        self, mock_client: AsyncMock
    ) -> None:
        mock_client.get.return_value = _project_enum_response(
            "workitem-link-role", ["parent"]
        )

        await guard_work_item_link_roles(mock_client, "P", ["parent", "parent"])

        mock_client.get.assert_awaited_once()

    async def test_empty_roles_skip_check(self, mock_client: AsyncMock) -> None:
        await guard_work_item_link_roles(mock_client, "P", [])

        mock_client.get.assert_not_awaited()

    async def test_empty_option_set_defers(self, mock_client: AsyncMock) -> None:
        mock_client.get.side_effect = PolarionNotFoundError("nope", status_code=404)

        await guard_work_item_link_roles(mock_client, "P", ["anything"])

    async def test_unreachable_backend_blocks_write(
        self, mock_client: AsyncMock
    ) -> None:
        mock_client.get.side_effect = PolarionError("backend down")

        with pytest.raises(RuntimeError, match="Refusing the write"):
            await guard_work_item_link_roles(mock_client, "P", ["relates_to"])

    async def test_auth_error_raises_permission_error(
        self, mock_client: AsyncMock
    ) -> None:
        mock_client.get.side_effect = PolarionAuthError("forbidden", status_code=403)

        with pytest.raises(PermissionError, match="lacks permission"):
            await guard_work_item_link_roles(mock_client, "P", ["relates_to"])


class TestGuardHyperlinkRoles:
    """Hyperlink-role guard for ``create_work_items`` / ``update_work_item``."""

    async def test_valid_role_passes(self, mock_client: AsyncMock) -> None:
        mock_client.get.return_value = _project_enum_response(
            "hyperlink-role", ["ref_int", "ref_ext"]
        )

        await guard_hyperlink_roles(mock_client, "P", ["ref_ext"])

    async def test_unknown_role_raises_with_options(
        self, mock_client: AsyncMock
    ) -> None:
        mock_client.get.return_value = _project_enum_response(
            "hyperlink-role", ["ref_int", "ref_ext"]
        )

        with pytest.raises(ValueError, match="ghost") as exc:
            await guard_hyperlink_roles(mock_client, "P", ["ghost"])

        assert "ref_int" in str(exc.value)

    async def test_empty_roles_skip_check(self, mock_client: AsyncMock) -> None:
        await guard_hyperlink_roles(mock_client, "P", [])

        mock_client.get.assert_not_awaited()


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

    async def test_auth_error_raises_permission_error(
        self, mock_client: AsyncMock
    ) -> None:
        mock_client.get.side_effect = PolarionAuthError("auth")

        with pytest.raises(PermissionError):
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

        with pytest.raises(RuntimeError, match="Refusing the delete"):
            await partition_delete_links(
                mock_client,
                "P",
                "MCPT-1",
                ["P/MCPT-1/parent/P/MCPT-2"],
            )
