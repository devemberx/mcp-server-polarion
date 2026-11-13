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
        # Gate verdict + shrink-mcp-tool-docs boundaries model-specific;
        # change = re-baseline.
        assert DEFAULT_MODEL == "openai/gpt-6-luna"

    def test_env_override(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("EVAL_MODEL", "openai/gpt-6.1-sol")
        assert resolve_model_id() == "openai/gpt-6.1-sol"


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
        # Explicit effort = Responses-API bridge on any OpenAI base; chat
        # completions reject function tools while gpt-6-luna reason.
        monkeypatch.setenv("EVAL_REASONING_EFFORT", "low")
        assert build_model().get_config()["params"]["reasoning_effort"] == "low"

    def test_no_temperature_and_no_drop_params(self) -> None:
        params = build_model().get_config()["params"]
        # gpt-6 reject temperature unless effort none; drop_params would mask
        # a silently discarded reasoning_effort.
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

    gpt-6-luna + function tools 400 on ``/v1/chat/completions`` unless effort
    ``none``; litellm dodge it by bridging to the Responses API. Chat routing
    here = whole gate 400s at run time.
    """

    @staticmethod
    def _bridge_mode(reasoning_effort: str, api_base: str | None = None) -> str | None:
        provider, _, model = DEFAULT_MODEL.partition("/")
        model_info, _ = responses_api_bridge_check(
            model=model,
            custom_llm_provider=provider,
            tools=[{"type": "function", "function": {"name": "list_projects"}}],
            reasoning_effort=reasoning_effort,
            api_base=api_base,
        )
        mode = model_info.get("mode")
        return str(mode) if mode is not None else None

    # Mirror reasoning efforts evals-on-demand.yml offer, minus chat-safe none.
    @pytest.mark.parametrize("effort", ["low", "medium", "high", "xhigh", "max"])
    def test_reasoning_effort_routes_to_responses(self, effort: str) -> None:
        assert self._bridge_mode(effort) == "responses"

    def test_explicit_effort_bridges_custom_openai_base(self) -> None:
        # Unset effort bridge only on hosts litellm recognise as OpenAI --
        # why build_model never omit param.
        mode = self._bridge_mode(
            DEFAULT_REASONING_EFFORT, api_base="https://llm-proxy.example.com/v1"
        )
        assert mode == "responses"
