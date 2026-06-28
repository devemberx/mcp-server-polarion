"""Tests for the ``list_test_runs`` tool."""

from __future__ import annotations

import inspect
from typing import Annotated, get_type_hints
from unittest.mock import AsyncMock, MagicMock

import pytest
from pydantic import TypeAdapter, ValidationError

from mcp_server_polarion.core.exceptions import (
    PolarionAuthError,
    PolarionError,
    PolarionNotFoundError,
)
from mcp_server_polarion.models import PaginatedResult
from mcp_server_polarion.tools.test_runs import list_test_runs


class TestListTestRuns:
    """Tests for the ``list_test_runs`` tool."""

    async def test_returns_test_runs(
        self, mock_ctx: MagicMock, mock_client: AsyncMock
    ) -> None:
        mock_client.get.return_value = {
            "data": [
                {
                    "type": "testruns",
                    "id": "proj1/TR-001",
                    "attributes": {
                        "title": "Sprint 1 Regression",
                        "type": "manual",
                        "status": "open",
                        "finishedOn": "2026-05-02T11:00:00Z",
                        "updated": "2026-05-01T09:00:00Z",
                        "isTemplate": False,
                    },
                    "relationships": {
                        "author": {"data": {"type": "users", "id": "proj1/devemberx"}}
                    },
                },
                {
                    "type": "testruns",
                    "id": "proj1/TR-002",
                    "attributes": {
                        "title": "Smoke Template",
                        "type": "automated",
                        "status": "finished",
                        "isTemplate": True,
                    },
                    "relationships": {"author": {"data": None}},
                },
            ],
            "included": [
                {
                    "type": "users",
                    "id": "proj1/devemberx",
                    "attributes": {"name": "Devember X"},
                }
            ],
            "meta": {"totalCount": 2},
        }

        result = await list_test_runs(
            mock_ctx,
            project_id="proj1",
            query=None,
            templates=False,
            page_size=100,
            page_number=1,
        )

        assert isinstance(result, PaginatedResult)
        assert len(result.items) == 2
        assert result.total_count == 2

        first = result.items[0]
        assert first.id == "TR-001"
        assert first.title == "Sprint 1 Regression"
        assert first.type == "manual"
        assert first.status == "open"
        assert first.finished_on == "2026-05-02T11:00:00Z"
        assert first.updated == "2026-05-01T09:00:00Z"
        assert first.author_name == "Devember X"
        assert first.is_template is False

        second = result.items[1]
        assert second.id == "TR-002"
        assert second.finished_on == ""
        assert second.updated == ""
        assert second.author_name == ""
        assert second.is_template is True

    async def test_missing_author_yields_empty_name(
        self, mock_ctx: MagicMock, mock_client: AsyncMock
    ) -> None:
        mock_client.get.return_value = {
            "data": [
                {
                    "id": "proj1/TR-003",
                    "attributes": {
                        "title": "No Author",
                        "type": "manual",
                        "status": "open",
                    },
                    "relationships": {
                        "author": {"data": {"type": "users", "id": "proj1/ghost"}}
                    },
                }
            ],
            "meta": {"totalCount": 1},
        }

        result = await list_test_runs(
            mock_ctx,
            project_id="proj1",
            query=None,
            templates=False,
            page_size=100,
            page_number=1,
        )

        assert result.items[0].author_name == ""

    async def test_sparse_fieldset_and_includes_requested(
        self, mock_ctx: MagicMock, mock_client: AsyncMock
    ) -> None:
        mock_client.get.return_value = {"data": [], "meta": {"totalCount": 0}}

        await list_test_runs(
            mock_ctx,
            project_id="proj1",
            query=None,
            templates=False,
            page_size=100,
            page_number=1,
        )

        _, kwargs = mock_client.get.call_args
        params = kwargs["params"]
        assert "fields[testruns]" in params
        assert params["include"] == "author"
        assert params["fields[users]"] == "name"

    async def test_strips_project_prefix_from_id(
        self, mock_ctx: MagicMock, mock_client: AsyncMock
    ) -> None:
        mock_client.get.return_value = {
            "data": [
                {
                    "id": "myproject/TR-100",
                    "attributes": {
                        "title": "Run",
                        "type": "manual",
                        "status": "open",
                    },
                }
            ],
            "meta": {"totalCount": 1},
        }

        result = await list_test_runs(
            mock_ctx,
            project_id="myproject",
            query=None,
            templates=False,
            page_size=100,
            page_number=1,
        )

        assert result.items[0].id == "TR-100"

    async def test_query_param_forwarded(
        self, mock_ctx: MagicMock, mock_client: AsyncMock
    ) -> None:
        mock_client.get.return_value = {"data": [], "meta": {"totalCount": 0}}

        await list_test_runs(
            mock_ctx,
            project_id="proj1",
            query="author.id:devemberx",
            templates=False,
            page_size=100,
            page_number=1,
        )

        _, kwargs = mock_client.get.call_args
        assert kwargs["params"]["query"] == "author.id:devemberx"

    async def test_query_none_omits_param(
        self, mock_ctx: MagicMock, mock_client: AsyncMock
    ) -> None:
        mock_client.get.return_value = {"data": [], "meta": {"totalCount": 0}}

        await list_test_runs(
            mock_ctx,
            project_id="proj1",
            query=None,
            templates=False,
            page_size=100,
            page_number=1,
        )

        _, kwargs = mock_client.get.call_args
        assert "query" not in kwargs["params"]

    async def test_sql_prefix_query_passed_verbatim(
        self, mock_ctx: MagicMock, mock_client: AsyncMock
    ) -> None:
        sql_query = (
            "SQL:(SELECT tr.* FROM POLARION.TESTRUN tr WHERE tr.C_STATUS = 'open')"
        )
        mock_client.get.return_value = {"data": [], "meta": {"totalCount": 0}}

        await list_test_runs(
            mock_ctx,
            project_id="proj1",
            query=sql_query,
            templates=False,
            page_size=100,
            page_number=1,
        )

        _, kwargs = mock_client.get.call_args
        assert kwargs["params"]["query"] == sql_query

    async def test_templates_true_adds_param(
        self, mock_ctx: MagicMock, mock_client: AsyncMock
    ) -> None:
        mock_client.get.return_value = {"data": [], "meta": {"totalCount": 0}}

        await list_test_runs(
            mock_ctx,
            project_id="proj1",
            query=None,
            templates=True,
            page_size=100,
            page_number=1,
        )

        _, kwargs = mock_client.get.call_args
        assert kwargs["params"]["templates"] == "true"

    async def test_templates_false_omits_param(
        self, mock_ctx: MagicMock, mock_client: AsyncMock
    ) -> None:
        mock_client.get.return_value = {"data": [], "meta": {"totalCount": 0}}

        await list_test_runs(
            mock_ctx,
            project_id="proj1",
            query=None,
            templates=False,
            page_size=100,
            page_number=1,
        )

        _, kwargs = mock_client.get.call_args
        assert "templates" not in kwargs["params"]

    async def test_project_not_found_raises_value_error(
        self, mock_ctx: MagicMock, mock_client: AsyncMock
    ) -> None:
        mock_client.get.side_effect = PolarionNotFoundError(
            "Not found", status_code=404
        )

        with pytest.raises(ValueError, match="not found"):
            await list_test_runs(
                mock_ctx,
                project_id="missing",
                query=None,
                templates=False,
                page_size=100,
                page_number=1,
            )

    async def test_auth_error_raises_permission_error(
        self, mock_ctx: MagicMock, mock_client: AsyncMock
    ) -> None:
        mock_client.get.side_effect = PolarionAuthError("auth", status_code=401)

        with pytest.raises(PermissionError):
            await list_test_runs(
                mock_ctx,
                project_id="proj1",
                query=None,
                templates=False,
                page_size=100,
                page_number=1,
            )

    async def test_other_error_raises_runtime_error(
        self, mock_ctx: MagicMock, mock_client: AsyncMock
    ) -> None:
        mock_client.get.side_effect = PolarionError("boom", status_code=500)

        with pytest.raises(RuntimeError, match="boom"):
            await list_test_runs(
                mock_ctx,
                project_id="proj1",
                query=None,
                templates=False,
                page_size=100,
                page_number=1,
            )


class TestListTestRunsFieldValidation:
    """``page_size`` bounds — direct calls bypass JSON Schema; proven via
    ``TypeAdapter`` rebuild from the signature.
    """

    @staticmethod
    def _adapter(param_name: str) -> TypeAdapter[object]:
        hints = get_type_hints(list_test_runs)
        sig = inspect.signature(list_test_runs)
        field_info = sig.parameters[param_name].default
        return TypeAdapter(Annotated[hints[param_name], field_info])

    def test_page_size_boundaries_accepted(self) -> None:
        adapter = self._adapter("page_size")
        assert adapter.validate_python(1) == 1
        assert adapter.validate_python(100) == 100

    def test_page_size_below_min_rejected(self) -> None:
        with pytest.raises(ValidationError):
            self._adapter("page_size").validate_python(0)

    def test_page_size_above_max_rejected(self) -> None:
        with pytest.raises(ValidationError):
            self._adapter("page_size").validate_python(101)

    def test_page_number_below_min_rejected(self) -> None:
        with pytest.raises(ValidationError):
            self._adapter("page_number").validate_python(0)
