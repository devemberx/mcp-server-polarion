"""Request-layer building blocks: fail-closed error translation in
``guarded_get`` and page iteration/termination in ``paged_responses``.
"""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from mcp_server_polarion.core.exceptions import (
    PolarionAuthError,
    PolarionError,
    PolarionNotFoundError,
)
from mcp_server_polarion.tools._shared.guard._http import (
    GUARD_PAGE_SIZE,
    guarded_get,
    paged_responses,
)


class TestGuardedGet:
    async def test_returns_response_on_success(self, mock_client: AsyncMock) -> None:
        mock_client.get.return_value = {"data": [{"id": "x"}]}

        response = await guarded_get(
            mock_client, "/projects/P/workitems", {}, what="w", project_id="P"
        )

        assert response == {"data": [{"id": "x"}]}

    async def test_auth_error_becomes_permission_error(
        self, mock_client: AsyncMock
    ) -> None:
        mock_client.get.side_effect = PolarionAuthError("forbidden", status_code=403)

        with pytest.raises(PermissionError, match="POLARION_TOKEN"):
            await guarded_get(
                mock_client, "/projects/P/workitems", {}, what="w", project_id="P"
            )

    async def test_not_found_folds_into_unreachable_block_by_default(
        self, mock_client: AsyncMock
    ) -> None:
        mock_client.get.side_effect = PolarionNotFoundError("gone", status_code=404)

        with pytest.raises(RuntimeError, match="Refusing the write"):
            await guarded_get(
                mock_client, "/projects/P/workitems", {}, what="w", project_id="P"
            )

    async def test_not_found_propagates_when_requested(
        self, mock_client: AsyncMock
    ) -> None:
        mock_client.get.side_effect = PolarionNotFoundError("gone", status_code=404)

        with pytest.raises(PolarionNotFoundError):
            await guarded_get(
                mock_client,
                "/projects/P/workitems",
                {},
                what="w",
                project_id="P",
                propagate_not_found=True,
            )

    async def test_generic_error_becomes_runtime_error(
        self, mock_client: AsyncMock
    ) -> None:
        mock_client.get.side_effect = PolarionError("backend down", status_code=502)

        with pytest.raises(RuntimeError, match="backend down"):
            await guarded_get(
                mock_client, "/projects/P/workitems", {}, what="w", project_id="P"
            )


def _page(count: int) -> dict[str, object]:
    return {"data": [{"id": f"i{n}"} for n in range(count)]}


class TestPagedResponses:
    async def test_short_page_stops_after_yield(self, mock_client: AsyncMock) -> None:
        mock_client.get.return_value = _page(3)

        pages = [p async for p in paged_responses(mock_client, "/p", {})]

        assert len(pages) == 1
        assert mock_client.get.call_count == 1

    async def test_full_page_fetches_next(self, mock_client: AsyncMock) -> None:
        mock_client.get.side_effect = [_page(GUARD_PAGE_SIZE), _page(1)]

        pages = [p async for p in paged_responses(mock_client, "/p", {"q": "x"})]

        assert len(pages) == 2
        assert mock_client.get.call_args_list[0].kwargs["params"] == {
            "q": "x",
            "page[number]": 1,
        }
        assert mock_client.get.call_args_list[1].kwargs["params"] == {
            "q": "x",
            "page[number]": 2,
        }

    async def test_non_list_data_yields_nothing(self, mock_client: AsyncMock) -> None:
        mock_client.get.return_value = {"data": {"id": "single"}}

        pages = [p async for p in paged_responses(mock_client, "/p", {})]

        assert pages == []

    async def test_errors_propagate_untranslated(self, mock_client: AsyncMock) -> None:
        mock_client.get.side_effect = PolarionError("boom", status_code=500)

        with pytest.raises(PolarionError, match="boom"):
            _ = [p async for p in paged_responses(mock_client, "/p", {})]
