"""Test-record guard tests: result enum, defect-target existence."""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from mcp_server_polarion.core.exceptions import (
    PolarionAuthError,
    PolarionError,
    PolarionNotFoundError,
)
from mcp_server_polarion.tools._shared.guard import (
    guard_test_record_defects,
    guard_test_record_results,
)
from tests.mcp_server_polarion.tools._shared.guard._builders import (
    project_enum_response,
    workitems_response,
)


class TestGuardTestRecordResults:
    """Result guard: ``testing/test-result`` project enumeration."""

    async def test_valid_result_passes(self, mock_client: AsyncMock) -> None:
        mock_client.get.return_value = project_enum_response(
            "test-result", ["passed", "failed", "blocked"]
        )

        await guard_test_record_results(mock_client, "P", ["passed"])

        path = mock_client.get.await_args.args[0]
        assert path == "/projects/P/enumerations/testing/test-result/~"

    async def test_unknown_result_raises_with_discovery_hint(
        self, mock_client: AsyncMock
    ) -> None:
        mock_client.get.return_value = project_enum_response(
            "test-result", ["passed", "failed", "blocked"]
        )

        with pytest.raises(ValueError, match="ghost_result") as exc:
            await guard_test_record_results(mock_client, "P", ["ghost_result"])

        msg = str(exc.value)
        assert "passed" in msg
        assert "list_test_records" in msg

    async def test_dedup_one_get_for_repeated_results(
        self, mock_client: AsyncMock
    ) -> None:
        mock_client.get.return_value = project_enum_response("test-result", ["passed"])

        await guard_test_record_results(mock_client, "P", ["passed", "passed"])

        mock_client.get.assert_awaited_once()

    async def test_empty_results_skip_check(self, mock_client: AsyncMock) -> None:
        await guard_test_record_results(mock_client, "P", [])

        mock_client.get.assert_not_awaited()

    async def test_empty_option_set_defers(self, mock_client: AsyncMock) -> None:
        mock_client.get.side_effect = PolarionNotFoundError("nope", status_code=404)

        await guard_test_record_results(mock_client, "P", ["anything"])

    async def test_unreachable_backend_blocks_write(
        self, mock_client: AsyncMock
    ) -> None:
        mock_client.get.side_effect = PolarionError("backend down")

        with pytest.raises(RuntimeError, match="Refusing the write"):
            await guard_test_record_results(mock_client, "P", ["passed"])

    async def test_auth_error_raises_permission_error(
        self, mock_client: AsyncMock
    ) -> None:
        mock_client.get.side_effect = PolarionAuthError("forbidden", status_code=403)

        with pytest.raises(PermissionError, match="lacks permission"):
            await guard_test_record_results(mock_client, "P", ["passed"])


class TestGuardTestRecordDefects:
    """Defect-target existence guard for ``update_test_records``."""

    async def test_all_defects_exist_one_get_per_project(
        self, mock_client: AsyncMock
    ) -> None:
        mock_client.get.return_value = workitems_response("P", ["A", "B"])

        await guard_test_record_defects(mock_client, ["P/A", "P/B"])

        mock_client.get.assert_awaited_once()
        path, kwargs = (
            mock_client.get.call_args.args[0],
            mock_client.get.call_args.kwargs,
        )
        assert path == "/projects/P/workitems"
        assert kwargs["params"]["query"] == "id:(A B)"
        assert kwargs["params"]["fields[workitems]"] == "id"

    async def test_missing_defect_raises_value_error(
        self, mock_client: AsyncMock
    ) -> None:
        mock_client.get.return_value = workitems_response("P", ["A"])

        with pytest.raises(ValueError, match="P/B") as exc:
            await guard_test_record_defects(mock_client, ["P/A", "P/B"])

        assert "get_work_item" in str(exc.value)

    async def test_cross_project_two_gets(self, mock_client: AsyncMock) -> None:
        responses = {
            "P": workitems_response("P", ["A"]),
            "Q": workitems_response("Q", ["X"]),
        }

        async def fake_get(path: str, **kwargs: object) -> dict[str, object]:
            project = path.split("/")[2]
            return responses[project]

        mock_client.get.side_effect = fake_get

        await guard_test_record_defects(mock_client, ["P/A", "Q/X"])

        assert mock_client.get.await_count == 2

    async def test_missing_in_cross_project_is_caught(
        self, mock_client: AsyncMock
    ) -> None:
        async def fake_get(path: str, **kwargs: object) -> dict[str, object]:
            project = path.split("/")[2]
            return workitems_response(project, ["A"] if project == "P" else [])

        mock_client.get.side_effect = fake_get

        with pytest.raises(ValueError, match="Q/X"):
            await guard_test_record_defects(mock_client, ["P/A", "Q/X"])

    async def test_chunks_above_page_size(self, mock_client: AsyncMock) -> None:
        ids = sorted(f"WI-{n}" for n in range(150))

        async def fake_get(path: str, **kwargs: object) -> dict[str, object]:
            query = str(kwargs["params"]["query"])  # type: ignore[index]
            chunk = query.removeprefix("id:(").removesuffix(")").split()
            return workitems_response("P", chunk)

        mock_client.get.side_effect = fake_get

        await guard_test_record_defects(mock_client, [f"P/{i}" for i in ids])

        assert mock_client.get.await_count == 2

    async def test_empty_defects_skip_requests(self, mock_client: AsyncMock) -> None:
        await guard_test_record_defects(mock_client, [])

        mock_client.get.assert_not_awaited()

    async def test_unreachable_backend_blocks_write(
        self, mock_client: AsyncMock
    ) -> None:
        mock_client.get.side_effect = PolarionError("backend down")

        with pytest.raises(RuntimeError, match="Refusing the write"):
            await guard_test_record_defects(mock_client, ["P/A"])

    async def test_auth_error_raises_permission_error(
        self, mock_client: AsyncMock
    ) -> None:
        mock_client.get.side_effect = PolarionAuthError("forbidden", status_code=403)

        with pytest.raises(PermissionError, match="lacks permission"):
            await guard_test_record_defects(mock_client, ["P/A"])

    async def test_missing_target_project_raises_value_error(
        self, mock_client: AsyncMock
    ) -> None:
        mock_client.get.side_effect = PolarionNotFoundError("no such project")

        with pytest.raises(ValueError, match="P/A"):
            await guard_test_record_defects(mock_client, ["P/A"])
