"""Real MCP transport-path tests via ``fastmcp.Client(mcp)`` in-memory —
covers registration → JSON Schema → lifespan → client → mocked HTTP, which
direct-call tool tests bypass."""

from __future__ import annotations

from collections.abc import AsyncIterator

import httpx
import pytest
import respx
from fastmcp import Client
from fastmcp.client.transports.memory import FastMCPTransport
from fastmcp.exceptions import ToolError

import mcp_server_polarion.core.client as _client_mod
from mcp_server_polarion.server import mcp

_POLARION_HOST = "https://polarion.example.com"
_BASE = f"{_POLARION_HOST}/polarion/rest/v1"
_MCPClient = Client[FastMCPTransport]

_READ_TOOL_NAMES: frozenset[str] = frozenset(
    {
        "list_projects",
        "list_documents",
        "get_document",
        "list_document_enum_options",
        "list_work_item_enum_options",
        "read_document_parts",
        "read_document",
        "list_work_items",
        "get_sql_query_recipes",
        "get_work_item",
        "read_work_item",
        "list_work_item_links",
        "list_document_comments",
    }
)
_WRITE_TOOL_NAMES: frozenset[str] = frozenset(
    {
        "create_work_items",
        "update_work_item",
        "move_work_item_to_document",
        "move_work_item_from_document",
        "create_work_item_links",
        "delete_work_item_links",
        "update_work_item_link",
        "create_document",
        "update_document",
        "create_document_comments",
        "update_document_comment",
    }
)
EXPECTED_TOOL_NAMES: frozenset[str] = _READ_TOOL_NAMES | _WRITE_TOOL_NAMES


@pytest.fixture
def _polarion_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Set env vars the lifespan reads and zero out write-delay sleeps."""
    monkeypatch.setenv("POLARION_URL", _POLARION_HOST)
    monkeypatch.setenv("POLARION_TOKEN", "test-token-secret")
    # The lifespan builds PolarionClient itself, so patch the module default
    # rather than its write_delay arg to keep write-tool cases fast.
    monkeypatch.setattr(_client_mod, "_WRITE_DELAY_SECONDS", 0.0)


@pytest.fixture
async def mcp_client(_polarion_env: None) -> AsyncIterator[_MCPClient]:
    """Yield an in-memory fastmcp Client connected to the real server."""
    async with Client(mcp) as client:
        yield client


class TestToolRegistration:
    """Every expected tool reaches the MCP transport."""

    async def test_all_expected_tools_registered(self, mcp_client: _MCPClient) -> None:
        names = {t.name for t in await mcp_client.list_tools()}
        assert names == EXPECTED_TOOL_NAMES


class TestSqlRecipeGallery:
    """The SQL recipe gallery reaches the transport as a callable tool."""

    async def test_get_sql_query_recipes_reads(self, mcp_client: _MCPClient) -> None:
        # list_work_items points the LLM here before hand-writing SQL, so the
        # payload must not be empty.
        result = await mcp_client.call_tool("get_sql_query_recipes", {})
        body = result.structured_content
        assert body is not None
        recipes = body["recipes"]
        assert "list_work_items SQL recipes" in recipes
        assert "POLARION.STRUCT_WORKITEM_LINKEDWORKITEMS" in recipes


@pytest.mark.parametrize("tool_name", sorted(EXPECTED_TOOL_NAMES))
class TestToolMetadata:
    """Per-tool metadata checks parametrized over every expected tool."""

    async def test_description_non_empty(
        self, mcp_client: _MCPClient, tool_name: str
    ) -> None:
        tool = next(t for t in await mcp_client.list_tools() if t.name == tool_name)
        assert tool.description is not None
        assert tool.description.strip()

    async def test_input_schema_is_object(
        self, mcp_client: _MCPClient, tool_name: str
    ) -> None:
        tool = next(t for t in await mcp_client.list_tools() if t.name == tool_name)
        assert tool.inputSchema["type"] == "object"
        assert "properties" in tool.inputSchema


class TestSchemaValidation:
    """Pydantic Field constraints must be enforced at the JSON Schema layer."""

    async def test_page_size_schema_caps_at_100(self, mcp_client: _MCPClient) -> None:
        tool = next(
            t for t in await mcp_client.list_tools() if t.name == "list_projects"
        )
        page_size_schema = tool.inputSchema["properties"]["page_size"]
        assert page_size_schema["maximum"] == 100
        assert page_size_schema["minimum"] == 1

    async def test_page_size_above_max_rejected(self, mcp_client: _MCPClient) -> None:
        with pytest.raises(ToolError):
            await mcp_client.call_tool(
                "list_projects",
                {"page_size": 999, "page_number": 1},
            )

    async def test_page_size_below_min_rejected(self, mcp_client: _MCPClient) -> None:
        with pytest.raises(ToolError):
            await mcp_client.call_tool(
                "list_projects",
                {"page_size": 0, "page_number": 1},
            )


class TestEndToEndInvocation:
    """One read + one write traversing the full MCP path."""

    async def test_list_projects_round_trip(self, mcp_client: _MCPClient) -> None:
        with respx.mock(base_url=_BASE, assert_all_called=False) as mock:
            mock.get("/projects").mock(
                return_value=httpx.Response(
                    200,
                    json={
                        "data": [
                            {
                                "type": "projects",
                                "id": "P1",
                                "attributes": {
                                    "name": "Proj One",
                                    "active": True,
                                },
                            }
                        ],
                        "meta": {"totalCount": 1},
                    },
                )
            )
            result = await mcp_client.call_tool(
                "list_projects",
                {"page_size": 100, "page_number": 1},
            )

        body = result.structured_content
        assert body is not None
        assert body["total_count"] == 1
        assert body["page"] == 1
        assert body["page_size"] == 100
        assert body["has_more"] is False
        assert body["items"][0]["id"] == "P1"
        assert body["items"][0]["name"] == "Proj One"

    async def test_polarion_not_found_surfaces_as_tool_error(
        self, mcp_client: _MCPClient
    ) -> None:
        with respx.mock(base_url=_BASE, assert_all_called=False) as mock:
            mock.get("/projects/P1/workitems/P1-1").mock(
                return_value=httpx.Response(404, json={"errors": []})
            )
            with pytest.raises(ToolError):
                await mcp_client.call_tool(
                    "get_work_item",
                    {"project_id": "P1", "work_item_id": "P1-1"},
                )

    @staticmethod
    def _stub_type_options(mock: respx.MockRouter) -> None:
        """Stub the enum guard's ``getAvailableOptions`` GET for ``type``.

        The guard runs even on ``dry_run`` and is fail-closed, so the dry_run
        path now needs the validation endpoint to be reachable.
        """
        mock.get(
            "/projects/MCP_Test_Project/workitems/fields/type/actions/"
            "getAvailableOptions"
        ).mock(
            return_value=httpx.Response(
                200,
                json={"data": [{"id": "task", "name": "Task"}], "meta": {}},
            )
        )

    async def test_create_work_items_dry_run(self, mcp_client: _MCPClient) -> None:
        with respx.mock(base_url=_BASE, assert_all_called=False) as mock:
            self._stub_type_options(mock)
            result = await mcp_client.call_tool(
                "create_work_items",
                {
                    "project_id": "MCP_Test_Project",
                    "items": [{"title": "smoke", "type": "task"}],
                    "dry_run": True,
                },
            )

        body = result.structured_content
        assert body is not None
        assert body["dry_run"] is True
        assert body["created"] is False
        assert body["work_item_ids"] == []
        assert "payload_preview" in body
        assert body["payload_preview"]["data"][0]["type"] == "workitems"
        assert body["payload_preview"]["data"][0]["attributes"]["title"] == "smoke"

    async def test_create_work_items_dry_run_materialises_result_data(
        self,
        mcp_client: _MCPClient,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        # Regression: recursive-alias fields make a $defs self-reference that
        # fastmcp's json_schema_to_type can't rebuild, leaving result.data
        # unmaterialised and logging "Error parsing structured content".
        with (
            caplog.at_level("WARNING", logger="fastmcp"),
            respx.mock(base_url=_BASE, assert_all_called=False) as mock,
        ):
            self._stub_type_options(mock)
            result = await mcp_client.call_tool(
                "create_work_items",
                {
                    "project_id": "MCP_Test_Project",
                    "items": [{"title": "smoke", "type": "task"}],
                    "dry_run": True,
                },
            )

        assert not any(
            "Error parsing structured content" in rec.message for rec in caplog.records
        )
        assert result.data is not None
        assert result.data.dry_run is True
        assert result.data.work_item_ids == []
        assert result.data.payload_preview is not None
