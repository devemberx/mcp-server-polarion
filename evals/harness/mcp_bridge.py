"""Bridge the in-memory FastMCP server to Strands agent tools.

Strands' native MCP client speaks over a transport (stdio / HTTP), which
would run the server in a separate process where respx cannot intercept its
httpx traffic. To keep everything in one process — so the fake Polarion
mock applies — we instead read the *real* tool specs (name, description,
JSON Schema) from the in-memory ``fastmcp.Client`` and wrap each as a
``PythonAgentTool`` that forwards the call back through that client.

Every forwarded call is recorded by ``TrajectoryRecorder`` before dispatch,
giving the deterministic Tier-1 evaluators the exact (name, args) sequence.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

from fastmcp import Client
from fastmcp.exceptions import ToolError
from strands.tools import PythonAgentTool
from strands.types.tools import ToolResult, ToolSpec, ToolUse


@dataclass
class TrajectoryRecorder:
    """Append-only log of tool calls the agent made, in order."""

    calls: list[dict[str, Any]] = field(default_factory=list)

    def record(self, name: str, args: dict[str, Any]) -> None:
        self.calls.append({"name": name, "args": args})


def _result_text(result: Any) -> str:
    """Flatten a fastmcp call result into a single text payload for the LLM."""
    structured = getattr(result, "structured_content", None)
    if structured is not None:
        return json.dumps(structured, ensure_ascii=False, default=str)
    blocks = getattr(result, "content", None) or []
    texts = [getattr(b, "text", "") for b in blocks]
    return "\n".join(t for t in texts if t)


def _make_tool_func(
    name: str, mcp_client: Client[Any], recorder: TrajectoryRecorder
) -> Any:
    async def tool_func(tool_use: ToolUse, **_state: Any) -> ToolResult:
        args: dict[str, Any] = dict(tool_use.get("input") or {})
        recorder.record(name, args)
        tool_use_id = tool_use["toolUseId"]
        try:
            result = await mcp_client.call_tool(name, args)
        except ToolError as exc:
            return ToolResult(
                toolUseId=tool_use_id,
                status="error",
                content=[{"text": f"{type(exc).__name__}: {exc}"}],
            )
        return ToolResult(
            toolUseId=tool_use_id,
            status="success",
            content=[{"text": _result_text(result)}],
        )

    return tool_func


async def build_agent_tools(
    mcp_client: Client[Any], recorder: TrajectoryRecorder
) -> list[PythonAgentTool]:
    """Build one Strands tool per registered MCP tool, sharing *recorder*."""
    tools: list[PythonAgentTool] = []
    for spec in await mcp_client.list_tools():
        tool_spec: ToolSpec = {
            "name": spec.name,
            "description": spec.description or spec.name,
            "inputSchema": {"json": spec.inputSchema},
        }
        tools.append(
            PythonAgentTool(
                spec.name, tool_spec, _make_tool_func(spec.name, mcp_client, recorder)
            )
        )
    return tools
