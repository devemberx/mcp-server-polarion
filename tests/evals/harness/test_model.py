"""Model-factory tests; env-driven, pinned via ``monkeypatch.setenv``/``delenv``."""

from __future__ import annotations

import pytest

# ``model`` imports ``strands.models.litellm`` at load; skip on the bare install.
pytest.importorskip("strands")

from evals.harness.model import (
    DEFAULT_MODEL,
    build_model,
    resolve_model_id,
)


class TestResolveModelId:
    def test_default_when_unset(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("EVAL_MODEL", raising=False)
        assert resolve_model_id() == DEFAULT_MODEL

    def test_env_override(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("EVAL_MODEL", "ollama/qwen2.5-coder:7b")
        assert resolve_model_id() == "ollama/qwen2.5-coder:7b"


class TestBuildModel:
    def test_temperature_is_pinned_to_zero(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv("EVAL_MODEL_BASE_URL", raising=False)
        model = build_model()
        assert model.get_config()["params"]["temperature"] == 0.0

    def test_parallel_tool_calls_pinned_off(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv("EVAL_MODEL_BASE_URL", raising=False)
        params = build_model().get_config()["params"]
        assert params["parallel_tool_calls"] is False
        assert params["drop_params"] is True

    def test_retries_and_timeout_from_env(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("EVAL_NUM_RETRIES", "3")
        monkeypatch.setenv("EVAL_LLM_TIMEOUT", "42")
        params = build_model().get_config()["params"]
        assert params["num_retries"] == 3
        assert params["timeout"] == 42.0

    def test_base_url_absent_by_default(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("EVAL_MODEL_BASE_URL", raising=False)
        # No base url -> no api_base routing (LiteLLM normalises None to {}).
        assert "api_base" not in (build_model().client_args or {})

    def test_base_url_routed_when_set(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("EVAL_MODEL_BASE_URL", "http://localhost:11434")
        assert build_model().client_args == {"api_base": "http://localhost:11434"}
