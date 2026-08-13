"""Test-run guard tests: enums, template resolution, custom-field keys."""

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
    store_test_run_custom_keys,
)
from mcp_server_polarion.tools._shared.guard import (
    guard_test_run_custom_fields,
    guard_test_run_enums,
    guard_test_run_templates,
)
from mcp_server_polarion.tools._shared.guard._http import GUARD_PAGE_SIZE
from mcp_server_polarion.tools._shared.guard.enums import (
    fetch_project_enum_option_ids,
)
from tests.mcp_server_polarion.tools._shared.guard._builders import (
    project_enum_response,
)


def _tr_list(*attrs: dict[str, object]) -> dict[str, object]:
    """JSON:API test-run list response with given ``attributes`` dicts."""
    return {
        "data": [
            {"type": "testruns", "id": f"P/TR-{i}", "attributes": a}
            for i, a in enumerate(attrs)
        ]
    }


class TestGuardTestRunEnums:
    """type/status validated via the ``testing``-context enumerations."""

    async def test_valid_type_and_status_pass(self, mock_client: AsyncMock) -> None:
        mock_client.get.side_effect = [
            project_enum_response("testrun-type", ["manual", "automated"]),
            project_enum_response("testrun-status", ["open", "inProgress"]),
        ]

        await guard_test_run_enums(mock_client, "P", type="manual", status="open")

        assert mock_client.get.await_count == 2
        paths = [call.args[0] for call in mock_client.get.await_args_list]
        assert paths == [
            "/projects/P/enumerations/testing/testrun-type/~",
            "/projects/P/enumerations/testing/testrun-status/~",
        ]

    async def test_unknown_type_raises_with_options(
        self, mock_client: AsyncMock
    ) -> None:
        mock_client.get.return_value = project_enum_response(
            "testrun-type", ["manual", "automated"]
        )

        with pytest.raises(ValueError, match="test run type") as exc:
            await guard_test_run_enums(mock_client, "P", type="ghost")

        assert "manual" in str(exc.value)

    async def test_unknown_status_raises(self, mock_client: AsyncMock) -> None:
        mock_client.get.return_value = project_enum_response("testrun-status", ["open"])

        with pytest.raises(ValueError, match="test run status"):
            await guard_test_run_enums(mock_client, "P", status="ghost")

    async def test_none_args_skip_all_checks(self, mock_client: AsyncMock) -> None:
        await guard_test_run_enums(mock_client, "P")

        mock_client.get.assert_not_awaited()

    async def test_testing_context_cached_apart_from_wildcard(
        self, mock_client: AsyncMock
    ) -> None:
        # Same enum name under "~" must not satisfy "testing"-context probe;
        # composite cache key keep the two contexts distinct.
        mock_client.get.side_effect = [
            project_enum_response("testrun-type", ["stale"]),
            project_enum_response("testrun-type", ["manual"]),
        ]
        await fetch_project_enum_option_ids(mock_client, "P", "testrun-type")

        await guard_test_run_enums(mock_client, "P", type="manual")

        assert mock_client.get.await_count == 2

    async def test_enum_404_defers(self, mock_client: AsyncMock) -> None:
        mock_client.get.side_effect = PolarionNotFoundError("nope", status_code=404)

        await guard_test_run_enums(mock_client, "P", type="anything")


class TestGuardTestRunTemplates:
    """Template ids resolved (``isTemplate`` proven) before write."""

    @staticmethod
    def _template_response(
        run_id: str, *, is_template: bool | None
    ) -> dict[str, object]:
        attributes: dict[str, object] = {"id": run_id}
        if is_template is not None:
            attributes["isTemplate"] = is_template
        return {
            "data": {
                "type": "testruns",
                "id": f"P/{run_id}",
                "attributes": attributes,
            }
        }

    async def test_existing_template_passes(self, mock_client: AsyncMock) -> None:
        mock_client.get.return_value = self._template_response(
            "Empty", is_template=True
        )

        await guard_test_run_templates(mock_client, "P", ["Empty"])

        path = mock_client.get.await_args.args[0]
        assert path == "/projects/P/testruns/Empty"
        params = mock_client.get.await_args.kwargs["params"]
        assert params["fields[testruns]"] == "id,isTemplate"

    async def test_missing_template_raises_value_error(
        self, mock_client: AsyncMock
    ) -> None:
        mock_client.get.side_effect = PolarionNotFoundError("nope", status_code=404)

        with pytest.raises(ValueError, match="templates=True"):
            await guard_test_run_templates(mock_client, "P", ["Ghost"])

    async def test_run_instance_rejected(self, mock_client: AsyncMock) -> None:
        # Instances omit isTemplate entirely (observed on Polarion 2506).
        mock_client.get.return_value = self._template_response("test", is_template=None)

        with pytest.raises(ValueError, match="run instance"):
            await guard_test_run_templates(mock_client, "P", ["test"])

    async def test_duplicate_ids_checked_once(self, mock_client: AsyncMock) -> None:
        mock_client.get.return_value = self._template_response(
            "Empty", is_template=True
        )

        await guard_test_run_templates(mock_client, "P", ["Empty", "Empty"])

        assert mock_client.get.await_count == 1

    async def test_empty_ids_skip_requests(self, mock_client: AsyncMock) -> None:
        await guard_test_run_templates(mock_client, "P", [])

        mock_client.get.assert_not_awaited()

    async def test_auth_error_raises_permission_error(
        self, mock_client: AsyncMock
    ) -> None:
        mock_client.get.side_effect = PolarionAuthError("forbidden", status_code=403)

        with pytest.raises(PermissionError, match="Refusing the write"):
            await guard_test_run_templates(mock_client, "P", ["Empty"])

    async def test_polarion_error_blocks_write(self, mock_client: AsyncMock) -> None:
        mock_client.get.side_effect = PolarionError("backend down")

        with pytest.raises(RuntimeError, match="Refusing the write"):
            await guard_test_run_templates(mock_client, "P", ["Empty"])


class TestGuardTestRunCustomFieldKeys:
    """Key-only validation via the project-wide run + template sample."""

    async def test_no_custom_fields_short_circuits(
        self, mock_client: AsyncMock
    ) -> None:
        await guard_test_run_custom_fields(mock_client, "P", {})

        mock_client.get.assert_not_awaited()

    async def test_cached_schema_passes_without_sample(
        self, mock_client: AsyncMock
    ) -> None:
        store_test_run_custom_keys("P", frozenset({"goal"}))

        await guard_test_run_custom_fields(mock_client, "P", {"goal": "x"})

        mock_client.get.assert_not_awaited()

    async def test_sample_unions_runs_and_templates(
        self, mock_client: AsyncMock
    ) -> None:
        mock_client.get.side_effect = [
            _tr_list({"id": "R1", "title": "r", "goal": "g"}),
            _tr_list({"id": "T1", "title": "t", "description": "d"}),
        ]

        await guard_test_run_custom_fields(
            mock_client, "P", {"goal": "x", "description": "y"}
        )

        assert mock_client.get.await_count == 2
        first, second = mock_client.get.await_args_list
        assert first.args[0] == "/projects/P/testruns"
        assert first.kwargs["params"]["fields[testruns]"] == "@all"
        assert "templates" not in first.kwargs["params"]
        assert second.kwargs["params"]["templates"] == "true"
        # Standard attributes (id/title) stay out of schema.
        assert cache_mod._test_run_custom_key_cache.get("P") == frozenset(
            {"goal", "description"}
        )

    async def test_unknown_key_against_fresh_sample_rejects(
        self, mock_client: AsyncMock
    ) -> None:
        mock_client.get.side_effect = [
            _tr_list({"id": "R1", "goal": "g"}),
            {"data": []},
        ]

        with pytest.raises(ValueError) as exc:
            await guard_test_run_custom_fields(mock_client, "P", {"nope": 1})

        msg = str(exc.value)
        assert "nope" in msg
        assert "goal" in msg
        assert mock_client.get.await_count == 2

    async def test_empty_sample_fails_closed(self, mock_client: AsyncMock) -> None:
        mock_client.get.return_value = {"data": []}

        with pytest.raises(RuntimeError, match="Refusing the write") as exc:
            await guard_test_run_custom_fields(mock_client, "P", {"goal": "x"})

        assert "ask the user" in str(exc.value).lower()

    async def test_malformed_data_stops_sampling_and_fails_closed(
        self, mock_client: AsyncMock
    ) -> None:
        # Non-list ``data`` end each sampling loop; nothing sampled -> refuse.
        mock_client.get.return_value = {"data": "junk"}

        with pytest.raises(RuntimeError, match="Refusing the write"):
            await guard_test_run_custom_fields(mock_client, "P", {"goal": "x"})

        assert mock_client.get.await_count == 2

    async def test_cached_unknown_key_refetches_then_passes(
        self, mock_client: AsyncMock
    ) -> None:
        # Stale cached schema trigger one fresh re-sample before reject.
        store_test_run_custom_keys("P", frozenset({"goal"}))
        mock_client.get.side_effect = [
            _tr_list({"id": "R1", "goal": "g", "description": "d"}),
            {"data": []},
        ]

        await guard_test_run_custom_fields(mock_client, "P", {"description": "y"})

        assert mock_client.get.await_count == 2

    async def test_paginates_full_pages(self, mock_client: AsyncMock) -> None:
        page1 = _tr_list(*({"id": "R", f"k{i}": 1} for i in range(GUARD_PAGE_SIZE)))
        page2 = _tr_list({"id": "R", "late_key": 9})
        mock_client.get.side_effect = [page1, page2, {"data": []}]

        await guard_test_run_custom_fields(mock_client, "P", {"late_key": 9})

        # Full instance page force page 2; templates add third GET.
        assert mock_client.get.await_count == 3
        schema = cache_mod._test_run_custom_key_cache.get("P")
        assert schema is not None
        assert "late_key" in schema

    async def test_auth_error_raises_permission_error(
        self, mock_client: AsyncMock
    ) -> None:
        mock_client.get.side_effect = PolarionAuthError("forbidden", status_code=403)

        with pytest.raises(PermissionError, match="Refusing the write"):
            await guard_test_run_custom_fields(mock_client, "P", {"goal": "x"})

    async def test_polarion_error_blocks_write(self, mock_client: AsyncMock) -> None:
        mock_client.get.side_effect = PolarionError("backend down")

        with pytest.raises(RuntimeError, match="Refusing the write"):
            await guard_test_run_custom_fields(mock_client, "P", {"goal": "x"})
