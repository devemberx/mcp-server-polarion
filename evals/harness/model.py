"""Eval-agent model factory: one LiteLLM adapter, backend switched via
``EVAL_MODEL`` (e.g. ``openai/gpt-4o-mini``, ``ollama/...`` + base URL).
``temperature=0`` / ``parallel_tool_calls=False`` keep gate stable;
``EVAL_NUM_RETRIES``/``EVAL_LLM_TIMEOUT`` absorb transient 429s.
"""

from __future__ import annotations

import os

from strands.models.litellm import LiteLLMModel

DEFAULT_MODEL = "openai/gpt-4o-mini"


def resolve_model_id() -> str:
    """Agent model id -- single source of truth.

    ``build_model`` + gate report both read this, so recorded model always
    match one driven.
    """
    return os.environ.get("EVAL_MODEL", DEFAULT_MODEL)


def build_model() -> LiteLLMModel:
    """Construct agent-under-test model from environment configuration."""
    model_id = resolve_model_id()
    base_url = os.environ.get("EVAL_MODEL_BASE_URL")

    client_args: dict[str, object] = {}
    if base_url:
        # litellm route both OpenAI-compatible + Ollama traffic via api_base.
        client_args["api_base"] = base_url

    return LiteLLMModel(
        client_args=client_args or None,
        model_id=model_id,
        params={
            "temperature": 0.0,
            # Some providers double-emit tool call in one parallel block --
            # nondeterminism no docstring can steer, pin off like temperature.
            # drop_params let flag-less backends (e.g. Ollama) ignore it.
            "parallel_tool_calls": False,
            "drop_params": True,
            "num_retries": max(0, int(os.environ.get("EVAL_NUM_RETRIES", "10"))),
            "timeout": max(1.0, float(os.environ.get("EVAL_LLM_TIMEOUT", "60"))),
        },
    )
