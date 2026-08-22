"""Work-item guard: enum args, custom-field keys/values, bulk id/type resolution."""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from mcp_server_polarion.core.exceptions import (
    PolarionAuthError,
    PolarionError,
    PolarionNotFoundError,
)
from mcp_server_polarion.tools._shared import cache as cache_mod
from mcp_server_polarion.tools._shared.cache import (
    store_work_item_custom_keys,
)
from mcp_server_polarion.tools._shared.guard import (
    guard_work_item_attachment_refs,
    guard_work_item_comment_attachment_refs,
    guard_work_item_custom_fields,
    guard_work_item_enums,
    resolve_work_item_types,
)
from mcp_server_polarion.tools._shared.guard._http import GUARD_PAGE_SIZE
from mcp_server_polarion.tools._shared.guard.work_items import (
    _check_work_item_custom_keys,
)
from tests.mcp_server_polarion.tools._shared.guard._builders import (
    attachments_response,
    enum_response,
)


class TestGuardWorkItemEnums:
    """Validation of each work-item enum argument."""

    async def test_listed_value_passes(self, mock_client: AsyncMock) -> None:
        mock_client.get.return_value = enum_response(["must_have", "should_have"])

        await guard_work_item_enums(
            mock_client, "P", "task", severity="must_have"
        )  # must not raise

    async def test_unlisted_value_raises_value_error_with_options(
        self, mock_client: AsyncMock
    ) -> None:
        mock_client.get.return_value = enum_response(["must_have", "should_have"])

        with pytest.raises(ValueError) as exc:
            await guard_work_item_enums(mock_client, "P", "task", severity="ghost")

        msg = str(exc.value)
        assert "severity='ghost'" in msg
        assert "must_have" in msg and "should_have" in msg

    async def test_options_list_capped_for_pathological_enum(
        self, mock_client: AsyncMock
    ) -> None:
        # 60-option enum show first 50 + (+N more) suffix, not all 60.
        ids = [f"opt{i:03d}" for i in range(60)]
        mock_client.get.return_value = enum_response(ids)

        with pytest.raises(ValueError) as exc:
            await guard_work_item_enums(mock_client, "P", "task", severity="ghost")

        msg = str(exc.value)
        assert "opt000" in msg
        assert "opt049" in msg
        assert "opt050" not in msg
        assert "(+10 more)" in msg

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
        mock_client.get.return_value = enum_response(["task", "requirement"])

        await guard_work_item_enums(mock_client, "P", "task", type="task")

        params = mock_client.get.call_args.kwargs["params"]
        assert params["type"] == "~"

    async def test_status_uses_work_item_type_axis(
        self, mock_client: AsyncMock
    ) -> None:
        mock_client.get.return_value = enum_response(["open", "done"])

        await guard_work_item_enums(mock_client, "P", "task", status="open")

        params = mock_client.get.call_args.kwargs["params"]
        assert params["type"] == "task"

    async def test_listed_resolution_passes(self, mock_client: AsyncMock) -> None:
        mock_client.get.return_value = enum_response(["done", "wontfix"])

        await guard_work_item_enums(
            mock_client, "P", "task", resolution="done"
        )  # must not raise

        params = mock_client.get.call_args.kwargs["params"]
        assert params["type"] == "task"

    async def test_unlisted_resolution_raises(self, mock_client: AsyncMock) -> None:
        mock_client.get.return_value = enum_response(["done", "wontfix"])

        with pytest.raises(ValueError) as exc:
            await guard_work_item_enums(mock_client, "P", "task", resolution="ghost")

        assert "resolution='ghost'" in str(exc.value)


def _wi_list(*attrs: dict[str, object]) -> dict[str, object]:
    """JSON:API work-item list response with given ``attributes`` dicts."""
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
        # Primary path issue MIN-per-key SQL with @all, not per-item GET.
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
        # Type with >100 distinct keys span pages; union must span them
        # too, else key on page 2+ false-rejected.
        page1 = _wi_list(
            *(
                {"title": "x", "type": "task", f"k{i}": 1}
                for i in range(GUARD_PAGE_SIZE)
            )
        )
        page2 = _wi_list({"title": "y", "type": "task", "late_key": 9})
        mock_client.get.side_effect = [page1, page2]

        await _check_work_item_custom_keys(mock_client, "P", "task", {"late_key": 9})

        # Full page 1 (==100) force page 2; short page 2 stop loop.
        assert mock_client.get.await_count == 2
        schema = cache_mod._work_item_custom_key_cache.get(("P", "task"))
        assert schema is not None
        assert "late_key" in schema
        assert "k0" in schema
        assert len(schema) == GUARD_PAGE_SIZE + 1

    async def test_unknown_key_against_fresh_sample_rejects_without_retry(
        self, mock_client: AsyncMock
    ) -> None:
        # Cold cache: sample already current — unknown key rejected
        # straight away, no redundant second fetch.
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

        with pytest.raises(RuntimeError, match="Refusing the write") as exc:
            await _check_work_item_custom_keys(
                mock_client, "P", "task", {"risk_score": 5}
            )

        msg = str(exc.value)
        # Name unverifiable key and defer to user -- never instruct
        # self-recovery write (mid-update LLM could create junk items).
        assert "risk_score" in msg
        assert "ask the user" in msg.lower()
        assert "save one" not in msg.lower()
        assert "retry" not in msg.lower()

    async def test_sql_rejection_fails_closed(self, mock_client: AsyncMock) -> None:
        # No Lucene fallback: rejected SQL sample block write rather than
        # validate against incomplete schema.
        mock_client.get.side_effect = PolarionError("SQL not supported")

        with pytest.raises(RuntimeError, match="Refusing the write"):
            await _check_work_item_custom_keys(
                mock_client, "P", "task", {"risk_score": 9}
            )

        mock_client.get.assert_awaited_once()

    async def test_cached_schema_unknown_key_refetches_then_passes(
        self, mock_client: AsyncMock
    ) -> None:
        # Key unknown against *cached* (possibly stale) schema trigger one
        # fresh re-fetch; admin-added field now present — write pass.
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
        # SQL sample fail -> fail-closed.
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


class TestGuardWorkItemCustomFieldEnums:
    """Enum-value stage of ``guard_work_item_custom_fields``.

    Key stage covered by ``TestGuardWorkItemCustomFieldKeys``; schemas
    primed here so each test exercise only enum-value checks.
    """

    @pytest.fixture(autouse=True)
    def _prime_key_schemas(self, _reset_guard_caches: None) -> None:
        store_work_item_custom_keys("P", "softwarerequirement", frozenset({"asil"}))
        store_work_item_custom_keys(
            "P", "task", frozenset({"a", "asil", "f", "ftti", "other", "platform"})
        )

    async def test_unknown_key_rejected_before_enum_probe(
        self, mock_client: AsyncMock
    ) -> None:
        # Key stage run first: ghost key never reach getAvailableOptions —
        # cannot plant long-lived 404 entry in enum cache.
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
        mock_client.get.return_value = enum_response(["1", "2", "3", "4"])

        await guard_work_item_custom_fields(
            mock_client, "P", "softwarerequirement", {"asil": "4"}
        )  # must not raise

    async def test_unknown_option_id_raises_with_options(
        self, mock_client: AsyncMock
    ) -> None:
        mock_client.get.return_value = enum_response(["1", "2", "3", "4"])

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
        # Option ids are strings; int 4 would ghost even though '4' valid.
        mock_client.get.return_value = enum_response(["1", "2", "3", "4"])

        with pytest.raises(ValueError, match="int 4"):
            await guard_work_item_custom_fields(
                mock_client, "P", "softwarerequirement", {"asil": 4}
            )

    async def test_dict_value_on_enum_field_raises(
        self, mock_client: AsyncMock
    ) -> None:
        mock_client.get.return_value = enum_response(["1", "2"])

        with pytest.raises(ValueError, match="dict"):
            await guard_work_item_custom_fields(
                mock_client, "P", "task", {"asil": {"id": "1"}}
            )

    async def test_list_of_valid_options_passes(self, mock_client: AsyncMock) -> None:
        mock_client.get.return_value = enum_response(["windows", "linux", "osx"])

        await guard_work_item_custom_fields(
            mock_client, "P", "task", {"platform": ["windows", "linux"]}
        )  # must not raise

    async def test_list_with_unknown_option_raises(
        self, mock_client: AsyncMock
    ) -> None:
        mock_client.get.return_value = enum_response(["windows", "linux"])

        with pytest.raises(ValueError, match="'beos'"):
            await guard_work_item_custom_fields(
                mock_client, "P", "task", {"platform": ["windows", "beos"]}
            )

    async def test_list_with_non_string_element_raises(
        self, mock_client: AsyncMock
    ) -> None:
        mock_client.get.return_value = enum_response(["1", "2"])

        with pytest.raises(ValueError, match="int 2"):
            await guard_work_item_custom_fields(
                mock_client, "P", "task", {"asil": ["1", 2]}
            )

    async def test_empty_values_skip_probe_entirely(
        self, mock_client: AsyncMock
    ) -> None:
        # Payload builders drop empties; nothing reach Polarion to ghost —
        # guard must not even spend the probe GET.
        await guard_work_item_custom_fields(
            mock_client, "P", "task", {"asil": "", "other": None, "platform": []}
        )

        mock_client.get.assert_not_awaited()

    async def test_options_fetched_once_per_key_within_ttl(
        self, mock_client: AsyncMock
    ) -> None:
        mock_client.get.return_value = enum_response(["1", "2"])

        await guard_work_item_custom_fields(mock_client, "P", "task", {"a": "1"})
        await guard_work_item_custom_fields(mock_client, "P", "task", {"a": "2"})

        assert mock_client.get.await_count == 1

    async def test_not_found_outlives_guard_ttl(
        self, mock_client: AsyncMock, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # 404 entries get long not_found TTL; positive sets keep enum TTL.
        mock_client.get.side_effect = PolarionNotFoundError("not enum", status_code=404)
        clock = [1000.0]
        monkeypatch.setattr(cache_mod, "_now", lambda: clock[0])
        # Fixture primed key schema under real monotonic clock; on freshly
        # booted host its expiry can precede 1000.0. Re-prime under patched
        # clock so only enum cache expiry measured.
        store_work_item_custom_keys("P", "task", frozenset({"f"}))

        await guard_work_item_custom_fields(mock_client, "P", "task", {"f": "x"})
        clock[0] += cache_mod._ENUM_TTL_SECONDS + 1.0  # within not_found TTL
        store_work_item_custom_keys("P", "task", frozenset({"f"}))
        await guard_work_item_custom_fields(mock_client, "P", "task", {"f": "x"})
        assert mock_client.get.await_count == 1

        clock[0] += cache_mod._FIELD_OPTIONS_NOT_FOUND_TTL_SECONDS
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


def _typed_workitems_response(
    project_id: str, pairs: list[tuple[str, str]]
) -> dict[str, object]:
    """JSON:API workitems list response carrying each item ``type``."""
    return {
        "data": [
            {
                "type": "workitems",
                "id": f"{project_id}/{short_id}",
                "attributes": {"type": type_id},
            }
            for short_id, type_id in pairs
        ],
        "meta": {"totalCount": len(pairs)},
    }


class TestResolveWorkItemTypes:
    """Batched existence + type lookup backing bulk update guards."""

    async def test_all_found_maps_id_to_type(self, mock_client: AsyncMock) -> None:
        mock_client.get.return_value = _typed_workitems_response(
            "P", [("A", "task"), ("B", "requirement")]
        )

        types = await resolve_work_item_types(mock_client, "P", ["B", "A"])

        assert types == {"A": "task", "B": "requirement"}
        mock_client.get.assert_awaited_once()
        path = mock_client.get.call_args.args[0]
        params = mock_client.get.call_args.kwargs["params"]
        assert path == "/projects/P/workitems"
        assert params["query"] == "id:(A B)"
        assert params["fields[workitems]"] == "id,type"

    async def test_missing_ids_all_named(self, mock_client: AsyncMock) -> None:
        mock_client.get.return_value = _typed_workitems_response("P", [("A", "task")])

        with pytest.raises(ValueError, match=r"\['B', 'C'\]") as exc:
            await resolve_work_item_types(mock_client, "P", ["A", "B", "C"])

        assert "list_work_items" in str(exc.value)

    async def test_empty_ids_no_request(self, mock_client: AsyncMock) -> None:
        assert await resolve_work_item_types(mock_client, "P", []) == {}
        mock_client.get.assert_not_awaited()

    async def test_chunks_above_page_size(self, mock_client: AsyncMock) -> None:
        ids = sorted(f"WI-{n}" for n in range(150))

        async def fake_get(path: str, **kwargs: object) -> dict[str, object]:
            query = str(kwargs["params"]["query"])  # type: ignore[index]
            chunk = query.removeprefix("id:(").removesuffix(")").split()
            return _typed_workitems_response("P", [(i, "task") for i in chunk])

        mock_client.get.side_effect = fake_get

        types = await resolve_work_item_types(mock_client, "P", ids)

        assert mock_client.get.await_count == 2
        assert len(types) == 150

    async def test_missing_type_attribute_maps_to_empty(
        self, mock_client: AsyncMock
    ) -> None:
        # Defensive: entry without attributes still count as existing.
        mock_client.get.return_value = {
            "data": [{"type": "workitems", "id": "P/A"}, "not-a-dict"]
        }

        assert await resolve_work_item_types(mock_client, "P", ["A"]) == {"A": ""}

    async def test_non_list_data_treated_as_missing(
        self, mock_client: AsyncMock
    ) -> None:
        # Defensive: malformed reply must not pass existence check.
        mock_client.get.return_value = {"data": {"type": "workitems"}}

        with pytest.raises(ValueError, match="not found"):
            await resolve_work_item_types(mock_client, "P", ["A"])

    async def test_project_not_found_raises_value_error(
        self, mock_client: AsyncMock
    ) -> None:
        mock_client.get.side_effect = PolarionNotFoundError(
            "no such project", status_code=404
        )

        with pytest.raises(ValueError, match="Project 'P' not found"):
            await resolve_work_item_types(mock_client, "P", ["A"])

    async def test_unreachable_backend_blocks_write(
        self, mock_client: AsyncMock
    ) -> None:
        mock_client.get.side_effect = PolarionError("backend down")

        with pytest.raises(RuntimeError, match="Refusing the write"):
            await resolve_work_item_types(mock_client, "P", ["A"])

    async def test_auth_error_raises_permission_error(
        self, mock_client: AsyncMock
    ) -> None:
        mock_client.get.side_effect = PolarionAuthError("forbidden", status_code=403)

        with pytest.raises(PermissionError, match="lacks permission"):
            await resolve_work_item_types(mock_client, "P", ["A"])


class TestGuardWorkItemAttachmentRefs:
    """Update-path guard on ``description_html`` attachment refs."""

    async def test_no_refs_returns_without_get(self, mock_client: AsyncMock) -> None:
        await guard_work_item_attachment_refs(
            mock_client, "P", "WI-1", "<p>no refs here</p>"
        )

        mock_client.get.assert_not_awaited()

    async def test_matching_raw_ref_passes(self, mock_client: AsyncMock) -> None:
        mock_client.get.return_value = attachments_response(["1-x.png"], meta=False)

        await guard_work_item_attachment_refs(
            mock_client, "P", "WI-1", '<img src="workitemimg:1-x.png"/>'
        )  # must not raise

    async def test_url_encoded_token_matches_raw_id(
        self, mock_client: AsyncMock
    ) -> None:
        mock_client.get.return_value = attachments_response(
            ["1-test file.txt"], meta=False
        )

        await guard_work_item_attachment_refs(
            mock_client,
            "P",
            "WI-1",
            '<img src="workitemimg:1-test%20file.txt"/>',
        )  # must not raise

    async def test_dangling_ref_rejects_naming_list_tool(
        self, mock_client: AsyncMock
    ) -> None:
        mock_client.get.return_value = attachments_response(["1-real.png"], meta=False)

        with pytest.raises(ValueError, match="list_work_item_attachments") as exc:
            await guard_work_item_attachment_refs(
                mock_client, "P", "WI-1", '<img src="workitemimg:1-ghost.png"/>'
            )

        assert "1-ghost.png" in str(exc.value)

    async def test_wrong_scheme_rejects_before_any_get(
        self, mock_client: AsyncMock
    ) -> None:
        with pytest.raises(ValueError, match="workitemimg") as exc:
            await guard_work_item_attachment_refs(
                mock_client, "P", "WI-1", '<img src="attachment:1-x.png"/>'
            )

        assert "attachment" in str(exc.value)
        mock_client.get.assert_not_awaited()

    async def test_get_failure_blocks_write(self, mock_client: AsyncMock) -> None:
        mock_client.get.side_effect = PolarionError("backend down")

        with pytest.raises(RuntimeError, match="Refusing the write"):
            await guard_work_item_attachment_refs(
                mock_client, "P", "WI-1", '<img src="workitemimg:1-x.png"/>'
            )

    async def test_auth_error_raises_permission_error(
        self, mock_client: AsyncMock
    ) -> None:
        mock_client.get.side_effect = PolarionAuthError("forbidden", status_code=403)

        with pytest.raises(PermissionError, match="lacks permission"):
            await guard_work_item_attachment_refs(
                mock_client, "P", "WI-1", '<img src="workitemimg:1-x.png"/>'
            )

    async def test_get_uses_encoded_path_and_basic_fieldset(
        self, mock_client: AsyncMock
    ) -> None:
        mock_client.get.return_value = attachments_response(["1-x.png"], meta=False)

        await guard_work_item_attachment_refs(
            mock_client, "P", "WI 1", '<img src="workitemimg:1-x.png"/>'
        )

        path = mock_client.get.call_args.args[0]
        params = mock_client.get.call_args.kwargs["params"]
        assert path == "/projects/P/workitems/WI%201/attachments"
        assert params["fields[workitem_attachments]"] == "@basic"


class TestGuardWorkItemCommentAttachmentRefs:
    """Create-path guard on work item comment ``text`` attachment refs."""

    async def test_matching_ref_passes_via_attachments_path(
        self, mock_client: AsyncMock
    ) -> None:
        mock_client.get.return_value = attachments_response(["1-x.png"], meta=False)

        await guard_work_item_comment_attachment_refs(
            mock_client, "P", "WI-1", ['<img src="workitemimg:1-x.png"/>']
        )  # must not raise

        path = mock_client.get.call_args.args[0]
        params = mock_client.get.call_args.kwargs["params"]
        assert path == "/projects/P/workitems/WI-1/attachments"
        assert params["fields[workitem_attachments]"] == "@basic"

    async def test_dangling_ref_rejects_naming_list_tool(
        self, mock_client: AsyncMock
    ) -> None:
        mock_client.get.return_value = attachments_response(["1-real.png"], meta=False)

        with pytest.raises(ValueError, match="list_work_item_attachments") as exc:
            await guard_work_item_comment_attachment_refs(
                mock_client, "P", "WI-1", ['<img src="workitemimg:1-ghost.png"/>']
            )

        assert "1-ghost.png" in str(exc.value)
        assert "Comment(s) on" in str(exc.value)
