"""Shared target-existence helper tests: chunked queries + positive cache."""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from mcp_server_polarion.core.exceptions import (
    PolarionAuthError,
    PolarionError,
    PolarionNotFoundError,
)
from mcp_server_polarion.tools._shared.guard._targets import (
    existing_target_ids,
    missing_work_item_targets,
)
from tests.mcp_server_polarion.tools._shared.guard._builders import workitems_response


class TestExistingTargetIds:
    """``existing_target_ids`` -- chunked ``id:(...)`` probe."""

    async def test_queries_sorted_ids_and_returns_short_ids(
        self, mock_client: AsyncMock
    ) -> None:
        mock_client.get.return_value = workitems_response("P", ["A", "B"])

        found = await existing_target_ids(mock_client, "P", frozenset({"B", "A"}))

        assert found == frozenset({"A", "B"})
        path, kwargs = (
            mock_client.get.call_args.args[0],
            mock_client.get.call_args.kwargs,
        )
        assert path == "/projects/P/workitems"
        assert kwargs["params"]["query"] == "id:(A B)"
        assert kwargs["params"]["fields[workitems]"] == "id"

    async def test_chunks_above_page_size(self, mock_client: AsyncMock) -> None:
        ids = frozenset(f"WI-{n}" for n in range(150))

        async def fake_get(path: str, **kwargs: object) -> dict[str, object]:
            query = str(kwargs["params"]["query"])  # type: ignore[index]
            chunk = query.removeprefix("id:(").removesuffix(")").split()
            return workitems_response("P", chunk)

        mock_client.get.side_effect = fake_get

        found = await existing_target_ids(mock_client, "P", ids)

        assert found == ids
        assert mock_client.get.await_count == 2

    async def test_non_list_data_yields_empty(self, mock_client: AsyncMock) -> None:
        mock_client.get.return_value = {"data": {"id": "P/A"}}

        assert await existing_target_ids(mock_client, "P", frozenset({"A"})) == (
            frozenset()
        )


class TestMissingWorkItemTargets:
    """``missing_work_item_targets`` -- grouped probe + confirmed-positive cache."""

    async def test_all_existing_returns_empty(self, mock_client: AsyncMock) -> None:
        mock_client.get.return_value = workitems_response("P", ["A"])

        assert await missing_work_item_targets(mock_client, {"P": {"A"}}) == []

    async def test_missing_ids_returned_qualified_and_sorted(
        self, mock_client: AsyncMock
    ) -> None:
        mock_client.get.return_value = workitems_response("P", [])

        missing = await missing_work_item_targets(mock_client, {"P": {"B", "A"}})

        assert missing == ["P/A", "P/B"]

    async def test_project_404_marks_whole_group_missing(
        self, mock_client: AsyncMock
    ) -> None:
        mock_client.get.side_effect = PolarionNotFoundError("no such project")

        missing = await missing_work_item_targets(mock_client, {"P": {"A"}})

        assert missing == ["P/A"]

    async def test_confirmed_ids_cached_across_calls(
        self, mock_client: AsyncMock
    ) -> None:
        mock_client.get.return_value = workitems_response("P", ["A"])
        await missing_work_item_targets(mock_client, {"P": {"A"}})
        mock_client.get.reset_mock()

        await missing_work_item_targets(mock_client, {"P": {"A"}})

        mock_client.get.assert_not_awaited()

    async def test_missing_ids_never_cached_as_negative(
        self, mock_client: AsyncMock
    ) -> None:
        mock_client.get.return_value = workitems_response("P", [])
        assert await missing_work_item_targets(mock_client, {"P": {"A"}}) == ["P/A"]

        # WI created since last call -- next probe must hit Polarion again.
        mock_client.get.return_value = workitems_response("P", ["A"])
        assert await missing_work_item_targets(mock_client, {"P": {"A"}}) == []
        assert mock_client.get.await_count == 2

    async def test_partial_cache_queries_only_misses(
        self, mock_client: AsyncMock
    ) -> None:
        mock_client.get.return_value = workitems_response("P", ["A"])
        await missing_work_item_targets(mock_client, {"P": {"A"}})
        mock_client.get.reset_mock()

        mock_client.get.return_value = workitems_response("P", ["B"])
        await missing_work_item_targets(mock_client, {"P": {"A", "B"}})

        mock_client.get.assert_awaited_once()
        assert mock_client.get.call_args.kwargs["params"]["query"] == "id:(B)"

    async def test_unreachable_backend_fails_closed(
        self, mock_client: AsyncMock
    ) -> None:
        mock_client.get.side_effect = PolarionError("backend down")

        with pytest.raises(RuntimeError, match="Refusing the write"):
            await missing_work_item_targets(mock_client, {"P": {"A"}})

    async def test_auth_error_raises_permission_error(
        self, mock_client: AsyncMock
    ) -> None:
        mock_client.get.side_effect = PolarionAuthError("forbidden", status_code=403)

        with pytest.raises(PermissionError, match="lacks permission"):
            await missing_work_item_targets(mock_client, {"P": {"A"}})
