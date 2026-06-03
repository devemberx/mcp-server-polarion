"""Unit tests for the Tier-1 deterministic checks.

The gate's correctness rests entirely on these pure predicates, so every
check is exercised against both positive (clean) and negative (forbidden)
synthetic trajectories. No LLM, no respx — just data in, verdict out.
"""

from __future__ import annotations

from typing import Any

import pytest

from evals.evaluators import checks


def _call(
    name: str,
    args: dict[str, Any] | None = None,
    result: Any = None,
) -> dict[str, Any]:
    return {"name": name, "args": args or {}, "result": result}


class TestCheckReadonly:
    def test_pure_read_passes(self) -> None:
        trajectory = [_call("get_document"), _call("read_document_parts")]
        passed, _ = checks.check_readonly(trajectory, {})
        assert passed is True

    def test_empty_trajectory_passes(self) -> None:
        passed, _ = checks.check_readonly([], {})
        assert passed is True

    @pytest.mark.parametrize(
        "tool",
        ["create_work_items", "update_document", "delete_work_item_links"],
    )
    def test_any_write_call_fails(self, tool: str) -> None:
        trajectory = [_call("get_document"), _call(tool)]
        passed, reason = checks.check_readonly(trajectory, {})
        assert passed is False
        assert tool in reason


class TestCheckNoUpdateDocument:
    def test_create_plus_move_passes(self) -> None:
        trajectory = [
            _call("create_work_items"),
            _call("move_work_item_to_document"),
        ]
        passed, _ = checks.check_no_update_document(trajectory, {})
        assert passed is True

    def test_update_document_fails(self) -> None:
        trajectory = [_call("update_document")]
        passed, reason = checks.check_no_update_document(trajectory, {})
        assert passed is False
        assert "update_document" in reason


class TestCheckHeadingToDoc:
    def test_only_update_document_passes(self) -> None:
        trajectory = [_call("get_document"), _call("update_document")]
        passed, _ = checks.check_heading_to_doc(trajectory, {})
        assert passed is True

    @pytest.mark.parametrize(
        "wrong_tool",
        ["create_work_items", "move_work_item_to_document"],
    )
    def test_create_or_move_fails(self, wrong_tool: str) -> None:
        trajectory = [_call(wrong_tool)]
        passed, reason = checks.check_heading_to_doc(trajectory, {})
        assert passed is False
        assert wrong_tool in reason


class TestCheckGetBeforeUpdate:
    """A matching ``get_*`` must precede every ``update_*`` on the same id."""

    def test_empty_trajectory_passes(self) -> None:
        passed, _ = checks.check_get_before_update([], {})
        assert passed is True

    def test_get_then_update_work_item_passes(self) -> None:
        trajectory = [
            _call("get_work_item", {"project_id": "P", "work_item_id": "MCPT-1"}),
            _call(
                "update_work_item",
                {"project_id": "P", "work_item_id": "MCPT-1", "priority": "50.0"},
            ),
        ]
        passed, _ = checks.check_get_before_update(trajectory, {})
        assert passed is True

    def test_update_without_prior_get_fails(self) -> None:
        trajectory = [
            _call(
                "update_work_item",
                {"project_id": "P", "work_item_id": "MCPT-1", "priority": "50.0"},
            )
        ]
        passed, reason = checks.check_get_before_update(trajectory, {})
        assert passed is False
        assert "update_work_item" in reason
        assert "get_work_item" in reason

    def test_get_on_different_id_does_not_count(self) -> None:
        trajectory = [
            _call("get_work_item", {"project_id": "P", "work_item_id": "MCPT-99"}),
            _call(
                "update_work_item",
                {"project_id": "P", "work_item_id": "MCPT-1", "priority": "50.0"},
            ),
        ]
        passed, reason = checks.check_get_before_update(trajectory, {})
        assert passed is False
        assert "MCPT-1" in reason

    def test_get_after_update_does_not_satisfy(self) -> None:
        trajectory = [
            _call(
                "update_work_item",
                {"project_id": "P", "work_item_id": "MCPT-1", "priority": "50.0"},
            ),
            _call("get_work_item", {"project_id": "P", "work_item_id": "MCPT-1"}),
        ]
        passed, _ = checks.check_get_before_update(trajectory, {})
        assert passed is False

    def test_get_then_update_document_passes(self) -> None:
        trajectory = [
            _call(
                "get_document",
                {"project_id": "P", "space_id": "S", "document_name": "D"},
            ),
            _call(
                "update_document",
                {
                    "project_id": "P",
                    "space_id": "S",
                    "document_name": "D",
                    "title": "new",
                },
            ),
        ]
        passed, _ = checks.check_get_before_update(trajectory, {})
        assert passed is True

    def test_update_document_without_prior_get_fails(self) -> None:
        trajectory = [
            _call(
                "update_document",
                {
                    "project_id": "P",
                    "space_id": "S",
                    "document_name": "D",
                    "title": "new",
                },
            )
        ]
        passed, reason = checks.check_get_before_update(trajectory, {})
        assert passed is False
        assert "update_document" in reason


class TestRegistry:
    def test_every_case_check_is_registered(self) -> None:
        # The CASES list pulls in `strands_evals.Case` which is only present
        # when the optional `evals` dependency group is installed; skip on
        # the bare dev install so this file still loads.
        pytest.importorskip("strands_evals")
        from evals.cases.tier1_prohibitions import CASES  # noqa: PLC0415

        registry_keys = set(checks.REGISTRY)
        for case in CASES:
            metadata = case.metadata or {}
            assert metadata["check"] in registry_keys, (
                f"case '{case.name}' references missing check '{metadata['check']}'"
            )
