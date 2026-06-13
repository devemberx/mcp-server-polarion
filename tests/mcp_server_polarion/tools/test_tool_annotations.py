"""Cross-tool invariant: write tools advertise the expected MCP annotations."""

from __future__ import annotations

import pytest

from mcp_server_polarion.server import mcp


class TestWriteToolAnnotations:
    """Write tools must advertise destructive/idempotent/openWorld hints — MCP
    clients use them for risk display and auto-approval policies.
    """

    @staticmethod
    async def _annotations_for(tool_name: str) -> object:
        tools = await mcp.list_tools()
        for tool in tools:
            if tool.name == tool_name:
                return tool.annotations
        msg = f"tool {tool_name!r} not registered on FastMCP instance"
        raise AssertionError(msg)

    @pytest.mark.parametrize(
        ("tool_name", "expected"),
        [
            (
                "create_work_items",
                {
                    "readOnlyHint": False,
                    "destructiveHint": False,
                    "idempotentHint": False,
                    "openWorldHint": True,
                },
            ),
            (
                "update_work_item",
                {
                    "readOnlyHint": False,
                    "destructiveHint": True,
                    "idempotentHint": True,
                    "openWorldHint": True,
                },
            ),
            (
                "move_work_item_to_document",
                {
                    "readOnlyHint": False,
                    "destructiveHint": True,
                    "idempotentHint": False,
                    "openWorldHint": True,
                },
            ),
            (
                "update_document",
                {
                    "readOnlyHint": False,
                    "destructiveHint": True,
                    "idempotentHint": True,
                    "openWorldHint": True,
                },
            ),
            (
                "create_work_item_links",
                {
                    "readOnlyHint": False,
                    "destructiveHint": False,
                    "idempotentHint": False,
                    "openWorldHint": True,
                },
            ),
            (
                "delete_work_item_links",
                {
                    "readOnlyHint": False,
                    "destructiveHint": True,
                    "idempotentHint": True,
                    "openWorldHint": True,
                },
            ),
            (
                "create_document_comments",
                {
                    "readOnlyHint": False,
                    "destructiveHint": False,
                    "idempotentHint": False,
                    "openWorldHint": True,
                },
            ),
            (
                "update_document_comment",
                {
                    "readOnlyHint": False,
                    "destructiveHint": False,
                    "idempotentHint": True,
                    "openWorldHint": True,
                },
            ),
        ],
    )
    async def test_write_tool_annotation(
        self,
        tool_name: str,
        expected: dict[str, bool],
    ) -> None:
        annotations = await self._annotations_for(tool_name)
        for key, value in expected.items():
            assert getattr(annotations, key) is value, (
                f"{tool_name}.{key} expected {value}, got {getattr(annotations, key)}"
            )
