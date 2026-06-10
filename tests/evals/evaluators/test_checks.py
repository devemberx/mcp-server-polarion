"""Unit tests for the Tier-1 deterministic checks.

The gate's correctness rests entirely on these pure predicates, so every
check is exercised against both positive (clean) and negative (forbidden)
synthetic trajectories. No LLM, no respx — just data in, verdict out.
"""

from __future__ import annotations

from typing import Any, ClassVar

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


_DOC_ARGS = {"project_id": "P", "space_id": "S", "document_name": "D"}


class TestCheckResolveRootComment:
    _PARAMS: ClassVar[dict[str, Any]] = {"root_ids": ["1"]}

    def test_list_then_resolve_root_passes(self) -> None:
        trajectory = [
            _call("list_document_comments", dict(_DOC_ARGS)),
            _call("update_document_comment", {**_DOC_ARGS, "comment_id": "1"}),
        ]
        passed, _ = checks.check_resolve_root_comment(trajectory, self._PARAMS)
        assert passed is True

    def test_resolving_only_reply_fails(self) -> None:
        trajectory = [
            _call("list_document_comments", dict(_DOC_ARGS)),
            _call("update_document_comment", {**_DOC_ARGS, "comment_id": "2"}),
        ]
        passed, reason = checks.check_resolve_root_comment(trajectory, self._PARAMS)
        assert passed is False
        assert "root" in reason

    def test_full_segment_only_reply_id_still_fails(self) -> None:
        trajectory = [
            _call("list_document_comments", dict(_DOC_ARGS)),
            _call("update_document_comment", {**_DOC_ARGS, "comment_id": "P/S/D/2"}),
        ]
        passed, _ = checks.check_resolve_root_comment(trajectory, self._PARAMS)
        assert passed is False

    def test_root_plus_stray_reply_passes(self) -> None:
        # The reply attempt 400s loudly in real Polarion (server-guarded);
        # the root resolve already did the job.
        trajectory = [
            _call("list_document_comments", dict(_DOC_ARGS)),
            _call("update_document_comment", {**_DOC_ARGS, "comment_id": "1"}),
            _call("update_document_comment", {**_DOC_ARGS, "comment_id": "2"}),
        ]
        passed, _ = checks.check_resolve_root_comment(trajectory, self._PARAMS)
        assert passed is True

    def test_resolving_without_prior_list_fails(self) -> None:
        trajectory = [
            _call("update_document_comment", {**_DOC_ARGS, "comment_id": "1"})
        ]
        passed, reason = checks.check_resolve_root_comment(trajectory, self._PARAMS)
        assert passed is False
        assert "list_document_comments" in reason

    def test_read_only_trajectory_passes(self) -> None:
        trajectory = [_call("list_document_comments", dict(_DOC_ARGS))]
        passed, _ = checks.check_resolve_root_comment(trajectory, self._PARAMS)
        assert passed is True


class TestCheckPreserveHyperlinks:
    _PARAMS: ClassVar[dict[str, Any]] = {
        "work_item_id": "MCPT-200",
        "required_uris": ["https://specs.example.com/fake-spec"],
    }

    def test_full_list_update_passes(self) -> None:
        trajectory = [
            _call(
                "update_work_item",
                {
                    "project_id": "P",
                    "work_item_id": "MCPT-200",
                    "hyperlinks": [
                        {
                            "role": "ref_ext",
                            "uri": "https://specs.example.com/fake-spec",
                        },
                        {"role": "ref_ext", "uri": "https://example.com/new"},
                    ],
                },
            )
        ]
        passed, _ = checks.check_preserve_hyperlinks(trajectory, self._PARAMS)
        assert passed is True

    def test_dropping_existing_uri_fails(self) -> None:
        trajectory = [
            _call(
                "update_work_item",
                {
                    "project_id": "P",
                    "work_item_id": "MCPT-200",
                    "hyperlinks": [
                        {"role": "ref_ext", "uri": "https://example.com/new"}
                    ],
                },
            )
        ]
        passed, reason = checks.check_preserve_hyperlinks(trajectory, self._PARAMS)
        assert passed is False
        assert "fake-spec" in reason

    def test_update_not_touching_hyperlinks_passes(self) -> None:
        trajectory = [
            _call(
                "update_work_item",
                {"project_id": "P", "work_item_id": "MCPT-200", "title": "x"},
            )
        ]
        passed, _ = checks.check_preserve_hyperlinks(trajectory, self._PARAMS)
        assert passed is True

    def test_other_work_item_not_constrained(self) -> None:
        trajectory = [
            _call(
                "update_work_item",
                {
                    "project_id": "P",
                    "work_item_id": "MCPT-999",
                    "hyperlinks": [{"role": "ref_ext", "uri": "https://x.example"}],
                },
            )
        ]
        passed, _ = checks.check_preserve_hyperlinks(trajectory, self._PARAMS)
        assert passed is True


class TestCheckRoundTripSource:
    def test_flagged_get_then_body_write_passes(self) -> None:
        trajectory = [
            _call(
                "get_document",
                {**_DOC_ARGS, "include_homepage_content_html": True},
            ),
            _call(
                "update_document",
                {**_DOC_ARGS, "home_page_content_html": "<p id='a'>x</p>"},
            ),
        ]
        passed, _ = checks.check_round_trip_source(trajectory, {})
        assert passed is True

    def test_unflagged_get_does_not_satisfy(self) -> None:
        trajectory = [
            _call("get_document", dict(_DOC_ARGS)),
            _call(
                "update_document",
                {**_DOC_ARGS, "home_page_content_html": "<p id='a'>x</p>"},
            ),
        ]
        passed, reason = checks.check_round_trip_source(trajectory, {})
        assert passed is False
        assert "include_homepage_content_html" in reason

    def test_read_document_does_not_satisfy(self) -> None:
        trajectory = [
            _call("read_document", dict(_DOC_ARGS)),
            _call(
                "update_document",
                {**_DOC_ARGS, "home_page_content_html": "<p id='a'>x</p>"},
            ),
        ]
        passed, _ = checks.check_round_trip_source(trajectory, {})
        assert passed is False

    def test_metadata_only_update_not_constrained(self) -> None:
        trajectory = [_call("update_document", {**_DOC_ARGS, "title": "new"})]
        passed, _ = checks.check_round_trip_source(trajectory, {})
        assert passed is True

    def test_work_item_body_write_needs_flagged_get(self) -> None:
        trajectory = [
            _call("get_work_item", {"project_id": "P", "work_item_id": "MCPT-200"}),
            _call(
                "update_work_item",
                {
                    "project_id": "P",
                    "work_item_id": "MCPT-200",
                    "description_html": "<p>x</p>",
                },
            ),
        ]
        passed, _ = checks.check_round_trip_source(trajectory, {})
        assert passed is False

        trajectory[0]["args"]["include_description_html"] = True
        passed, _ = checks.check_round_trip_source(trajectory, {})
        assert passed is True


class TestCheckNoBlindDetach:
    _PARAMS: ClassVar[dict[str, Any]] = {"floating_ids": ["MCPT-200"]}

    def test_read_only_answer_passes(self) -> None:
        trajectory = [
            _call("get_work_item", {"project_id": "P", "work_item_id": "MCPT-200"})
        ]
        passed, _ = checks.check_no_blind_detach(trajectory, self._PARAMS)
        assert passed is True

    def test_detach_on_floating_item_fails(self) -> None:
        trajectory = [
            _call(
                "move_work_item_from_document",
                {"project_id": "P", "work_item_id": "MCPT-200"},
            )
        ]
        passed, reason = checks.check_no_blind_detach(trajectory, self._PARAMS)
        assert passed is False
        assert "MCPT-200" in reason

    def test_project_qualified_id_still_fails(self) -> None:
        trajectory = [
            _call(
                "move_work_item_from_document",
                {"project_id": "P", "work_item_id": "P/MCPT-200"},
            )
        ]
        passed, _ = checks.check_no_blind_detach(trajectory, self._PARAMS)
        assert passed is False

    def test_detach_on_attached_item_passes(self) -> None:
        trajectory = [
            _call(
                "move_work_item_from_document",
                {"project_id": "P", "work_item_id": "MCPT-100"},
            )
        ]
        passed, _ = checks.check_no_blind_detach(trajectory, self._PARAMS)
        assert passed is True


class TestCheckSingleBulkCreate:
    def test_one_bulk_call_passes(self) -> None:
        trajectory = [
            _call("create_work_items", {"items": [{"title": "a"}, {"title": "b"}]})
        ]
        passed, _ = checks.check_single_bulk_create(trajectory, {})
        assert passed is True

    def test_split_calls_fail(self) -> None:
        trajectory = [
            _call("create_work_items", {"items": [{"title": "a"}]}),
            _call("create_work_items", {"items": [{"title": "b"}]}),
        ]
        passed, reason = checks.check_single_bulk_create(trajectory, {})
        assert passed is False
        assert "2" in reason

    def test_dry_run_preview_not_counted(self) -> None:
        trajectory = [
            _call("create_work_items", {"items": [{"title": "a"}], "dry_run": True}),
            _call("create_work_items", {"items": [{"title": "a"}]}),
        ]
        passed, _ = checks.check_single_bulk_create(trajectory, {})
        assert passed is True

    def test_errored_call_not_counted(self) -> None:
        trajectory = [
            _call(
                "create_work_items",
                {"items": [{"title": "a"}]},
                result={"error": "ToolError: bad severity"},
            ),
            _call("create_work_items", {"items": [{"title": "a"}]}),
        ]
        passed, _ = checks.check_single_bulk_create(trajectory, {})
        assert passed is True


class TestCheckDirectRead:
    _PARAMS: ClassVar[dict[str, Any]] = {"work_item_id": "MCPT-200"}

    def test_direct_get_passes(self) -> None:
        trajectory = [
            _call("get_work_item", {"project_id": "P", "work_item_id": "MCPT-200"})
        ]
        passed, _ = checks.check_direct_read(trajectory, self._PARAMS)
        assert passed is True

    def test_list_scan_fails(self) -> None:
        trajectory = [
            _call("list_work_items", {"project_id": "P", "query": "id:MCPT-200"}),
            _call("get_work_item", {"project_id": "P", "work_item_id": "MCPT-200"}),
        ]
        passed, reason = checks.check_direct_read(trajectory, self._PARAMS)
        assert passed is False
        assert "list_work_items" in reason

    def test_no_read_at_all_fails(self) -> None:
        trajectory = [_call("list_projects", {})]
        passed, _ = checks.check_direct_read(trajectory, self._PARAMS)
        assert passed is False

    def test_extra_benign_reads_tolerated(self) -> None:
        trajectory = [
            _call("read_work_item", {"project_id": "P", "work_item_id": "MCPT-200"}),
            _call(
                "list_work_item_links",
                {"project_id": "P", "work_item_id": "MCPT-200"},
            ),
        ]
        passed, _ = checks.check_direct_read(trajectory, self._PARAMS)
        assert passed is True


class TestCheckNoDuplicateReads:
    def test_distinct_reads_pass(self) -> None:
        trajectory = [
            _call("get_document", dict(_DOC_ARGS)),
            _call("list_document_comments", dict(_DOC_ARGS)),
        ]
        passed, _ = checks.check_no_duplicate_reads(trajectory, {})
        assert passed is True

    def test_identical_state_read_fails(self) -> None:
        trajectory = [
            _call("get_document", dict(_DOC_ARGS)),
            _call("get_document", dict(_DOC_ARGS)),
        ]
        passed, reason = checks.check_no_duplicate_reads(trajectory, {})
        assert passed is False
        assert "get_document" in reason

    def test_state_reread_after_write_passes(self) -> None:
        trajectory = [
            _call("get_document", dict(_DOC_ARGS)),
            _call("update_document", {**_DOC_ARGS, "title": "x"}),
            _call("get_document", dict(_DOC_ARGS)),
        ]
        passed, _ = checks.check_no_duplicate_reads(trajectory, {})
        assert passed is True

    def test_stable_reread_even_after_write_fails(self) -> None:
        enum_args = {
            "project_id": "P",
            "field_id": "severity",
            "work_item_type": "task",
        }
        trajectory = [
            _call("list_work_item_enum_options", dict(enum_args)),
            _call("create_work_items", {"items": [{"title": "a"}]}),
            _call("list_work_item_enum_options", dict(enum_args)),
        ]
        passed, reason = checks.check_no_duplicate_reads(trajectory, {})
        assert passed is False
        assert "list_work_item_enum_options" in reason

    def test_different_args_not_duplicates(self) -> None:
        trajectory = [
            _call("list_work_items", {"project_id": "P", "page_number": 1}),
            _call("list_work_items", {"project_id": "P", "page_number": 2}),
        ]
        passed, _ = checks.check_no_duplicate_reads(trajectory, {})
        assert passed is True


class TestCheckScopedQueryUsesSql:
    def test_sql_prefixed_module_query_passes(self) -> None:
        trajectory = [
            _call(
                "list_work_items",
                {"project_id": "P", "query": "SQL:(... module ...)"},
            )
        ]
        passed, _ = checks.check_scoped_query_uses_sql(trajectory, {})
        assert passed is True

    def test_lucene_module_query_fails(self) -> None:
        trajectory = [
            _call("list_work_items", {"project_id": "P", "query": "module:FakeDoc"})
        ]
        passed, reason = checks.check_scoped_query_uses_sql(trajectory, {})
        assert passed is False
        assert "module" in reason

    def test_parts_path_passes(self) -> None:
        trajectory = [_call("read_document_parts", dict(_DOC_ARGS))]
        passed, _ = checks.check_scoped_query_uses_sql(trajectory, {})
        assert passed is True

    def test_plain_query_passes(self) -> None:
        trajectory = [
            _call("list_work_items", {"project_id": "P", "query": "type:task"})
        ]
        passed, _ = checks.check_scoped_query_uses_sql(trajectory, {})
        assert passed is True
