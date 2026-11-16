"""Run one eval case end to end. ``run_case`` = synchronous ``task`` callable
for ``strands_evals``; one ``asyncio.run`` drive Agent -> bridged tools ->
in-memory server -> respx -> FakePolarion. LLM traffic bypass respx and reach
real provider: litellm default aiohttp transport replace httpcore under
``httpx.AsyncClient``, respx patch httpcore only. Polarion never touched.
"""

from __future__ import annotations

import asyncio
import os
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any, Final

import openai
import respx
from fastmcp import Client
from strands import Agent
from strands.hooks import BeforeModelCallEvent, HookRegistry
from strands.types.exceptions import ModelThrottledException
from strands_evals import Case
from strands_evals.types import TaskOutput

import mcp_server_polarion.core.client as _client_mod
import mcp_server_polarion.tools._shared.cache as _cache_mod
from mcp_server_polarion.server import mcp

from .fake_polarion import FakePolarion
from .fixtures import POLARION_HOST, PROJECT
from .mcp_bridge import TrajectoryRecorder, build_agent_tools
from .model import build_model

# Runaway-agent caps: _MAX_CYCLES count BeforeModelCallEvent firings;
# _CASE_TIMEOUT_SECONDS = wall-clock ceiling on invoke_async.
_MAX_CYCLES: int = max(1, int(os.environ.get("EVAL_MAX_CYCLES", "10")))
_CASE_TIMEOUT_SECONDS: float = max(
    1.0, float(os.environ.get("EVAL_CASE_TIMEOUT", "600"))
)

# Deliberately generic: must NOT teach case rules, else eval test prompt
# instead of tool docstrings (only guard).
SYSTEM_PROMPT = (
    "You are an assistant with read/write access to a Polarion ALM instance "
    "through the provided tools. Use the tools to fulfil the user's request. "
    f"The project id is '{PROJECT}'. The default space id is '_default'. "
    "Choose tools by reading their descriptions. Stop once the request is done."
)

# Output prefix for agent that raised before finishing; gate fail any output
# starting with it (crashed agent clean verdict moot).
AGENT_ERROR_PREFIX = "<agent-error:"

# Output prefix for provider-side failure (throttle, timeout, 5xx, connection)
# surviving litellm retries; infra flake, not agent verdict.
INFRA_ERROR_PREFIX = "<infra-error:"

_INFRA_STATUS_CODES: Final[frozenset[int]] = frozenset({408, 429})
_MAX_CHAIN_DEPTH: Final[int] = 16

# litellm wrap unmapped exceptions (own bridge bugs) as APIConnectionError or
# InternalServerError, status 500, original only on implicit __context__.
# Programming error anywhere in chain = deterministic, re-run only burn time.
_BUG_TYPES: Final[tuple[type[BaseException], ...]] = (
    AssertionError,
    AttributeError,
    ImportError,
    LookupError,
    NameError,
    NotImplementedError,
    TypeError,
)

# OpenAI exhausted-billing 429; litellm keep marker in message only (code/body
# overwritten). Permanent until billing fixed.
_PERMANENT_MARKERS: Final[tuple[str, ...]] = ("insufficient_quota",)


def _chain_links(exc: BaseException) -> Iterator[BaseException]:
    """Yield links via ``__cause__``, ``__context__``, strands
    ``original_exception``; cycle-safe, depth-bounded.
    """
    seen: set[int] = set()
    stack: list[BaseException | None] = [exc]
    while stack and len(seen) < _MAX_CHAIN_DEPTH:
        link = stack.pop()
        if link is None or id(link) in seen:
            continue
        seen.add(id(link))
        yield link
        stack.append(link.__context__)
        stack.append(link.__cause__)
        original = getattr(link, "original_exception", None)
        if isinstance(original, BaseException):
            stack.append(original)


def _is_transient(link: BaseException) -> bool:
    if isinstance(link, ModelThrottledException | openai.APIConnectionError):
        return True
    status = getattr(link, "status_code", None)
    # bool = int subclass; exclude.
    return (
        isinstance(status, int)
        and not isinstance(status, bool)
        and (status in _INFRA_STATUS_CODES or status >= 500)
    )


def _is_permanent(link: BaseException) -> bool:
    if isinstance(link, _BUG_TYPES):
        return True
    text = str(link)
    return any(marker in text for marker in _PERMANENT_MARKERS)


def _is_infra_error(exc: BaseException) -> bool:
    """True if chain hold provider-side transient failure and no permanent one."""
    links = list(_chain_links(exc))
    if any(_is_permanent(link) for link in links):
        return False
    return any(_is_transient(link) for link in links)


def _error_output(exc: BaseException) -> str:
    prefix = INFRA_ERROR_PREFIX if _is_infra_error(exc) else AGENT_ERROR_PREFIX
    return f"{prefix} {type(exc).__name__}: {exc}>"


def _extract_text(result: Any) -> str:
    message = getattr(result, "message", None)
    if isinstance(message, dict):
        blocks = message.get("content", []) or []
        texts = [b.get("text", "") for b in blocks if isinstance(b, dict)]
        return "\n".join(t for t in texts if t)
    return str(result)


_PATCHED_ENV_VARS: Final[tuple[str, ...]] = (
    "POLARION_URL",
    "POLARION_TOKEN",
    "POLARION_MAX_REQUESTS_PER_SECOND",
)


def _set_polarion_env() -> None:
    # Hard-set, not setdefault: inherited real POLARION_URL would route writes
    # (respx match by host) to live instance.
    os.environ["POLARION_URL"] = POLARION_HOST
    os.environ["POLARION_TOKEN"] = "fake-token"
    # Mock backend throttle-free; inherited cap would pace every eval tool call.
    os.environ["POLARION_MAX_REQUESTS_PER_SECOND"] = "0"


@contextmanager
def _patched_polarion_env() -> Iterator[None]:
    """Scope fake env to one case — leaked cap 0 unpace later live clients."""
    saved = {name: os.environ.get(name) for name in _PATCHED_ENV_VARS}
    _set_polarion_env()
    try:
        yield
    finally:
        for name, value in saved.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value


class _CycleGuard:
    """Model-call counter fail-closing runaway agents. Class because Strands
    ``hooks=`` want ``HookProvider`` instances; caller fail-close on forced
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
            # No tool call yet = first model call never returned: provider stall
            # (litellm worst case (1 + retries) x timeout outlast case timeout).
            # After one, re-run could erase forbidden write -> plain FAIL.
            prefix = AGENT_ERROR_PREFIX if recorder.calls else INFRA_ERROR_PREFIX
            return f"{prefix} TimeoutError: case exceeded {_CASE_TIMEOUT_SECONDS:.0f}s>"
        except Exception as exc:
            return _error_output(exc)
        if cycle_guard.count > _MAX_CYCLES:
            return (
                f"{AGENT_ERROR_PREFIX} CycleGuard: exceeded {_MAX_CYCLES} model cycles>"
            )
        return _extract_text(result)


def _clear_server_caches() -> None:
    """Fresh fake world per case; process-wide TTL caches (document
    discovery, confirmed ids, enum options) would replay prior case's state.
    """
    for value in vars(_cache_mod).values():
        if isinstance(value, _cache_mod.TTLCache):
            value.clear()


def run_case(case: Case) -> TaskOutput:
    """Drive one case, return tool-call trajectory as ``TaskOutput``."""
    recorder = TrajectoryRecorder()
    fake = FakePolarion()
    _clear_server_caches()

    old_delay = _client_mod._WRITE_DELAY_SECONDS
    _client_mod._WRITE_DELAY_SECONDS = 0.0
    try:
        with (
            _patched_polarion_env(),
            respx.mock(assert_all_mocked=False, assert_all_called=False) as router,
        ):
            fake.install(router)
            output = asyncio.run(_run_case_async(case, recorder))
    finally:
        _client_mod._WRITE_DELAY_SECONDS = old_delay

    return TaskOutput(
        input=case.input,
        output=output,
        trajectory=recorder.calls,
    )
