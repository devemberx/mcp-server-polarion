"""Tests for the ``list_projects`` tool."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from mcp_server_polarion.core.exceptions import (
    PolarionAuthError,
    PolarionError,
)
from mcp_server_polarion.models import (
    PaginatedResult,
    ProjectSummary,
)
from mcp_server_polarion.tools.projects import list_projects


class TestListProjects:
    """Tests for the ``list_projects`` tool."""

    async def test_returns_projects(
        self, mock_ctx: MagicMock, mock_client: AsyncMock
    ) -> None:
        mock_client.get.return_value = {
            "data": [
                {
                    "type": "projects",
                    "id": "proj1",
                    "attributes": {"name": "Project One", "active": True},
                },
                {
                    "type": "projects",
                    "id": "proj2",
                    "attributes": {"name": "Project Two", "active": False},
                },
            ],
            "meta": {"totalCount": 2},
        }

        result = await list_projects(
            mock_ctx,
            query=None,
            page_size=100,
            page_number=1,
        )

        assert isinstance(result, PaginatedResult)
        assert len(result.items) == 2
        assert result.total_count == 2
        assert result.page == 1
        assert result.page_size == 100
        assert result.has_more is False
        p1 = ProjectSummary(id="proj1", name="Project One", active=True)
        assert result.items[0] == p1
        p2 = ProjectSummary(id="proj2", name="Project Two", active=False)
        assert result.items[1] == p2

    async def test_active_defaults_true_when_missing(
        self, mock_ctx: MagicMock, mock_client: AsyncMock
    ) -> None:
        mock_client.get.return_value = {
            "data": [
                {
                    "type": "projects",
                    "id": "proj1",
                    "attributes": {"name": "No Active Field"},
                },
                {
                    "type": "projects",
                    "id": "proj2",
                    "attributes": {"name": "Non-bool Active", "active": "yes"},
                },
            ],
            "meta": {"totalCount": 2},
        }

        result = await list_projects(
            mock_ctx,
            query=None,
            page_size=100,
            page_number=1,
        )

        assert result.items[0].active is True
        assert result.items[1].active is True

    async def test_requests_active_field(
        self, mock_ctx: MagicMock, mock_client: AsyncMock
    ) -> None:
        mock_client.get.return_value = {
            "data": [],
            "meta": {"totalCount": 0},
        }

        await list_projects(
            mock_ctx,
            query=None,
            page_size=100,
            page_number=1,
        )

        _, kwargs = mock_client.get.call_args
        assert "active" in kwargs["params"]["fields[projects]"].split(",")

    async def test_empty_projects(
        self, mock_ctx: MagicMock, mock_client: AsyncMock
    ) -> None:
        mock_client.get.return_value = {
            "data": [],
            "meta": {"totalCount": 0},
        }

        result = await list_projects(
            mock_ctx,
            query=None,
            page_size=100,
            page_number=1,
        )

        assert result.items == []
        assert result.total_count == 0
        assert result.has_more is False

    async def test_pagination_params_forwarded(
        self, mock_ctx: MagicMock, mock_client: AsyncMock
    ) -> None:
        mock_client.get.return_value = {
            "data": [
                {
                    "type": "projects",
                    "id": "proj2",
                    "attributes": {"name": "Project 2"},
                },
                {
                    "type": "projects",
                    "id": "proj3",
                    "attributes": {"name": "Project 3"},
                },
            ],
            "meta": {"totalCount": 5},
        }

        result = await list_projects(
            mock_ctx,
            query=None,
            page_size=2,
            page_number=2,
        )

        assert result.total_count == 5
        assert len(result.items) == 2
        assert result.page == 2
        assert result.has_more is True
        assert result.items[0].id == "proj2"
        assert result.items[1].id == "proj3"

        _, kwargs = mock_client.get.call_args
        assert kwargs["params"]["page[size]"] == 2
        assert kwargs["params"]["page[number]"] == 2

    async def test_auth_error_raises_permission_error(
        self, mock_ctx: MagicMock, mock_client: AsyncMock
    ) -> None:
        mock_client.get.side_effect = PolarionAuthError(
            "Unauthorized",
            status_code=401,
        )

        with pytest.raises(PermissionError, match="POLARION_TOKEN"):
            await list_projects(
                mock_ctx,
                query=None,
                page_size=100,
                page_number=1,
            )

    async def test_generic_error_raises_runtime_error(
        self, mock_ctx: MagicMock, mock_client: AsyncMock
    ) -> None:
        mock_client.get.side_effect = PolarionError(
            "Server error",
            status_code=500,
        )

        with pytest.raises(RuntimeError, match="Failed to list"):
            await list_projects(
                mock_ctx,
                query=None,
                page_size=100,
                page_number=1,
            )

    async def test_query_param_forwarded(
        self, mock_ctx: MagicMock, mock_client: AsyncMock
    ) -> None:
        mock_client.get.return_value = {
            "data": [],
            "meta": {"totalCount": 0},
        }

        await list_projects(
            mock_ctx,
            query="name:ILCU*",
            page_size=100,
            page_number=1,
        )

        _, kwargs = mock_client.get.call_args
        assert kwargs["params"]["query"] == "name:ILCU*"

    async def test_query_none_omits_param(
        self, mock_ctx: MagicMock, mock_client: AsyncMock
    ) -> None:
        mock_client.get.return_value = {
            "data": [],
            "meta": {"totalCount": 0},
        }

        await list_projects(
            mock_ctx,
            query=None,
            page_size=100,
            page_number=1,
        )

        _, kwargs = mock_client.get.call_args
        assert "query" not in kwargs["params"]

    async def test_query_returns_matching_items(
        self, mock_ctx: MagicMock, mock_client: AsyncMock
    ) -> None:
        mock_client.get.return_value = {
            "data": [
                {
                    "type": "projects",
                    "id": "proj1",
                    "attributes": {"name": "ILCU Main"},
                },
            ],
            "meta": {"totalCount": 1},
        }

        result = await list_projects(
            mock_ctx,
            query="name:ILCU*",
            page_size=100,
            page_number=1,
        )

        assert isinstance(result, PaginatedResult)
        assert len(result.items) == 1
        assert result.items[0].id == "proj1"

    async def test_total_count_floor_when_api_returns_zero(
        self, mock_ctx: MagicMock, mock_client: AsyncMock
    ) -> None:
        """totalCount=0 with items present uses item count."""
        mock_client.get.return_value = {
            "data": [
                {
                    "type": "projects",
                    "id": "proj1",
                    "attributes": {"name": "Project One"},
                },
            ],
            "meta": {"totalCount": 0},
        }

        result = await list_projects(
            mock_ctx,
            query=None,
            page_size=100,
            page_number=1,
        )

        assert result.total_count >= 1
