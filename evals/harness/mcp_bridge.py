"""Bridge the in-memory FastMCP server to Strands tools — Strands' native MCP
client spawns a separate process, out of respx's reach. ``TrajectoryRecorder``
captures each call's parsed result (checks need returns, not just args)."""

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
    """Append-only (name, args, result) log in call order; ``result`` is the parsed
    payload so checks can verify values came from prior calls, not ghosted ids.
    """

    calls: list[dict[str, Any]] = field(default_factory=list)

    def record_call(self, name: str, args: dict[str, Any]) -> int:
        self.calls.append({"name": name, "args": args, "result": None})
        return len(self.calls) - 1

    def record_result(self, index: int, result: object) -> None:
        self.calls[index]["result"] = result


def _result_payload(result: Any) -> object:
    """Parse a fastmcp call result into a JSON-shaped trajectory object.

    Prefers typed ``structured_content``; else JSON-parses the flattened text,
    falling back to the raw string; ``None`` for empty responses.
    """
    structured = getattr(result, "structured_content", None)
    if structured is not None:
        return structured
    blocks = getattr(result, "content", None) or []
    texts = [getattr(b, "text", "") for b in blocks]
    text = "\n".join(t for t in texts if t)
    if not text:
        return None
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        return text


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
        idx = recorder.record_call(name, args)
        tool_use_id = tool_use["toolUseId"]
        try:
            result = await mcp_client.call_tool(name, args)
        except ToolError as exc:
            recorder.record_result(idx, {"error": f"{type(exc).__name__}: {exc}"})
            return ToolResult(
                toolUseId=tool_use_id,
                status="error",
                content=[{"text": f"{type(exc).__name__}: {exc}"}],
            )
        recorder.record_result(idx, _result_payload(result))
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
