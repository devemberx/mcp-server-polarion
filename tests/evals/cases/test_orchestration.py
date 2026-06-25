"""Orchestration case-definition invariants: the ``ordered_trajectory`` check,
``min_pass_rate == 0.8``, documented intent, auto-derived covers, and a
well-formed step DSL (each step names a tool; every ``observed_in`` references a
tool that appears earlier in the same case's steps, so the threaded id can
actually have been produced).
"""

from __future__ import annotations

import pytest

pytest.importorskip("strands_evals")

from evals.cases.orchestration import CASES, _case
from evals.evaluators import checks
from tests.mcp_server_polarion.test_mcp_transport import EXPECTED_TOOL_NAMES


def _params(case: object) -> dict[str, object]:
    metadata = getattr(case, "metadata", None) or {}
    params = metadata.get("params", {})
    assert isinstance(params, dict)
    return params


def _tools(step: dict[str, object]) -> list[str]:
    tool = step["tool"]
    return [tool] if isinstance(tool, str) else list(tool)


def _single_tool(step: dict[str, object]) -> set[str]:
    """A step's tool name only if it names exactly one tool -- the names an
    ``after``/``observed_in`` dep may resolve to unambiguously."""
    tools = _tools(step)
    return set(tools) if len(tools) == 1 else set()


class TestCases:
    def test_every_case_uses_the_ordered_trajectory_check(self) -> None:
        for case in CASES:
            assert (case.metadata or {})["check"] == "ordered_trajectory"

    def test_ordered_trajectory_check_is_registered(self) -> None:
        assert "ordered_trajectory" in checks.REGISTRY

    def test_every_case_uses_efficiency_threshold(self) -> None:
        for case in CASES:
            assert (case.metadata or {}).get("min_pass_rate") == 0.8

    def test_every_case_documents_intent(self) -> None:
        for case in CASES:
            intent = (case.metadata or {}).get("intent")
            assert isinstance(intent, str) and intent.strip(), case.name

    def test_covers_is_derived_from_steps_and_real(self) -> None:
        for case in CASES:
            covers = (case.metadata or {}).get("covers")
            assert isinstance(covers, list) and covers, case.name
            assert set(covers) <= EXPECTED_TOOL_NAMES, case.name
            derived = {t for step in _params(case)["steps"] for t in _tools(step)}
            assert set(covers) == derived, case.name

    def test_every_case_declares_at_least_one_step(self) -> None:
        for case in CASES:
            steps = _params(case).get("steps", [])
            assert isinstance(steps, list) and steps, case.name

    def test_every_step_names_a_tool(self) -> None:
        for case in CASES:
            for step in _params(case)["steps"]:
                assert isinstance(step, dict)
                assert _tools(step), case.name

    def test_observed_source_appears_earlier_in_steps(self) -> None:
        # An ``observed_in`` tool with no earlier matching step can never be
        # satisfied -- guard against a typo'd sequence. The source must be a
        # single-tool step: ``name_to_step`` resolves a multi-tool alternative
        # group ambiguously (a dep could point at a step that matched the *other*
        # alternative), so the threaded id would not provably come from it.
        for case in CASES:
            steps = _params(case)["steps"]
            seen_tools: set[str] = set()
            for step in steps:
                if step.get("observed_arg") is not None:
                    source = step["observed_in"]
                    assert source in seen_tools, (
                        f"{case.name}: observed_in '{source}' precedes no "
                        "single-tool step"
                    )
                    assert step.get("observed_path"), case.name
                seen_tools.update(_single_tool(step))

    def test_after_references_an_earlier_step_tool(self) -> None:
        # ``after`` deps must reference a single-tool step for the same reason as
        # ``observed_in`` -- a multi-tool group's index is ambiguous.
        for case in CASES:
            steps = _params(case)["steps"]
            seen_tools: set[str] = set()
            for step in steps:
                for dep in step.get("after", []):
                    assert dep in seen_tools, (
                        f"{case.name}: 'after' dep '{dep}' precedes no single-tool step"
                    )
                seen_tools.update(_single_tool(step))

    def test_helper_builds_expected_metadata_shape(self) -> None:
        case = _case(
            "ORCH-X",
            "do a thing",
            intent="walk the path",
            steps=[{"tool": "get_document"}],
        )
        assert case.name == "ORCH-X"
        assert case.input == "do a thing"
        assert case.metadata == {
            "check": "ordered_trajectory",
            "params": {"steps": [{"tool": "get_document"}]},
            "min_pass_rate": 0.8,
            "intent": "walk the path",
            "covers": ["get_document"],
        }
