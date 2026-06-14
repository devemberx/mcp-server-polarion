"""Tests for the tool-argument ValidationError compaction middleware: the pure
``compact_validation_error`` seam plus the ``on_call_tool`` wrapper behaviour.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from fastmcp.exceptions import ToolError
from pydantic import BaseModel, ValidationError

from mcp_server_polarion.middleware import (
    CompactValidationErrorMiddleware,
    compact_validation_error,
)


class _Link(BaseModel):
    role: str
    target: str


class _Args(BaseModel):
    project_id: str
    links: list[_Link]


def _validation_error(payload: dict[str, object]) -> ValidationError:
    try:
        _Args.model_validate(payload)
    except ValidationError as exc:
        return exc
    msg = "payload unexpectedly validated"
    raise AssertionError(msg)


class TestCompactValidationError:
    """Tests for `compact_validation_error(tool_name, exc)`."""

    def test_names_tool_and_field(self) -> None:
        exc = _validation_error({"links": []})
        msg = compact_validation_error("create_work_items", exc)
        assert msg.startswith("Invalid arguments for tool 'create_work_items':")
        assert "project_id: Field required" in msg

    def test_renders_nested_loc_path_dotted(self) -> None:
        exc = _validation_error(
            {"project_id": "P", "links": [{"role": "a"}, {"target": "b"}]}
        )
        msg = compact_validation_error("create_work_item_links", exc)
        assert "links.0.target" in msg
        assert "links.1.role" in msg

    def test_drops_input_value_and_pydantic_url(self) -> None:
        exc = _validation_error(
            {"project_id": "P", "links": [{"role": "a", "target": 123}]}
        )
        msg = compact_validation_error("create_work_item_links", exc)
        assert "input_value" not in msg
        assert "errors.pydantic.dev" not in msg

    def test_caps_error_count(self) -> None:
        # 25 malformed link entries → 25 errors, capped at the default 20.
        payload = {"project_id": "P", "links": [{} for _ in range(25)]}
        exc = _validation_error(payload)
        msg = compact_validation_error("create_work_item_links", exc, max_errors=20)
        assert "(+" in msg and "more)" in msg
        assert msg.count(";") <= 20


class TestCompactValidationErrorMiddleware:
    """Tests for `CompactValidationErrorMiddleware.on_call_tool`."""

    async def test_compacts_validation_error_into_tool_error(self) -> None:
        mw = CompactValidationErrorMiddleware()
        context = SimpleNamespace(message=SimpleNamespace(name="create_work_items"))

        async def call_next(_ctx: object) -> object:
            raise _validation_error({"links": []})

        with pytest.raises(ToolError) as exc:
            await mw.on_call_tool(context, call_next)  # type: ignore[arg-type]

        msg = str(exc.value)
        assert "Invalid arguments for tool 'create_work_items'" in msg
        assert "input_value" not in msg

    async def test_passes_through_successful_result(self) -> None:
        mw = CompactValidationErrorMiddleware()
        context = SimpleNamespace(message=SimpleNamespace(name="list_projects"))
        sentinel = object()

        async def call_next(_ctx: object) -> object:
            return sentinel

        result = await mw.on_call_tool(context, call_next)  # type: ignore[arg-type]
        assert result is sentinel

    async def test_does_not_swallow_other_exceptions(self) -> None:
        mw = CompactValidationErrorMiddleware()
        context = SimpleNamespace(message=SimpleNamespace(name="get_work_item"))

        async def call_next(_ctx: object) -> object:
            raise RuntimeError("backend down")

        with pytest.raises(RuntimeError, match="backend down"):
            await mw.on_call_tool(context, call_next)  # type: ignore[arg-type]
