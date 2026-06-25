"""Run one eval case end to end. ``run_case`` is the synchronous ``task``
callable for ``strands_evals``; one ``asyncio.run`` drives Agent -> bridged
tools -> in-memory server -> respx -> FakePolarion. LLM traffic falls through
to the real provider (``assert_all_mocked=False``); Polarion never touched.
"""

from __future__ import annotations

import asyncio
import os
from typing import Any

import respx
from fastmcp import Client
from strands import Agent
from strands.hooks import BeforeModelCallEvent, HookRegistry
from strands_evals import Case
from strands_evals.types import TaskOutput

import mcp_server_polarion.core.client as _client_mod
from mcp_server_polarion.server import mcp

from .fake_polarion import FakePolarion
from .fixtures import POLARION_HOST, PROJECT
from .mcp_bridge import TrajectoryRecorder, build_agent_tools
from .model import build_model

# Runaway-agent caps: _MAX_CYCLES counts BeforeModelCallEvent firings;
# _CASE_TIMEOUT_SECONDS is a wall-clock ceiling on invoke_async.
_MAX_CYCLES: int = max(1, int(os.environ.get("EVAL_MAX_CYCLES", "10")))
_CASE_TIMEOUT_SECONDS: float = max(
    1.0, float(os.environ.get("EVAL_CASE_TIMEOUT", "120"))
)

# Deliberately generic: must NOT teach the case rules, else the eval tests
# the prompt instead of the tool docstrings (the only guard).
SYSTEM_PROMPT = (
    "You are an assistant with read/write access to a Polarion ALM instance "
    "through the provided tools. Use the tools to fulfil the user's request. "
    f"The project id is '{PROJECT}'. The default space id is '_default'. "
    "Choose tools by reading their descriptions. Stop once the request is done."
)

# Output prefix for an agent that raised before finishing; the gate fails any
# output starting with it (a crashed agent's clean verdict is moot).
AGENT_ERROR_PREFIX = "<agent-error:"


def _extract_text(result: Any) -> str:
    message = getattr(result, "message", None)
    if isinstance(message, dict):
        blocks = message.get("content", []) or []
        texts = [b.get("text", "") for b in blocks if isinstance(b, dict)]
        return "\n".join(t for t in texts if t)
    return str(result)


def _set_polarion_env() -> None:
    # Hard-set, not setdefault: an inherited real POLARION_URL would route writes
    # (respx matches by host) to a live instance.
    os.environ["POLARION_URL"] = POLARION_HOST
    os.environ["POLARION_TOKEN"] = "fake-token"


class _CycleGuard:
    """Model-call counter fail-closing runaway agents. A class because Strands'
    ``hooks=`` wants ``HookProvider`` instances; caller fail-closes on a forced
    stop (clean ``stop_event_loop`` could silently pass with empty text).
    """

    def __init__(self, max_cycles: int) -> None:
        self._max_cycles = max_cycles
        self.count = 0

    def register_hooks(self, registry: HookRegistry, **_: object) -> None:
        registry.add_callback(BeforeModelCallEvent, self._on_before_model_call)

    def _on_before_model_call(self, event: BeforeModelCallEvent) -> None:
        self.count += 1
        if self.count > self._max_cycles:
            rs: dict[str, object] = event.invocation_state.setdefault(
                "request_state", {}
            )
            rs["stop_event_loop"] = True


async def _run_case_async(case: Case, recorder: TrajectoryRecorder) -> str:
    async with Client(mcp) as mcp_client:
        tools = await build_agent_tools(mcp_client, recorder)
        cycle_guard = _CycleGuard(_MAX_CYCLES)
        agent = Agent(
            model=build_model(),
            tools=tools,
            system_prompt=SYSTEM_PROMPT,
            hooks=[cycle_guard],
        )
        try:
            result = await asyncio.wait_for(
                agent.invoke_async(case.input),
                timeout=_CASE_TIMEOUT_SECONDS,
            )
        except TimeoutError:
            return (
                f"{AGENT_ERROR_PREFIX} TimeoutError: "
                f"case exceeded {_CASE_TIMEOUT_SECONDS:.0f}s>"
            )
        except Exception as exc:
            return f"{AGENT_ERROR_PREFIX} {type(exc).__name__}: {exc}>"
        if cycle_guard.count > _MAX_CYCLES:
            return (
                f"{AGENT_ERROR_PREFIX} CycleGuard: exceeded {_MAX_CYCLES} model cycles>"
            )
        return _extract_text(result)


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
