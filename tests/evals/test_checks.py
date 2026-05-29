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
        ["create_work_item", "update_document", "delete_work_item_links"],
    )
    def test_any_write_call_fails(self, tool: str) -> None:
        trajectory = [_call("get_document"), _call(tool)]
        passed, reason = checks.check_readonly(trajectory, {})
        assert passed is False
        assert tool in reason


class TestCheckNoUpdateDocument:
    def test_create_plus_move_passes(self) -> None:
        trajectory = [
            _call("create_work_item"),
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
        ["create_work_item", "move_work_item_to_document"],
    )
    def test_create_or_move_fails(self, wrong_tool: str) -> None:
        trajectory = [_call(wrong_tool)]
        passed, reason = checks.check_heading_to_doc(trajectory, {})
        assert passed is False
        assert wrong_tool in reason


def _enum_result(option_ids: list[str]) -> dict[str, Any]:
    return {"items": [{"id": opt_id, "name": opt_id} for opt_id in option_ids]}


class TestCheckEnumBeforeCreate:
    def test_no_create_passes(self) -> None:
        passed, _ = checks.check_enum_before_create([], {})
        assert passed is True

    def test_listed_and_used_passes(self) -> None:
        trajectory = [
            _call(
                "list_work_item_enum_options",
                {"field_id": "type"},
                _enum_result(["task", "issue"]),
            ),
            _call(
                "list_work_item_enum_options",
                {"field_id": "severity"},
                _enum_result(["must_have", "should_have"]),
            ),
            _call("create_work_item", {"type": "task", "severity": "must_have"}),
        ]
        passed, _ = checks.check_enum_before_create(trajectory, {})
        assert passed is True

    def test_missing_field_listing_fails(self) -> None:
        trajectory = [
            _call(
                "list_work_item_enum_options",
                {"field_id": "type"},
                _enum_result(["task"]),
            ),
            _call("create_work_item", {"type": "task", "severity": "must_have"}),
        ]
        passed, reason = checks.check_enum_before_create(trajectory, {})
        assert passed is False
        assert "severity" in reason

    def test_ghost_value_not_in_listed_fails(self) -> None:
        trajectory = [
            _call(
                "list_work_item_enum_options",
                {"field_id": "type"},
                _enum_result(["task"]),
            ),
            _call("create_work_item", {"type": "ghost_type"}),
        ]
        passed, reason = checks.check_enum_before_create(trajectory, {})
        assert passed is False
        assert "ghost_type" in reason

    def test_omitted_enum_args_skipped(self) -> None:
        trajectory = [
            _call(
                "list_work_item_enum_options",
                {"field_id": "type"},
                _enum_result(["task"]),
            ),
            _call("create_work_item", {"type": "task", "severity": None}),
        ]
        passed, _ = checks.check_enum_before_create(trajectory, {})
        assert passed is True


def _wi_result(custom_fields: dict[str, str]) -> dict[str, Any]:
    return {"id": "MCPT-200", "custom_fields": custom_fields}


class TestCheckCustomFieldKeysKnown:
    def test_no_custom_fields_passes(self) -> None:
        trajectory = [_call("update_work_item", {"work_item_id": "MCPT-200"})]
        passed, _ = checks.check_custom_field_keys_known(trajectory, {})
        assert passed is True

    def test_reused_known_key_passes(self) -> None:
        trajectory = [
            _call(
                "get_work_item",
                {"work_item_id": "MCPT-200"},
                _wi_result({"acceptance_criteria_id": "AC-1"}),
            ),
            _call(
                "update_work_item",
                {
                    "work_item_id": "MCPT-200",
                    "custom_fields": {"acceptance_criteria_id": "AC-42"},
                },
            ),
        ]
        passed, _ = checks.check_custom_field_keys_known(trajectory, {})
        assert passed is True

    def test_unknown_key_without_prior_get_fails(self) -> None:
        trajectory = [
            _call(
                "update_work_item",
                {
                    "work_item_id": "MCPT-200",
                    "custom_fields": {"release_train_id": "RT-42"},
                },
            )
        ]
        passed, reason = checks.check_custom_field_keys_known(trajectory, {})
        assert passed is False
        assert "release_train_id" in reason or "without reading" in reason

    def test_unknown_key_with_prior_get_fails(self) -> None:
        trajectory = [
            _call(
                "get_work_item",
                {"work_item_id": "MCPT-200"},
                _wi_result({"acceptance_criteria_id": "AC-1"}),
            ),
            _call(
                "update_work_item",
                {
                    "work_item_id": "MCPT-200",
                    "custom_fields": {"release_train_id": "RT-42"},
                },
            ),
        ]
        passed, reason = checks.check_custom_field_keys_known(trajectory, {})
        assert passed is False
        assert "release_train_id" in reason


class TestCheckPriorityInListedOptions:
    def test_no_priority_passes(self) -> None:
        trajectory = [_call("update_work_item", {"work_item_id": "MCPT-200"})]
        passed, _ = checks.check_priority_in_listed_options(trajectory, {})
        assert passed is True

    def test_listed_and_used_passes(self) -> None:
        trajectory = [
            _call(
                "list_work_item_enum_options",
                {"field_id": "priority"},
                _enum_result(["90.0", "50.0", "10.0"]),
            ),
            _call(
                "update_work_item",
                {"work_item_id": "MCPT-200", "priority": "90.0"},
            ),
        ]
        passed, _ = checks.check_priority_in_listed_options(trajectory, {})
        assert passed is True

    def test_skipped_listing_fails(self) -> None:
        trajectory = [
            _call(
                "update_work_item",
                {"work_item_id": "MCPT-200", "priority": "999.0"},
            )
        ]
        passed, reason = checks.check_priority_in_listed_options(trajectory, {})
        assert passed is False
        assert "without first listing" in reason

    def test_out_of_range_value_fails(self) -> None:
        trajectory = [
            _call(
                "list_work_item_enum_options",
                {"field_id": "priority"},
                _enum_result(["90.0", "50.0", "10.0"]),
            ),
            _call(
                "update_work_item",
                {"work_item_id": "MCPT-200", "priority": "999.0"},
            ),
        ]
        passed, reason = checks.check_priority_in_listed_options(trajectory, {})
        assert passed is False
        assert "999.0" in reason


class TestCheckTypeListedBeforeMove:
    def test_no_create_passes(self) -> None:
        passed, _ = checks.check_type_listed_before_move([], {})
        assert passed is True

    def test_create_without_move_passes(self) -> None:
        trajectory = [_call("create_work_item", {"type": "epic"})]
        passed, _ = checks.check_type_listed_before_move(trajectory, {})
        assert passed is True

    def test_listed_type_then_move_passes(self) -> None:
        trajectory = [
            _call(
                "list_work_item_enum_options",
                {"field_id": "type"},
                _enum_result(["task", "requirement"]),
            ),
            _call("create_work_item", {"type": "task"}),
            _call("move_work_item_to_document", {"work_item_id": "MCPT-9001"}),
        ]
        passed, _ = checks.check_type_listed_before_move(trajectory, {})
        assert passed is True

    def test_ghost_type_then_move_fails(self) -> None:
        trajectory = [
            _call(
                "list_work_item_enum_options",
                {"field_id": "type"},
                _enum_result(["task", "requirement"]),
            ),
            _call("create_work_item", {"type": "epic"}),
            _call("move_work_item_to_document", {"work_item_id": "MCPT-9001"}),
        ]
        passed, reason = checks.check_type_listed_before_move(trajectory, {})
        assert passed is False
        assert "epic" in reason

    def test_unlisted_type_without_listing_then_move_fails(self) -> None:
        trajectory = [
            _call("create_work_item", {"type": "epic"}),
            _call("move_work_item_to_document", {"work_item_id": "MCPT-9001"}),
        ]
        passed, reason = checks.check_type_listed_before_move(trajectory, {})
        assert passed is False
        assert "epic" in reason


class TestCheckDocumentTypeListed:
    def test_no_create_passes(self) -> None:
        passed, _ = checks.check_document_type_listed([], {})
        assert passed is True

    def test_listed_and_used_passes(self) -> None:
        trajectory = [
            _call(
                "list_document_enum_options",
                {"field_id": "type"},
                _enum_result(["systemRequirementSpecification", "generic"]),
            ),
            _call("create_document", {"type": "generic"}),
        ]
        passed, _ = checks.check_document_type_listed(trajectory, {})
        assert passed is True

    def test_skipped_listing_fails(self) -> None:
        trajectory = [
            _call("create_document", {"type": "productRequirementSpecification"})
        ]
        passed, reason = checks.check_document_type_listed(trajectory, {})
        assert passed is False
        assert "without first listing" in reason

    def test_ghost_type_after_listing_fails(self) -> None:
        trajectory = [
            _call(
                "list_document_enum_options",
                {"field_id": "type"},
                _enum_result(["systemRequirementSpecification"]),
            ),
            _call("create_document", {"type": "productRequirementSpecification"}),
        ]
        passed, reason = checks.check_document_type_listed(trajectory, {})
        assert passed is False
        assert "productRequirementSpecification" in reason


class TestCheckUpdateDocumentIds:
    def test_no_update_passes(self) -> None:
        passed, _ = checks.check_update_document_ids([], {})
        assert passed is True

    def test_headings_only_passes(self) -> None:
        trajectory = [
            _call(
                "update_document",
                {"home_page_content_html": "<h1>A</h1><h3>B</h3>"},
            )
        ]
        passed, _ = checks.check_update_document_ids(trajectory, {})
        assert passed is True

    def test_stamped_paragraph_passes(self) -> None:
        trajectory = [
            _call(
                "update_document",
                {"home_page_content_html": '<p id="polarion_mcp_1">x</p>'},
            )
        ]
        passed, _ = checks.check_update_document_ids(trajectory, {})
        assert passed is True

    @pytest.mark.parametrize(
        "html",
        [
            "<p>raw paragraph</p>",
            "<ul><li>x</li></ul>",
            "<table><tr><td>x</td></tr></table>",
        ],
    )
    def test_anchorless_block_fails(self, html: str) -> None:
        trajectory = [_call("update_document", {"home_page_content_html": html})]
        passed, reason = checks.check_update_document_ids(trajectory, {})
        assert passed is False
        assert "without an id" in reason


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
