"""Test-record guard tests: result enum + cached defect existence."""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from mcp_server_polarion.core.exceptions import (
    PolarionAuthError,
    PolarionError,
    PolarionNotFoundError,
)
from mcp_server_polarion.tools._shared.guard import (
    guard_test_record_defect_targets,
    guard_test_record_results,
)
from tests.mcp_server_polarion.tools._shared.guard._builders import (
    project_enum_response,
    workitems_response,
)


class TestGuardTestRecordResults:
    """``result`` validated via the ``testing``-context ``test-result`` enum."""

    async def test_valid_result_passes(self, mock_client: AsyncMock) -> None:
        mock_client.get.return_value = project_enum_response(
            "test-result", ["passed", "failed", "blocked"]
        )

        await guard_test_record_results(mock_client, "P", ["passed"])

        path = mock_client.get.await_args.args[0]
        assert path == "/projects/P/enumerations/testing/test-result/~"

    async def test_unknown_result_raises_with_options(
        self, mock_client: AsyncMock
    ) -> None:
        mock_client.get.return_value = project_enum_response(
            "test-result", ["passed", "failed", "blocked"]
        )

        with pytest.raises(ValueError, match="ghost") as exc:
            await guard_test_record_results(mock_client, "P", ["ghost"])

        assert "passed" in str(exc.value)

    async def test_empty_results_skip_check(self, mock_client: AsyncMock) -> None:
        await guard_test_record_results(mock_client, "P", [])

        mock_client.get.assert_not_awaited()

    async def test_enum_404_defers(self, mock_client: AsyncMock) -> None:
        mock_client.get.side_effect = PolarionNotFoundError("nope", status_code=404)

        await guard_test_record_results(mock_client, "P", ["anything"])

    async def test_dedup_one_get_for_repeated_results(
        self, mock_client: AsyncMock
    ) -> None:
        mock_client.get.return_value = project_enum_response("test-result", ["passed"])

        await guard_test_record_results(mock_client, "P", ["passed", "passed"])

        mock_client.get.assert_awaited_once()


class TestGuardTestRecordDefectTargets:
    """Defect-target existence guard -- cached across calls."""

    async def test_existing_target_passes(self, mock_client: AsyncMock) -> None:
        mock_client.get.return_value = workitems_response("P", ["MCPT-1"])

        await guard_test_record_defect_targets(mock_client, "P", ["P/MCPT-1"])

        mock_client.get.assert_awaited_once()
        path, kwargs = (
            mock_client.get.call_args.args[0],
            mock_client.get.call_args.kwargs,
        )
        assert path == "/projects/P/workitems"
        assert kwargs["params"]["query"] == "id:(MCPT-1)"

    async def test_missing_target_raises_value_error(
        self, mock_client: AsyncMock
    ) -> None:
        mock_client.get.return_value = workitems_response("P", [])

        with pytest.raises(ValueError, match="P/MCPT-99999") as exc:
            await guard_test_record_defect_targets(mock_client, "P", ["P/MCPT-99999"])

        assert "dangling" in str(exc.value)

    async def test_mixed_projects_grouped_one_get_per_project(
        self, mock_client: AsyncMock
    ) -> None:
        responses = {
            "P": workitems_response("P", ["A"]),
            "Q": workitems_response("Q", ["X"]),
        }

        async def fake_get(path: str, **kwargs: object) -> dict[str, object]:
            project = path.split("/")[2]
            return responses[project]

        mock_client.get.side_effect = fake_get

        await guard_test_record_defect_targets(mock_client, "P", ["P/A", "Q/X"])

        assert mock_client.get.await_count == 2

    async def test_bare_id_defaults_to_passed_project(
        self, mock_client: AsyncMock
    ) -> None:
        mock_client.get.return_value = workitems_response("P", ["A"])

        await guard_test_record_defect_targets(mock_client, "P", ["A"])

        kwargs = mock_client.get.call_args.kwargs
        assert kwargs["params"]["query"] == "id:(A)"

    async def test_cache_hit_second_call_makes_zero_http_calls(
        self, mock_client: AsyncMock
    ) -> None:
        mock_client.get.return_value = workitems_response("P", ["MCPT-1"])

        await guard_test_record_defect_targets(mock_client, "P", ["P/MCPT-1"])
        assert mock_client.get.await_count == 1

        mock_client.get.reset_mock()
        await guard_test_record_defect_targets(mock_client, "P", ["P/MCPT-1"])

        mock_client.get.assert_not_awaited()

    async def test_partial_cache_only_misses_queried(
        self, mock_client: AsyncMock
    ) -> None:
        mock_client.get.return_value = workitems_response("P", ["MCPT-1"])
        await guard_test_record_defect_targets(mock_client, "P", ["P/MCPT-1"])
        mock_client.get.reset_mock()

        mock_client.get.return_value = workitems_response("P", ["MCPT-2"])
        await guard_test_record_defect_targets(
            mock_client, "P", ["P/MCPT-1", "P/MCPT-2"]
        )

        mock_client.get.assert_awaited_once()
        kwargs = mock_client.get.call_args.kwargs
        assert kwargs["params"]["query"] == "id:(MCPT-2)"

    async def test_unreachable_backend_blocks_write(
        self, mock_client: AsyncMock
    ) -> None:
        mock_client.get.side_effect = PolarionError("backend down")

        with pytest.raises(RuntimeError, match="Refusing the write"):
            await guard_test_record_defect_targets(mock_client, "P", ["P/A"])

    async def test_auth_error_raises_permission_error(
        self, mock_client: AsyncMock
    ) -> None:
        mock_client.get.side_effect = PolarionAuthError("forbidden", status_code=403)

        with pytest.raises(PermissionError, match="Refusing the write"):
            await guard_test_record_defect_targets(mock_client, "P", ["P/A"])

    async def test_missing_target_project_raises_value_error(
        self, mock_client: AsyncMock
    ) -> None:
        mock_client.get.side_effect = PolarionNotFoundError("no such project")

        with pytest.raises(ValueError, match="P/A"):
            await guard_test_record_defect_targets(mock_client, "P", ["P/A"])
