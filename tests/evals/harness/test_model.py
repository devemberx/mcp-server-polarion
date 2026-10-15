"""Model-factory tests; env-driven, pinned via ``monkeypatch.setenv``/``delenv``."""

from __future__ import annotations

import pytest

# ``model`` import ``strands.models.litellm`` at load; skip on bare install.
pytest.importorskip("strands")
pytest.importorskip("litellm")

from litellm.main import responses_api_bridge_check

from evals.harness.model import (
    DEFAULT_MODEL,
    DEFAULT_REASONING_EFFORT,
    build_model,
    resolve_model_id,
    resolve_reasoning_effort,
)


class TestResolveModelId:
    def test_default_when_unset(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("EVAL_MODEL", raising=False)
        assert resolve_model_id() == DEFAULT_MODEL

    def test_default_is_luna_tier(self) -> None:
        # Bare `openai/gpt-5.6` route to Sol -- gate price/behaviour differ.
        assert DEFAULT_MODEL == "openai/gpt-5.6-luna"

    def test_env_override(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("EVAL_MODEL", "openai/gpt-5.6-terra")
        assert resolve_model_id() == "openai/gpt-5.6-terra"


class TestResolveReasoningEffort:
    def test_default_when_unset(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("EVAL_REASONING_EFFORT", raising=False)
        assert resolve_reasoning_effort() == DEFAULT_REASONING_EFFORT == "medium"

    def test_env_override(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("EVAL_REASONING_EFFORT", "high")
        assert resolve_reasoning_effort() == "high"

    def test_blank_env_falls_back_to_default(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("EVAL_REASONING_EFFORT", "  ")
        assert resolve_reasoning_effort() == DEFAULT_REASONING_EFFORT


class TestBuildModel:
    def test_parallel_tool_calls_pinned_off(self) -> None:
        assert build_model().get_config()["params"]["parallel_tool_calls"] is False

    def test_reasoning_effort_always_sent(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Explicit effort = litellm Responses-API bridge trigger; absent param
        # send tools over chat-completions, which gpt-5.6 reject.
        monkeypatch.setenv("EVAL_REASONING_EFFORT", "low")
        assert build_model().get_config()["params"]["reasoning_effort"] == "low"

    def test_no_temperature_and_no_drop_params(self) -> None:
        params = build_model().get_config()["params"]
        # gpt-5 family accept temperature=1 only; drop_params would mask a
        # silently discarded reasoning_effort.
        assert "temperature" not in params
        assert "drop_params" not in params

    def test_retries_and_timeout_from_env(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("EVAL_NUM_RETRIES", "3")
        monkeypatch.setenv("EVAL_LLM_TIMEOUT", "42")
        params = build_model().get_config()["params"]
        assert params["num_retries"] == 3
        assert params["timeout"] == 42.0

    def test_timeout_default_covers_reasoning_latency(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv("EVAL_LLM_TIMEOUT", raising=False)
        assert build_model().get_config()["params"]["timeout"] == 180.0


class TestResponsesApiBridge:
    """Upstream-regression alarm: litellm unpinned, so routing can shift.

    gpt-5.6 + function tools 400 on ``/v1/chat/completions``; litellm dodge it
    by bridging to the Responses API, but only when ``reasoning_effort`` ride
    along. Chat routing here = whole gate 400s at run time.
    """

    @staticmethod
    def _bridge_mode(reasoning_effort: str | None) -> str | None:
        provider, _, model = DEFAULT_MODEL.partition("/")
        model_info, _ = responses_api_bridge_check(
            model=model,
            custom_llm_provider=provider,
            tools=[{"type": "function", "function": {"name": "list_projects"}}],
            reasoning_effort=reasoning_effort,
        )
        mode = model_info.get("mode")
        return str(mode) if mode is not None else None

    def test_default_model_with_effort_routes_to_responses(self) -> None:
        assert self._bridge_mode(DEFAULT_REASONING_EFFORT) == "responses"

    def test_effortless_call_stays_on_chat(self) -> None:
        # Pin why build_model never omit param.
        assert self._bridge_mode(None) == "chat"
