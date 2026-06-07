"""Model factory for the eval agent.

A single LiteLLM adapter serves both cloud and local backends — switch with
the ``EVAL_MODEL`` env var, no code change:

    EVAL_MODEL=openai/gpt-4o-mini            # default, CI (needs OPENAI_API_KEY)
    EVAL_MODEL=ollama/qwen2.5-coder:7b       # local (set EVAL_MODEL_BASE_URL)

``temperature`` is pinned to 0.0 to keep the zero-tolerance gate stable.
``EVAL_NUM_RETRIES`` / ``EVAL_LLM_TIMEOUT`` forward to LiteLLM so transient
cloud 429/RateLimitError gets absorbed by backoff, not failing the case.
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
            "num_retries": max(0, int(os.environ.get("EVAL_NUM_RETRIES", "10"))),
            "timeout": max(1.0, float(os.environ.get("EVAL_LLM_TIMEOUT", "60"))),
        },
    )
