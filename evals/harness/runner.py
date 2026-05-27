"""Run a single eval case end to end and return its trajectory.

``run_case`` is the ``task`` callable handed to
``strands_evals.Experiment.run_evaluations``. It is synchronous (the
Experiment API requires that) and drives the whole async stack inside one
``asyncio.run`` under an active respx mock:

    Strands Agent -> bridged MCP tools -> in-memory FastMCP server
        -> PolarionClient -> respx -> FakePolarion

The agent's LLM traffic is *not* mocked — respx is created with
``assert_all_mocked=False`` so it falls through to the real provider, while
every Polarion request is served by the fake. No real Polarion is touched.
"""

from __future__ import annotations

import asyncio
import os
from typing import Any

import respx
from fastmcp import Client
from strands import Agent
from strands_evals import Case
from strands_evals.types import TaskOutput

import mcp_server_polarion.core.client as _client_mod
from mcp_server_polarion.server import mcp

from .fake_polarion import POLARION_HOST, PROJECT, FakePolarion
from .mcp_bridge import TrajectoryRecorder, build_agent_tools
from .model import build_model

# Deliberately generic: it must NOT teach the agent the Tier-1 rules, or the
# eval would test the prompt rather than the tool docstrings (the only guard).
SYSTEM_PROMPT = (
    "You are an assistant with read/write access to a Polarion ALM instance "
    "through the provided tools. Use the tools to fulfil the user's request. "
    f"The project id is '{PROJECT}'. The default space id is '_default'. "
    "Choose tools by reading their descriptions. Stop once the request is done."
)


def _extract_text(result: Any) -> str:
    message = getattr(result, "message", None)
    if isinstance(message, dict):
        blocks = message.get("content", []) or []
        texts = [b.get("text", "") for b in blocks if isinstance(b, dict)]
        return "\n".join(t for t in texts if t)
    return str(result)


def _set_polarion_env() -> None:
    os.environ.setdefault("POLARION_URL", POLARION_HOST)
    os.environ.setdefault("POLARION_TOKEN", "fake-token")


async def _run_case_async(case: Case, recorder: TrajectoryRecorder) -> str:
    async with Client(mcp) as mcp_client:
        tools = await build_agent_tools(mcp_client, recorder)
        agent = Agent(
            model=build_model(),
            tools=tools,
            system_prompt=SYSTEM_PROMPT,
        )
        try:
            result = await agent.invoke_async(case.input)
            return _extract_text(result)
        except Exception as exc:
            return f"<agent-error: {type(exc).__name__}: {exc}>"


def run_case(case: Case) -> TaskOutput:
    """Drive one case and return its tool-call trajectory as a ``TaskOutput``."""
    _set_polarion_env()
    recorder = TrajectoryRecorder()
    fake = FakePolarion()

    old_delay = _client_mod._WRITE_DELAY_SECONDS
    _client_mod._WRITE_DELAY_SECONDS = 0.0
    try:
        with respx.mock(assert_all_mocked=False, assert_all_called=False) as router:
            fake.install(router)
            output = asyncio.run(_run_case_async(case, recorder))
    finally:
        _client_mod._WRITE_DELAY_SECONDS = old_delay

    return TaskOutput(
        input=case.input,
        output=output,
        trajectory=recorder.calls,
    )
