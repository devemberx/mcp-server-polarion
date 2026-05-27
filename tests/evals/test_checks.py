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


class TestCheckNoMoveHeading:
    def test_no_move_passes(self) -> None:
        passed, _ = checks.check_no_move_heading([], {"heading_ids": ["MCPT-201"]})
        assert passed is True

    def test_move_of_non_heading_passes(self) -> None:
        trajectory = [
            _call("move_work_item_to_document", {"work_item_id": "MCPT-200"}),
        ]
        passed, _ = checks.check_no_move_heading(
            trajectory, {"heading_ids": ["MCPT-201"]}
        )
        assert passed is True

    def test_move_of_heading_fails(self) -> None:
        trajectory = [
            _call("move_work_item_to_document", {"work_item_id": "MCPT-201"}),
        ]
        passed, reason = checks.check_no_move_heading(
            trajectory, {"heading_ids": ["MCPT-201"]}
        )
        assert passed is False
        assert "MCPT-201" in reason


class TestCheckNoResolveReply:
    def test_no_update_passes(self) -> None:
        passed, _ = checks.check_no_resolve_reply([], {"reply_comment_ids": ["2"]})
        assert passed is True

    def test_root_comment_update_passes(self) -> None:
        trajectory = [_call("update_document_comment", {"comment_id": "1"})]
        passed, _ = checks.check_no_resolve_reply(
            trajectory, {"reply_comment_ids": ["2"]}
        )
        assert passed is True

    def test_reply_comment_update_fails(self) -> None:
        trajectory = [_call("update_document_comment", {"comment_id": "2"})]
        passed, reason = checks.check_no_resolve_reply(
            trajectory, {"reply_comment_ids": ["2"]}
        )
        assert passed is False
        assert "'2'" in reason


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


def _doc_list_result(pairs: list[tuple[str, str]]) -> dict[str, Any]:
    return {"items": [{"space_id": s, "document_name": d} for s, d in pairs]}


class TestCheckListBeforeCreateDocument:
    def test_no_create_passes(self) -> None:
        passed, _ = checks.check_list_before_create_document([], {})
        assert passed is True

    def test_listed_then_unique_name_passes(self) -> None:
        trajectory = [
            _call(
                "list_documents",
                result=_doc_list_result([("_default", "FakeDoc")]),
            ),
            _call(
                "create_document",
                {"space_id": "_default", "document_name": "NewDoc"},
            ),
        ]
        passed, _ = checks.check_list_before_create_document(trajectory, {})
        assert passed is True

    def test_skipped_listing_fails(self) -> None:
        trajectory = [
            _call(
                "create_document",
                {"space_id": "_default", "document_name": "NewDoc"},
            )
        ]
        passed, reason = checks.check_list_before_create_document(trajectory, {})
        assert passed is False
        assert "without first listing" in reason

    def test_duplicate_name_after_listing_fails(self) -> None:
        trajectory = [
            _call(
                "list_documents",
                result=_doc_list_result([("_default", "FakeDoc")]),
            ),
            _call(
                "create_document",
                {"space_id": "_default", "document_name": "FakeDoc"},
            ),
        ]
        passed, reason = checks.check_list_before_create_document(trajectory, {})
        assert passed is False
        assert "FakeDoc" in reason


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
