#!/usr/bin/env python3
"""Dump each MCP tool's LLM-facing char counts: docstring (-> description) vs
Field(description=...) params. Step 0 of shrink-mcp-tool-docs and the only
authoritative baseline — never assume a fixed tool count or total, the set drifts.

Run from outside the repo so the repo .env is not auto-loaded (a stray
OPENAI_API_KEY there shadows PolarionConfig):

    REPO=$(git rev-parse --show-toplevel)
    cd /tmp && uv run --project "$REPO" python \
      "$REPO"/.claude/skills/shrink-mcp-tool-docs/scripts/dump_schema.py
"""

from __future__ import annotations

import asyncio

from mcp_server_polarion.server import mcp


async def go() -> None:
    tools = sorted(await mcp.list_tools(), key=lambda t: t.name)
    rows: list[tuple[str, int, int, int]] = []
    for t in tools:
        props = (t.parameters or {}).get("properties", {})
        desc_n = len(t.description or "")
        param_n = sum(len(s.get("description", "")) for s in props.values())
        rows.append((t.name, desc_n, param_n, desc_n + param_n))
    rows.sort(key=lambda r: -r[3])
    total = sum(r[3] for r in rows)
    print(f"{'tool':34}{'desc':>6}{'param':>7}{'total':>7}")
    for name, d, p, tot in rows:
        print(f"{name:34}{d:6}{p:7}{tot:7}")
    print(f"{'TOTAL LLM-facing chars':34}{total:20}")


if __name__ == "__main__":
    asyncio.run(go())
