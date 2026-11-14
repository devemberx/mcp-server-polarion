"""Eval-agent model factory: one LiteLLM adapter, model switched via
``EVAL_MODEL``, reasoning depth via ``EVAL_REASONING_EFFORT``.
``reasoning_effort`` ship explicit always: gpt-6-luna serve function tools on
``/v1/chat/completions`` only at effort ``none``, so reasoning run must ride the
Responses API. Explicit effort = litellm bridge on any OpenAI base (unset bridge
only on hosts litellm recognise as OpenAI) + effort pinned vs server-default
drift. ``parallel_tool_calls=False`` keep gate stable;
``EVAL_NUM_RETRIES``/``EVAL_LLM_TIMEOUT`` absorb transient 429s.
"""

from __future__ import annotations

import os

from strands.models.litellm import LiteLLMModel

# Luna = gpt-6 high-volume tier.
DEFAULT_MODEL = "openai/gpt-6-luna"
DEFAULT_REASONING_EFFORT = "medium"


def resolve_model_id() -> str:
    """Agent model id -- single source of truth.

    ``build_model`` + gate report both read this, so recorded model always
    match one driven.
    """
    return os.environ.get("EVAL_MODEL", DEFAULT_MODEL)


def resolve_reasoning_effort() -> str:
    """Agent reasoning effort -- single source of truth, same as model id.

    Same model at different effort = different gate verdict, so report record
    it next to the model.
    """
    effort = os.environ.get("EVAL_REASONING_EFFORT", "").strip()
    return effort or DEFAULT_REASONING_EFFORT


def build_model() -> LiteLLMModel:
    """Construct agent-under-test model from environment configuration."""
    return LiteLLMModel(
        model_id=resolve_model_id(),
        params={
            # Some providers double-emit tool call in one parallel block --
            # nondeterminism no docstring can steer, pin off.
            "parallel_tool_calls": False,
            "reasoning_effort": resolve_reasoning_effort(),
            "num_retries": max(0, int(os.environ.get("EVAL_NUM_RETRIES", "10"))),
            "timeout": max(1.0, float(os.environ.get("EVAL_LLM_TIMEOUT", "180"))),
        },
        # No drop_params: silently dropped reasoning_effort would run whole gate
        # unreasoned and still report pass.
    )
