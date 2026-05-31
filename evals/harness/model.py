"""Model factory for the eval agent.

A single LiteLLM adapter serves both cloud and local backends — switch with
the ``EVAL_MODEL`` env var, no code change:

    EVAL_MODEL=openai/gpt-4o-mini            # default, CI (needs OPENAI_API_KEY)
    EVAL_MODEL=ollama/qwen2.5-coder:7b       # local (set EVAL_MODEL_BASE_URL)

``temperature`` is pinned to 0.0 for non-reasoning models to minimise
run-to-run flakiness so the zero-tolerance gate stays stable. OpenAI
reasoning models (o1/o3 families) reject ``temperature`` — those skip the
param. ``EVAL_NUM_RETRIES`` / ``EVAL_LLM_TIMEOUT`` forward to LiteLLM so
transient 429/RateLimitError from cloud providers gets absorbed by
exponential backoff rather than failing the case.
"""

from __future__ import annotations

import os

import litellm
from strands.models.litellm import LiteLLMModel

DEFAULT_MODEL = "openai/gpt-4o-mini"


def resolve_model_id() -> str:
    """Return the model id the agent will use — the single source of truth.

    Both ``build_model`` and the gate's report read this, so the recorded
    model always matches the one actually driven.
    """
    return os.environ.get("EVAL_MODEL", DEFAULT_MODEL)


def _is_reasoning_model(model_id: str) -> bool:
    # OpenAI o1/o3 families reject `temperature` and use `max_completion_tokens`.
    tail = model_id.split("/", 1)[-1].lower()
    return tail.startswith(("o1", "o3"))


def build_model() -> LiteLLMModel:
    """Construct the agent-under-test model from environment configuration."""
    model_id = resolve_model_id()
    base_url = os.environ.get("EVAL_MODEL_BASE_URL")

    # Belt-and-suspenders: tell LiteLLM to silently drop params a model
    # rejects, so a future reasoning-model addition cannot 400 the gate.
    litellm.drop_params = True

    client_args: dict[str, object] = {}
    if base_url:
        # litellm routes both OpenAI-compatible and Ollama traffic via api_base.
        client_args["api_base"] = base_url

    params: dict[str, object] = {
        "num_retries": max(0, int(os.environ.get("EVAL_NUM_RETRIES", "10"))),
        "timeout": max(1.0, float(os.environ.get("EVAL_LLM_TIMEOUT", "60"))),
    }
    if not _is_reasoning_model(model_id):
        params["temperature"] = 0.0

    return LiteLLMModel(
        client_args=client_args or None,
        model_id=model_id,
        params=params,
    )
