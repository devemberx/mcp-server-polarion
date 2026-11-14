"""Runner tests: text extractor, runaway-agent ``_CycleGuard``, hermetic env
pinning. End-to-end ``run_case`` (real agent + LLM) out of scope.
"""

from __future__ import annotations

import asyncio
import os
from typing import Any

import httpx
import litellm
import openai
import pytest

# ``runner`` import ``strands`` / ``strands_evals`` at load; skip on bare install.
pytest.importorskip("strands_evals")

from strands.types.exceptions import (
    ContextWindowOverflowException,
    EventLoopException,
    ModelThrottledException,
)
from strands_evals import Case

from evals.harness import runner
from evals.harness.mcp_bridge import TrajectoryRecorder
from evals.harness.runner import (
    AGENT_ERROR_PREFIX,
    INFRA_ERROR_PREFIX,
    POLARION_HOST,
    _CycleGuard,
    _error_output,
    _extract_text,
    _is_infra_error,
    _patched_polarion_env,
    _run_case_async,
    _set_polarion_env,
)


class _Result:
    def __init__(self, message: Any) -> None:
        self.message = message


class TestExtractText:
    def test_joins_text_blocks_from_dict_message(self) -> None:
        result = _Result({"content": [{"text": "hello"}, {"text": "world"}]})
        assert _extract_text(result) == "hello\nworld"

    def test_skips_blocks_without_text(self) -> None:
        result = _Result({"content": [{"text": ""}, {"other": 1}, {"text": "kept"}]})
        assert _extract_text(result) == "kept"

    def test_non_dict_message_falls_back_to_str(self) -> None:
        result = _Result("plain")  # message is a str, not a dict
        assert _extract_text(result) == str(result)


class _Event:
    def __init__(self) -> None:
        self.invocation_state: dict[str, Any] = {}


class TestCycleGuard:
    def test_counts_model_calls(self) -> None:
        guard = _CycleGuard(max_cycles=3)
        for _ in range(3):
            guard._on_before_model_call(_Event())
        assert guard.count == 3

    def test_does_not_stop_within_budget(self) -> None:
        guard = _CycleGuard(max_cycles=3)
        event = _Event()
        guard._on_before_model_call(event)
        assert event.invocation_state == {}

    def test_trips_stop_event_loop_past_budget(self) -> None:
        guard = _CycleGuard(max_cycles=1)
        guard._on_before_model_call(_Event())  # count -> 1, still within budget
        event = _Event()
        guard._on_before_model_call(event)  # count -> 2, over budget
        assert event.invocation_state["request_state"]["stop_event_loop"] is True


class TestSetPolarionEnv:
    def test_hard_sets_all_vars(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("POLARION_URL", "https://real.example.com")
        monkeypatch.delenv("POLARION_TOKEN", raising=False)
        # Inherited cap would pace every eval tool call — must lose to "0".
        monkeypatch.setenv("POLARION_MAX_REQUESTS_PER_SECOND", "3")
        _set_polarion_env()
        assert os.environ["POLARION_URL"] == POLARION_HOST
        assert os.environ["POLARION_TOKEN"] == "fake-token"
        assert os.environ["POLARION_MAX_REQUESTS_PER_SECOND"] == "0"


class TestPatchedPolarionEnv:
    """Fake env scoped to the case, never leaked to later clients."""

    def test_restores_previous_values(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("POLARION_URL", "https://real.example.com")
        monkeypatch.setenv("POLARION_TOKEN", "real-token")
        monkeypatch.setenv("POLARION_MAX_REQUESTS_PER_SECOND", "2")

        with _patched_polarion_env():
            assert os.environ["POLARION_MAX_REQUESTS_PER_SECOND"] == "0"

        assert os.environ["POLARION_URL"] == "https://real.example.com"
        assert os.environ["POLARION_TOKEN"] == "real-token"
        assert os.environ["POLARION_MAX_REQUESTS_PER_SECOND"] == "2"

    def test_unsets_vars_absent_before(self, monkeypatch: pytest.MonkeyPatch) -> None:
        for name in (
            "POLARION_URL",
            "POLARION_TOKEN",
            "POLARION_MAX_REQUESTS_PER_SECOND",
        ):
            monkeypatch.delenv(name, raising=False)

        with _patched_polarion_env():
            assert os.environ["POLARION_URL"] == POLARION_HOST

        assert "POLARION_URL" not in os.environ
        assert "POLARION_MAX_REQUESTS_PER_SECOND" not in os.environ

    def test_restores_on_exception(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("POLARION_MAX_REQUESTS_PER_SECOND", "2")

        with pytest.raises(RuntimeError), _patched_polarion_env():
            raise RuntimeError("case blew up")

        assert os.environ["POLARION_MAX_REQUESTS_PER_SECOND"] == "2"


def test_agent_error_prefix_is_a_sentinel() -> None:
    assert AGENT_ERROR_PREFIX.startswith("<agent-error")
    assert INFRA_ERROR_PREFIX.startswith("<infra-error")


def test_error_prefixes_are_not_prefixes_of_each_other() -> None:
    assert not AGENT_ERROR_PREFIX.startswith(INFRA_ERROR_PREFIX)
    assert not INFRA_ERROR_PREFIX.startswith(AGENT_ERROR_PREFIX)


class _Status(Exception):
    def __init__(self, status_code: object) -> None:
        super().__init__("status")
        self.status_code = status_code


def _req_error() -> openai.APIConnectionError:
    return openai.APIConnectionError(request=httpx.Request("POST", "https://x"))


def _timeout() -> litellm.Timeout:
    return litellm.Timeout("t", model="m", llm_provider="p")


def _rate_limit() -> litellm.RateLimitError:
    return litellm.RateLimitError("r", llm_provider="p", model="m")


def _server_error() -> litellm.InternalServerError:
    return litellm.InternalServerError("s", llm_provider="p", model="m")


def _bad_request() -> litellm.BadRequestError:
    return litellm.BadRequestError("b", model="m", llm_provider="p")


def _chained(inner: BaseException) -> BaseException:
    try:
        try:
            raise inner
        except BaseException as exc:
            raise RuntimeError("outer") from exc
    except RuntimeError as outer:
        return outer


def _cyclic() -> BaseException:
    a, b = RuntimeError("a"), RuntimeError("b")
    a.__cause__ = b
    b.__cause__ = a
    return a


_INFRA_CASES: list[tuple[str, BaseException, bool]] = [
    ("throttled", ModelThrottledException("x"), True),
    ("api-connection", _req_error(), True),
    ("litellm-timeout", _timeout(), True),
    ("litellm-rate-limit", _rate_limit(), True),
    ("litellm-500", _server_error(), True),
    ("litellm-400", _bad_request(), False),
    ("context-overflow", ContextWindowOverflowException("x"), False),
    ("builtin-timeout", TimeoutError(), False),
    ("plain", RuntimeError("x"), False),
    ("wrapped-throttle", EventLoopException(ModelThrottledException("x")), True),
    ("wrapped-timeout", EventLoopException(_timeout()), True),
    ("wrapped-400", EventLoopException(_bad_request()), False),
    ("chained-rate-limit", _chained(_rate_limit()), True),
    ("chained-plain", _chained(RuntimeError("in")), False),
    ("cyclic", _cyclic(), False),
    *[
        (f"status-{code}", _Status(code), infra)
        for code, infra in [
            (403, False),
            (400, False),
            (404, False),
            (408, True),
            (422, False),
            (429, True),
            (500, True),
            (529, True),
            (None, False),
            (True, False),
        ]
    ],
]


@pytest.mark.parametrize(
    ("exc", "expected"),
    [pytest.param(e, x, id=i) for i, e, x in _INFRA_CASES],
)
def test_is_infra_error(exc: BaseException, expected: bool) -> None:
    assert _is_infra_error(exc) is expected


def _long_chain(plain_links: int) -> BaseException:
    tail: BaseException = _rate_limit()
    for _ in range(plain_links):
        head = RuntimeError("link")
        head.__cause__ = tail
        tail = head
    return tail


class TestChainDepthBound:
    def test_infra_link_within_bound_found(self) -> None:
        assert _is_infra_error(_long_chain(runner._MAX_CHAIN_DEPTH - 1)) is True

    def test_infra_link_beyond_bound_ignored(self) -> None:
        assert _is_infra_error(_long_chain(20)) is False


class TestErrorOutput:
    def test_infra_prefix_and_message(self) -> None:
        out = _error_output(_rate_limit())
        assert out.startswith(INFRA_ERROR_PREFIX)
        assert "RateLimitError" in out
        assert out.endswith(">")

    def test_agent_prefix_and_message(self) -> None:
        out = _error_output(RuntimeError("boom"))
        assert out == f"{AGENT_ERROR_PREFIX} RuntimeError: boom>"


class _RaisingAgent:
    exc: BaseException = RuntimeError("unset")

    def __init__(self, **_: object) -> None:
        pass

    async def invoke_async(self, _: object) -> object:
        raise self.exc


@pytest.mark.parametrize(
    ("exc", "prefix"),
    [
        (ModelThrottledException("slow down"), INFRA_ERROR_PREFIX),
        (RuntimeError("boom"), AGENT_ERROR_PREFIX),
    ],
)
def test_run_case_async_classifies_exception(
    monkeypatch: pytest.MonkeyPatch, exc: BaseException, prefix: str
) -> None:
    class _Agent(_RaisingAgent):
        pass

    _Agent.exc = exc
    monkeypatch.setattr(runner, "Agent", _Agent)
    monkeypatch.setattr(runner, "build_model", object)
    with _patched_polarion_env():
        out = asyncio.run(
            _run_case_async(Case(name="c", input="hi"), TrajectoryRecorder())
        )
    assert out.startswith(prefix)
