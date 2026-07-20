"""Real MCP transport-path tests via ``fastmcp.Client(mcp)`` in-memory —
cover registration → JSON Schema → lifespan → client → mocked HTTP, which
direct-call tool tests bypass.
"""

from __future__ import annotations

import re
from collections.abc import AsyncIterator
from pathlib import Path

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
        "list_test_runs",
        "list_test_records",
        "get_test_record",
        "get_test_run",
        "get_sql_query_recipes",
        "get_html_recipes",
        "get_work_item",
        "read_work_item",
        "list_work_item_links",
        "list_document_comments",
        "list_work_item_comments",
        "list_document_attachments",
        "get_document_attachment_content",
        "list_work_item_attachments",
    }
)
_WRITE_TOOL_NAMES: frozenset[str] = frozenset(
    {
        "create_work_items",
        "create_test_runs",
        "create_test_records",
        "update_test_runs",
        "update_test_records",
        "update_work_items",
        "move_work_item_to_document",
        "move_work_item_from_document",
        "create_work_item_links",
        "delete_work_item_links",
        "update_work_item_link",
        "create_document",
        "update_document",
        "copy_document",
        "create_document_comments",
        "create_work_item_comments",
        "update_document_comment",
        "update_work_item_comment",
    }
)
EXPECTED_TOOL_NAMES: frozenset[str] = _READ_TOOL_NAMES | _WRITE_TOOL_NAMES


@pytest.fixture
def _polarion_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Set env vars lifespan read; zero out write-delay sleeps."""
    monkeypatch.setenv("POLARION_URL", _POLARION_HOST)
    monkeypatch.setenv("POLARION_TOKEN", "test-token-secret")
    # Lifespan build PolarionClient itself — patch module default rather
    # than write_delay arg to keep write-tool cases fast.
    monkeypatch.setattr(_client_mod, "_WRITE_DELAY_SECONDS", 0.0)


@pytest.fixture
async def mcp_client(_polarion_env: None) -> AsyncIterator[_MCPClient]:
    """In-memory fastmcp Client connected to real server."""
    async with Client(mcp) as client:
        yield client


class TestToolRegistration:
    """Every expected tool reach MCP transport."""

    async def test_all_expected_tools_registered(self, mcp_client: _MCPClient) -> None:
        names = {t.name for t in await mcp_client.list_tools()}
        assert names == EXPECTED_TOOL_NAMES


class TestSqlRecipeGallery:
    """SQL recipe gallery reach transport as callable tool."""

    async def test_get_sql_query_recipes_reads(self, mcp_client: _MCPClient) -> None:
        # list_work_items point LLM here before hand-writing SQL — payload
        # must not be empty.
        result = await mcp_client.call_tool("get_sql_query_recipes", {})
        body = result.structured_content
        assert body is not None
        recipes = body["recipes"]
        assert "list_work_items SQL recipes" in recipes
        assert "POLARION.STRUCT_WORKITEM_LINKEDWORKITEMS" in recipes


class TestHtmlRecipeGallery:
    """HTML recipe gallery reach transport as callable tool."""

    async def test_get_html_recipes_reads(self, mcp_client: _MCPClient) -> None:
        # Update tools point LLM here — payload must not be empty.
        result = await mcp_client.call_tool("get_html_recipes", {})
        body = result.structured_content
        assert body is not None
        recipes = body["recipes"]
        assert "polarion-Document-table" in recipes
        assert "polarion-rte-caption" in recipes


@pytest.mark.parametrize("tool_name", sorted(EXPECTED_TOOL_NAMES))
class TestToolMetadata:
    """Per-tool metadata checks."""

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
    """Pydantic Field constraints enforced at JSON Schema layer."""

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

    async def test_invalid_args_error_is_compacted(
        self, mcp_client: _MCPClient
    ) -> None:
        # 10 link entries missing required fields fail validation pre-HTTP;
        # middleware must compact dump, not echo input reprs or pydantic URLs.
        with pytest.raises(ToolError) as exc:
            await mcp_client.call_tool(
                "create_work_item_links",
                {
                    "project_id": "P1",
                    "work_item_id": "MCPT-1",
                    "links": [{} for _ in range(10)],
                },
            )

        msg = str(exc.value)
        assert "links.0.role" in msg
        assert "input_value" not in msg
        assert "errors.pydantic.dev" not in msg
        assert len(msg) < 1500


class TestEndToEndInvocation:
    """One read + one write traversing full MCP path."""

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

    async def test_list_test_runs_round_trip(self, mcp_client: _MCPClient) -> None:
        with respx.mock(base_url=_BASE, assert_all_called=False) as mock:
            mock.get("/projects/P1/testruns").mock(
                return_value=httpx.Response(
                    200,
                    json={
                        "data": [
                            {
                                "type": "testruns",
                                "id": "P1/TR-1",
                                "attributes": {
                                    "title": "Regression",
                                    "type": "manual",
                                    "status": "open",
                                    "isTemplate": False,
                                },
                                "relationships": {
                                    "author": {
                                        "data": {"type": "users", "id": "P1/devemberx"}
                                    }
                                },
                            }
                        ],
                        "included": [
                            {
                                "type": "users",
                                "id": "P1/devemberx",
                                "attributes": {"name": "Devember X"},
                            }
                        ],
                        "meta": {"totalCount": 1},
                    },
                )
            )
            result = await mcp_client.call_tool(
                "list_test_runs",
                {"project_id": "P1", "page_size": 100, "page_number": 1},
            )

        body = result.structured_content
        assert body is not None
        assert body["total_count"] == 1
        assert body["has_more"] is False
        assert body["items"][0]["id"] == "TR-1"
        assert body["items"][0]["author_name"] == "Devember X"

    async def test_list_test_records_round_trip(self, mcp_client: _MCPClient) -> None:
        with respx.mock(base_url=_BASE, assert_all_called=False) as mock:
            mock.get("/projects/P1/testruns/TR-1/testrecords").mock(
                return_value=httpx.Response(
                    200,
                    json={
                        "data": [
                            {
                                "type": "testrecords",
                                "id": "P1/TR-1/P1/TC-9/0",
                                "attributes": {
                                    "result": "passed",
                                    "duration": 3.5,
                                    "iteration": 0,
                                },
                                "relationships": {
                                    "testCase": {
                                        "data": {"type": "workitems", "id": "P1/TC-9"}
                                    },
                                    "executedBy": {
                                        "data": {"type": "users", "id": "P1/devemberx"}
                                    },
                                },
                            }
                        ],
                        "included": [
                            {
                                "type": "users",
                                "id": "P1/devemberx",
                                "attributes": {"name": "Devember X"},
                            }
                        ],
                        # Live endpoint omit meta.totalCount.
                    },
                )
            )
            result = await mcp_client.call_tool(
                "list_test_records",
                {"project_id": "P1", "test_run_id": "TR-1"},
            )

        body = result.structured_content
        assert body is not None
        assert body["total_count"] == 1
        assert body["has_more"] is False
        assert body["items"][0]["test_case_id"] == "P1/TC-9"
        assert body["items"][0]["result"] == "passed"
        assert body["items"][0]["executed_by_name"] == "Devember X"

    async def test_get_test_record_round_trip(self, mcp_client: _MCPClient) -> None:
        with respx.mock(base_url=_BASE, assert_all_called=False) as mock:
            mock.get("/projects/P1/testruns/TR-1/testrecords/P1/TC-9/0").mock(
                return_value=httpx.Response(
                    200,
                    json={
                        "data": {
                            "type": "testrecords",
                            "id": "P1/TR-1/P1/TC-9/0",
                            "attributes": {
                                "result": "failed",
                                "duration": 3.5,
                                "iteration": 0,
                                "comment": {
                                    "type": "text/html",
                                    "value": "<p>Investigate.</p>",
                                },
                                "testCaseRevision": "5",
                            },
                            "relationships": {
                                "testCase": {
                                    "data": {"type": "workitems", "id": "P1/TC-9"}
                                },
                                "executedBy": {
                                    "data": {"type": "users", "id": "P1/devemberx"}
                                },
                            },
                        },
                        "included": [
                            {
                                "type": "users",
                                "id": "P1/devemberx",
                                "attributes": {"name": "Devember X"},
                            }
                        ],
                    },
                )
            )
            result = await mcp_client.call_tool(
                "get_test_record",
                {
                    "project_id": "P1",
                    "test_run_id": "TR-1",
                    "test_case_id": "P1/TC-9",
                },
            )

        body = result.structured_content
        assert body is not None
        assert body["test_case_id"] == "P1/TC-9"
        assert body["result"] == "failed"
        assert body["executed_by_name"] == "Devember X"
        assert body["test_case_revision"] == "5"
        assert body["comment_html"] == "<p>Investigate.</p>"

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
        """Stub enum guard ``getAvailableOptions`` GET for ``type``.

        Guard run even on ``dry_run`` and is fail-closed — dry_run path
        need the validation endpoint reachable.
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
        # Regression: recursive-alias fields make $defs self-reference that
        # fastmcp json_schema_to_type can't rebuild — result.data stays
        # unmaterialised, log "Error parsing structured content".
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


_README_PATH = Path(__file__).parents[2] / "README.md"
# First-column backtick name only — description prose may cite tool names.
# \s* tolerate column-align padding; digits allowed in future tool names.
_TOOL_ROW_RE = re.compile(r"^\|\s*`([a-z0-9_]+)`\s*\|", re.MULTILINE)


def _readme_table_names(section: str) -> set[str]:
    """Tool names inside README marker-fenced table block."""
    readme = _README_PATH.read_text(encoding="utf-8")
    start = f"<!-- tool-table:{section}:start -->"
    end = f"<!-- tool-table:{section}:end -->"
    # Marker anchor, not heading — README prose free to restructure.
    assert readme.count(start) == 1, f"README marker {start!r} missing or duplicated"
    assert readme.count(end) == 1, f"README marker {end!r} missing or duplicated"
    block = readme.split(start, 1)[1].split(end, 1)[0]
    names = _TOOL_ROW_RE.findall(block)
    # Set-equality alone hide duplicated row — catch before dedup.
    dupes = sorted({n for n in names if names.count(n) > 1})
    assert not dupes, f"README {section} tool table duplicate rows: {dupes}"
    return set(names)


class TestReadmeToolTable:
    """README tool tables sync with registration — set equality per table."""

    @pytest.mark.parametrize(
        ("section", "expected"),
        [
            pytest.param("read", _READ_TOOL_NAMES, id="read"),
            pytest.param("write", _WRITE_TOOL_NAMES, id="write"),
        ],
    )
    def test_table_matches_registration(
        self, section: str, expected: frozenset[str]
    ) -> None:
        actual = _readme_table_names(section)
        missing = sorted(expected - actual)
        stale = sorted(actual - expected)
        assert not missing and not stale, (
            f"README {section} tool table out of sync — "
            f"add rows for {missing}; drop stale rows {stale}"
        )

    def test_prose_count_matches_registration(self) -> None:
        # Tables marker-synced above; prose "**N tools**" claim drift silent
        # without this (went stale at #178).
        readme = _README_PATH.read_text(encoding="utf-8")
        claims = re.findall(r"\*\*(\d+) tools?\*\*", readme)
        assert len(claims) == 1, (
            f"README must claim tool count once as '**N tools**', found {claims}"
        )
        assert int(claims[0]) == len(EXPECTED_TOOL_NAMES), (
            f"README claims {claims[0]} tools; registered {len(EXPECTED_TOOL_NAMES)} — "
            f"update the '**N tools**' line"
        )
