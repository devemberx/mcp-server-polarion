"""Mechanical style gate for LLM-facing tool text — walk every registered
tool's description + full input schema (incl ``$defs`` spec models), enforce
template bans + budgets so drift fail CI, not eval.
"""

from __future__ import annotations

import re
from collections.abc import Iterator

import pytest
from fastmcp import Client
from mcp.types import Tool

from mcp_server_polarion.server import mcp

# Caps = current max + headroom (desc 1110 / param 230). Bump = deliberate
# decision in review, not silent drift.
_MAX_TOOL_DESC_CHARS = 1300
_MAX_PARAM_DESC_CHARS = 400

# Prevention-form rule: no exception class names, no raw HTTP status codes.
_EXCEPTION_NAME = re.compile(r"\b[A-Z]\w*Error\b")
_HTTP_STATUS_CODE = re.compile(r"\b(20[0-9]|30[0-9]|4[0-9]{2}|5[0-9]{2})\b")

# dry_run phrasing deliberate per write shape — byte-exact, no paraphrase.
_DRY_RUN_VARIANTS: frozenset[str] = frozenset(
    {
        "Preview payload without calling Polarion.",
        "Preview payload without writing; guards still query Polarion.",
        "Preview payload without deleting; the pre-read still queries Polarion.",
    }
)


@pytest.fixture
async def all_tools(monkeypatch: pytest.MonkeyPatch) -> list[Tool]:
    """Every registered tool as the MCP client see it."""
    monkeypatch.setenv("POLARION_URL", "https://polarion.example.com")
    monkeypatch.setenv("POLARION_TOKEN", "test-token-secret")
    async with Client(mcp) as client:
        return list(await client.list_tools())


def _iter_descriptions(node: object, path: str) -> Iterator[tuple[str, str]]:
    """Yield (schema path, text) for every description in input schema."""
    if isinstance(node, dict):
        for key, value in node.items():
            if key == "description" and isinstance(value, str):
                yield path, value
            else:
                yield from _iter_descriptions(value, f"{path}.{key}")
    elif isinstance(node, list):
        for index, value in enumerate(node):
            yield from _iter_descriptions(value, f"{path}[{index}]")


def _all_texts(tools: list[Tool]) -> Iterator[tuple[str, str]]:
    """Yield (location, text) over tool descriptions + schema descriptions."""
    for tool in tools:
        yield f"{tool.name}", tool.description or ""
        yield from _iter_descriptions(tool.inputSchema, tool.name)


class TestDescriptionStyleGate:
    """Template bans hold across every tool + every shipped Field text."""

    async def test_no_exception_type_names(self, all_tools: list[Tool]) -> None:
        bad = [
            (where, match.group())
            for where, text in _all_texts(all_tools)
            if (match := _EXCEPTION_NAME.search(text))
        ]
        assert not bad, (
            f"Exception class names in shipped text (rephrase as prevention: "
            f"what to call first / what is rejected): {bad}"
        )

    async def test_no_raw_http_status_codes(self, all_tools: list[Tool]) -> None:
        bad = [
            (where, match.group())
            for where, text in _all_texts(all_tools)
            if (match := _HTTP_STATUS_CODE.search(text))
        ]
        assert not bad, (
            f"Raw HTTP status codes in shipped text (state the conflict/limit "
            f"in words instead): {bad}"
        )

    async def test_no_rst_double_backticks(self, all_tools: list[Tool]) -> None:
        bad = [where for where, text in _all_texts(all_tools) if "``" in text]
        assert not bad, f"RST double-backticks ship as noise — plain identifiers: {bad}"

    async def test_schema_descriptions_single_line(self, all_tools: list[Tool]) -> None:
        bad = [
            where
            for tool in all_tools
            for where, text in _iter_descriptions(tool.inputSchema, tool.name)
            if "\n" in text
        ]
        assert not bad, (
            f"Multi-line Field/spec-model descriptions (one line each; long "
            f"guidance belongs in the tool docstring): {bad}"
        )

    async def test_tool_description_budget(self, all_tools: list[Tool]) -> None:
        bad = [
            (tool.name, len(tool.description or ""))
            for tool in all_tools
            if len(tool.description or "") > _MAX_TOOL_DESC_CHARS
        ]
        assert not bad, (
            f"Tool descriptions over {_MAX_TOOL_DESC_CHARS} chars — trim or "
            f"raise the cap deliberately: {bad}"
        )

    async def test_param_description_budget(self, all_tools: list[Tool]) -> None:
        bad = [
            (where, len(text))
            for tool in all_tools
            for where, text in _iter_descriptions(tool.inputSchema, tool.name)
            if len(text) > _MAX_PARAM_DESC_CHARS
        ]
        assert not bad, (
            f"Param descriptions over {_MAX_PARAM_DESC_CHARS} chars — move "
            f"guidance into the tool docstring: {bad}"
        )

    async def test_dry_run_descriptions_byte_exact(self, all_tools: list[Tool]) -> None:
        bad = [
            (tool.name, props["dry_run"].get("description"))
            for tool in all_tools
            if "dry_run" in (props := tool.inputSchema.get("properties", {}))
            and props["dry_run"].get("description") not in _DRY_RUN_VARIANTS
        ]
        assert not bad, (
            f"dry_run description must be one approved variant (no paraphrase): {bad}"
        )
