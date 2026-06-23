"""Tests for the enum option tools (work item + document)."""

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
from mcp_server_polarion.models import EnumOption, PaginatedResult
from mcp_server_polarion.tools.enum import (
    list_document_enum_options,
    list_work_item_enum_options,
)

_STATUS_DATA: list[dict[str, object]] = [
    {
        "id": "draft",
        "name": "Draft",
        "description": "Initial state",
        "default": True,
        "hidden": False,
        "terminal": False,
    },
    {
        "id": "inreview",
        "name": "In Review",
        "default": False,
        "hidden": False,
        "terminal": False,
    },
    {
        "id": "approved",
        "name": "Approved",
        "default": False,
        "hidden": False,
        "terminal": True,
    },
]


class TestListWorkItemEnumOptions:
    """Tests for the ``list_work_item_enum_options`` tool."""

    async def test_returns_enum_options(
        self, mock_ctx: MagicMock, mock_client: AsyncMock
    ) -> None:
        mock_client.get.return_value = {
            "data": _STATUS_DATA,
            "meta": {"totalCount": 3},
        }

        result = await list_work_item_enum_options(
            mock_ctx,
            project_id="MCP_Test_Project",
            field_id="status",
            work_item_type="task",
            page_size=100,
            page_number=1,
        )

        assert isinstance(result, PaginatedResult)
        assert len(result.items) == 3
        assert result.total_count == 3
        assert result.has_more is False
        first = result.items[0]
        assert isinstance(first, EnumOption)
        assert first.id == "draft"
        assert first.name == "Draft"
        assert first.description == "Initial state"
        assert first.default is True
        assert first.terminal is False
        assert result.items[2].terminal is True

    async def test_request_path_and_params(
        self, mock_ctx: MagicMock, mock_client: AsyncMock
    ) -> None:
        mock_client.get.return_value = {
            "data": [],
            "meta": {"totalCount": 0},
        }

        await list_work_item_enum_options(
            mock_ctx,
            project_id="MCP_Test_Project",
            field_id="status",
            work_item_type="task",
            page_size=50,
            page_number=2,
        )

        args, kwargs = mock_client.get.call_args
        assert args[0] == (
            "/projects/MCP_Test_Project"
            "/workitems/fields/status"
            "/actions/getAvailableOptions"
        )
        params = kwargs["params"]
        assert params["type"] == "task"
        assert params["page[size]"] == 50
        assert params["page[number]"] == 2

    async def test_type_agnostic_tilde_passes_through(
        self, mock_ctx: MagicMock, mock_client: AsyncMock
    ) -> None:
        mock_client.get.return_value = {
            "data": [],
            "meta": {"totalCount": 0},
        }

        await list_work_item_enum_options(
            mock_ctx,
            project_id="MCP_Test_Project",
            field_id="type",
            work_item_type="~",
            page_size=100,
            page_number=1,
        )

        _, kwargs = mock_client.get.call_args
        assert kwargs["params"]["type"] == "~"

    async def test_missing_optional_fields_default(
        self, mock_ctx: MagicMock, mock_client: AsyncMock
    ) -> None:
        mock_client.get.return_value = {
            "data": [{"id": "open", "name": "Open"}],
            "meta": {"totalCount": 1},
        }

        result = await list_work_item_enum_options(
            mock_ctx,
            project_id="MCP_Test_Project",
            field_id="status",
            work_item_type="task",
            page_size=100,
            page_number=1,
        )

        opt = result.items[0]
        assert opt.id == "open"
        assert opt.description == ""
        assert opt.default is False
        assert opt.hidden is False
        assert opt.terminal is False

    async def test_non_bool_flag_falls_back_to_false(
        self, mock_ctx: MagicMock, mock_client: AsyncMock
    ) -> None:
        mock_client.get.return_value = {
            "data": [
                {
                    "id": "weird",
                    "name": "Weird",
                    "default": "true",
                    "hidden": 1,
                    "terminal": None,
                }
            ],
            "meta": {"totalCount": 1},
        }

        result = await list_work_item_enum_options(
            mock_ctx,
            project_id="MCP_Test_Project",
            field_id="status",
            work_item_type="task",
            page_size=100,
            page_number=1,
        )

        opt = result.items[0]
        assert opt.default is False
        assert opt.hidden is False
        assert opt.terminal is False

    async def test_pagination_has_more(
        self, mock_ctx: MagicMock, mock_client: AsyncMock
    ) -> None:
        mock_client.get.return_value = {
            "data": _STATUS_DATA * 34,
            "meta": {"totalCount": 150},
        }

        result = await list_work_item_enum_options(
            mock_ctx,
            project_id="MCP_Test_Project",
            field_id="status",
            work_item_type="task",
            page_size=100,
            page_number=1,
        )

        assert result.total_count == 150
        assert result.has_more is True

    async def test_not_found_raises_value_error(
        self, mock_ctx: MagicMock, mock_client: AsyncMock
    ) -> None:
        mock_client.get.side_effect = PolarionNotFoundError(
            "Not found",
            status_code=404,
        )

        with pytest.raises(ValueError, match="No enum options"):
            await list_work_item_enum_options(
                mock_ctx,
                project_id="MCP_Test_Project",
                field_id="nope",
                work_item_type="task",
                page_size=100,
                page_number=1,
            )

    async def test_auth_error_raises_permission_error(
        self, mock_ctx: MagicMock, mock_client: AsyncMock
    ) -> None:
        mock_client.get.side_effect = PolarionAuthError(
            "Unauthorized",
            status_code=401,
        )

        with pytest.raises(PermissionError, match="POLARION_TOKEN"):
            await list_work_item_enum_options(
                mock_ctx,
                project_id="MCP_Test_Project",
                field_id="status",
                work_item_type="task",
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

        with pytest.raises(RuntimeError, match="Failed to list enum options"):
            await list_work_item_enum_options(
                mock_ctx,
                project_id="MCP_Test_Project",
                field_id="status",
                work_item_type="task",
                page_size=100,
                page_number=1,
            )


class TestListWorkItemEnumOptionsFieldValidation:
    """Verify Field constraints on ``list_work_item_enum_options`` parameters."""

    @staticmethod
    def _adapter_for(param_name: str) -> TypeAdapter[object]:
        hints = get_type_hints(list_work_item_enum_options)
        sig = inspect.signature(list_work_item_enum_options)
        field_info = sig.parameters[param_name].default
        return TypeAdapter(Annotated[hints[param_name], field_info])

    def test_page_size_rejects_above_max(self) -> None:
        with pytest.raises(ValidationError):
            self._adapter_for("page_size").validate_python(101)

    def test_page_size_accepts_max(self) -> None:
        assert self._adapter_for("page_size").validate_python(100) == 100

    def test_page_size_rejects_zero(self) -> None:
        with pytest.raises(ValidationError):
            self._adapter_for("page_size").validate_python(0)

    def test_page_number_rejects_zero(self) -> None:
        with pytest.raises(ValidationError):
            self._adapter_for("page_number").validate_python(0)


class TestListDocumentEnumOptions:
    """Tests for the ``list_document_enum_options`` tool."""

    async def test_returns_enum_options(
        self, mock_ctx: MagicMock, mock_client: AsyncMock
    ) -> None:
        mock_client.get.return_value = {
            "data": _STATUS_DATA,
            "meta": {"totalCount": 3},
        }

        result = await list_document_enum_options(
            mock_ctx,
            project_id="MCP_Test_Project",
            field_id="status",
            document_type="systemReqSpecification",
            page_size=100,
            page_number=1,
        )

        assert isinstance(result, PaginatedResult)
        assert len(result.items) == 3
        assert result.total_count == 3
        assert result.has_more is False
        first = result.items[0]
        assert isinstance(first, EnumOption)
        assert first.id == "draft"
        assert first.name == "Draft"
        assert first.description == "Initial state"
        assert first.default is True
        assert first.terminal is False
        assert result.items[2].terminal is True

    async def test_request_path_and_params(
        self, mock_ctx: MagicMock, mock_client: AsyncMock
    ) -> None:
        mock_client.get.return_value = {
            "data": [],
            "meta": {"totalCount": 0},
        }

        await list_document_enum_options(
            mock_ctx,
            project_id="MCP_Test_Project",
            field_id="status",
            document_type="systemReqSpecification",
            page_size=50,
            page_number=2,
        )

        args, kwargs = mock_client.get.call_args
        assert args[0] == (
            "/projects/MCP_Test_Project"
            "/documents/fields/status"
            "/actions/getAvailableOptions"
        )
        params = kwargs["params"]
        assert params["type"] == "systemReqSpecification"
        assert params["page[size]"] == 50
        assert params["page[number]"] == 2

    async def test_type_agnostic_tilde_passes_through(
        self, mock_ctx: MagicMock, mock_client: AsyncMock
    ) -> None:
        mock_client.get.return_value = {
            "data": [],
            "meta": {"totalCount": 0},
        }

        await list_document_enum_options(
            mock_ctx,
            project_id="MCP_Test_Project",
            field_id="status",
            document_type="~",
            page_size=100,
            page_number=1,
        )

        _, kwargs = mock_client.get.call_args
        assert kwargs["params"]["type"] == "~"

    async def test_missing_optional_fields_default(
        self, mock_ctx: MagicMock, mock_client: AsyncMock
    ) -> None:
        mock_client.get.return_value = {
            "data": [{"id": "open", "name": "Open"}],
            "meta": {"totalCount": 1},
        }

        result = await list_document_enum_options(
            mock_ctx,
            project_id="MCP_Test_Project",
            field_id="status",
            document_type="systemReqSpecification",
            page_size=100,
            page_number=1,
        )

        opt = result.items[0]
        assert opt.id == "open"
        assert opt.description == ""
        assert opt.default is False
        assert opt.hidden is False
        assert opt.terminal is False

    async def test_non_bool_flag_falls_back_to_false(
        self, mock_ctx: MagicMock, mock_client: AsyncMock
    ) -> None:
        mock_client.get.return_value = {
            "data": [
                {
                    "id": "weird",
                    "name": "Weird",
                    "default": "true",
                    "hidden": 1,
                    "terminal": None,
                }
            ],
            "meta": {"totalCount": 1},
        }

        result = await list_document_enum_options(
            mock_ctx,
            project_id="MCP_Test_Project",
            field_id="status",
            document_type="systemReqSpecification",
            page_size=100,
            page_number=1,
        )

        opt = result.items[0]
        assert opt.default is False
        assert opt.hidden is False
        assert opt.terminal is False

    async def test_pagination_has_more(
        self, mock_ctx: MagicMock, mock_client: AsyncMock
    ) -> None:
        mock_client.get.return_value = {
            "data": _STATUS_DATA * 34,
            "meta": {"totalCount": 150},
        }

        result = await list_document_enum_options(
            mock_ctx,
            project_id="MCP_Test_Project",
            field_id="status",
            document_type="systemReqSpecification",
            page_size=100,
            page_number=1,
        )

        assert result.total_count == 150
        assert result.has_more is True

    async def test_not_found_raises_value_error(
        self, mock_ctx: MagicMock, mock_client: AsyncMock
    ) -> None:
        mock_client.get.side_effect = PolarionNotFoundError(
            "Not found",
            status_code=404,
        )

        with pytest.raises(ValueError, match="No enum options"):
            await list_document_enum_options(
                mock_ctx,
                project_id="MCP_Test_Project",
                field_id="nope",
                document_type="systemReqSpecification",
                page_size=100,
                page_number=1,
            )

    async def test_auth_error_raises_permission_error(
        self, mock_ctx: MagicMock, mock_client: AsyncMock
    ) -> None:
        mock_client.get.side_effect = PolarionAuthError(
            "Unauthorized",
            status_code=401,
        )

        with pytest.raises(PermissionError, match="POLARION_TOKEN"):
            await list_document_enum_options(
                mock_ctx,
                project_id="MCP_Test_Project",
                field_id="status",
                document_type="systemReqSpecification",
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

        with pytest.raises(RuntimeError, match="Failed to list enum options"):
            await list_document_enum_options(
                mock_ctx,
                project_id="MCP_Test_Project",
                field_id="status",
                document_type="systemReqSpecification",
                page_size=100,
                page_number=1,
            )


class TestListDocumentEnumOptionsFieldValidation:
    """Verify Field constraints on ``list_document_enum_options`` parameters."""

    @staticmethod
    def _adapter_for(param_name: str) -> TypeAdapter[object]:
        hints = get_type_hints(list_document_enum_options)
        sig = inspect.signature(list_document_enum_options)
        field_info = sig.parameters[param_name].default
        return TypeAdapter(Annotated[hints[param_name], field_info])

    def test_page_size_rejects_above_max(self) -> None:
        with pytest.raises(ValidationError):
            self._adapter_for("page_size").validate_python(101)

    def test_page_size_accepts_max(self) -> None:
        assert self._adapter_for("page_size").validate_python(100) == 100

    def test_page_size_rejects_zero(self) -> None:
        with pytest.raises(ValidationError):
            self._adapter_for("page_size").validate_python(0)

    def test_page_number_rejects_zero(self) -> None:
        with pytest.raises(ValidationError):
            self._adapter_for("page_number").validate_python(0)
