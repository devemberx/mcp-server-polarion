"""FastMCP middleware compacting tool-argument validation errors.

FastMCP validates tool arguments via Pydantic before the tool body runs, so a
per-tool wrapper can't catch the failure; the raw ``ValidationError`` dump
(per-error ``input_value`` reprs + pydantic.dev URLs) would otherwise become the
tool-result text the LLM pays for. ``on_call_tool`` wraps the call and rewrites
it to a one-line field summary.
"""

from __future__ import annotations

from fastmcp.exceptions import ToolError
from fastmcp.server.middleware import CallNext, Middleware, MiddlewareContext
from fastmcp.tools import ToolResult
from mcp.types import CallToolRequestParams
from pydantic import ValidationError


def compact_validation_error(
    tool_name: str, exc: ValidationError, *, max_errors: int = 20
) -> str:
    """One-line ``<loc.path>: <msg>`` summary of a tool-argument
    ``ValidationError``; drops ``input_value`` reprs and pydantic.dev URLs, caps
    at *max_errors* entries with a ``(+N more)`` suffix.
    """
    errors = exc.errors(include_url=False)
    parts = [
        f"{'.'.join(str(p) for p in err['loc'])}: {err['msg']}"
        for err in errors[:max_errors]
    ]
    summary = "; ".join(parts)
    if len(errors) > max_errors:
        summary += f"; (+{len(errors) - max_errors} more)"
    return f"Invalid arguments for tool '{tool_name}': {summary}"


class CompactValidationErrorMiddleware(Middleware):
    """Rewrite tool-argument ``ValidationError``s into compact ``ToolError``s.

    Also catches a ``ValidationError`` raised inside a tool body (e.g. result
    model construction); the compacted message still names the offending paths.
    """

    async def on_call_tool(
        self,
        context: MiddlewareContext[CallToolRequestParams],
        call_next: CallNext[CallToolRequestParams, ToolResult],
    ) -> ToolResult:
        try:
            return await call_next(context)
        except ValidationError as exc:
            raise ToolError(
                compact_validation_error(context.message.name, exc)
            ) from exc
