"""Eval-agent model factory: one LiteLLM adapter, backend switched via
``EVAL_MODEL`` (e.g. ``openai/gpt-4o-mini``, ``ollama/...`` + base URL).
``temperature=0`` / ``parallel_tool_calls=False`` keep the gate stable;
``EVAL_NUM_RETRIES``/``EVAL_LLM_TIMEOUT`` absorb transient 429s.
"""

from __future__ import annotations

import os

from strands.models.litellm import LiteLLMModel

DEFAULT_MODEL = "openai/gpt-4o-mini"


def resolve_model_id() -> str:
    """Return the agent's model id -- single source of truth.

    ``build_model`` and the gate report both read this, so the recorded model
    always matches the one driven.
    """
    return os.environ.get("EVAL_MODEL", DEFAULT_MODEL)


def build_model() -> LiteLLMModel:
    """Construct the agent-under-test model from environment configuration."""
    model_id = resolve_model_id()
    base_url = os.environ.get("EVAL_MODEL_BASE_URL")

    client_args: dict[str, object] = {}
    if base_url:
        # litellm routes both OpenAI-compatible and Ollama traffic via api_base.
        client_args["api_base"] = base_url

    return LiteLLMModel(
        client_args=client_args or None,
        model_id=model_id,
        params={
            "temperature": 0.0,
            # Some providers emit the same tool call twice in one parallel
            # block -- nondeterminism no docstring can steer, pinned off like
            # temperature. drop_params lets backends without the flag
            # (e.g. Ollama) ignore it.
            "parallel_tool_calls": False,
            "drop_params": True,
            "num_retries": max(0, int(os.environ.get("EVAL_NUM_RETRIES", "10"))),
            "timeout": max(1.0, float(os.environ.get("EVAL_LLM_TIMEOUT", "60"))),
        },
    )
