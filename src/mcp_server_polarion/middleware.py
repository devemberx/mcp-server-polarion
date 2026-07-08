"""FastMCP middleware compacting tool-argument validation errors.

FastMCP validate tool args via Pydantic before tool body run — per-tool
wrapper can't catch. Raw ``ValidationError`` dump (``input_value`` reprs +
pydantic.dev URLs) would become tool-result text LLM pay for.
``on_call_tool`` wrap call, rewrite to one-line field summary.
"""

from __future__ import annotations

from fastmcp.exceptions import ToolError
from fastmcp.exceptions import ValidationError as FastMCPValidationError
from fastmcp.server.middleware import CallNext, Middleware, MiddlewareContext
from fastmcp.tools import ToolResult
from mcp.types import CallToolRequestParams
from pydantic import ValidationError


def compact_validation_error(
    tool_name: str, exc: ValidationError, *, max_errors: int = 20
) -> str:
    """One-line ``<loc.path>: <msg>`` summary of tool-arg ``ValidationError``;
    drop ``input_value`` reprs + pydantic.dev URLs, cap at *max_errors* with
    ``(+N more)`` suffix.
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

    Also catch ``ValidationError`` from inside tool body (e.g. result model
    construction); compacted message still name offending paths.
    """

    async def on_call_tool(
        self,
        context: MiddlewareContext[CallToolRequestParams],
        call_next: CallNext[CallToolRequestParams, ToolResult],
    ) -> ToolResult:
        try:
            return await call_next(context)
        except (ValidationError, FastMCPValidationError) as exc:
            # fastmcp >=3.4.3 wraps tool-arg pydantic errors in its own
            # ValidationError (original on __cause__); tool-body errors stay raw
            # pydantic. Normalise to the pydantic error before compacting.
            pydantic_exc = exc if isinstance(exc, ValidationError) else exc.__cause__
            if not isinstance(pydantic_exc, ValidationError):
                raise
            raise ToolError(
                compact_validation_error(context.message.name, pydantic_exc)
            ) from exc
