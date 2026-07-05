"""Shared Case factory: every behaviour category builds the one metadata shape
(``check``/``params``/``min_pass_rate``/``intent``/``covers``) that ``run.py``
and ``CheckDispatchEvaluator`` consume. Category files bind their threshold via
a thin ``_case`` wrapper around :func:`make_case`.
"""

from __future__ import annotations

from typing import Required, TypedDict

from strands_evals import Case


class Step(TypedDict, total=False):
    """One ``ordered_trajectory`` step: the accepted tool(s), arg constraints,
    and ordering/id-threading deps. Mirrors the DSL read back out of untyped
    ``Case.metadata`` by ``evaluators.checks.check_ordered_trajectory``.
    """

    tool: Required[str | list[str]]
    match: dict[str, str]
    after: list[str]
    observed_arg: str | list[str]
    observed_in: str
    observed_path: str


def make_case(
    name: str,
    prompt: str,
    check: str,
    *,
    intent: str,
    covers: list[str],
    min_pass_rate: float,
    **params: object,
) -> Case:
    return Case(
        name=name,
        input=prompt,
        metadata={
            "check": check,
            "params": params,
            "min_pass_rate": min_pass_rate,
            "intent": intent,
            "covers": covers,
        },
    )
