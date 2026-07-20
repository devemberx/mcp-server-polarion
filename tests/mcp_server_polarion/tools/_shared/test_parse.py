"""JSON:API parsers worth pinning beyond transitive per-tool coverage —
relationship/id extraction edge cases, raw-HTML passthrough, comment
text-format branches, phantom-editor skip.
"""

from __future__ import annotations

import pytest

from mcp_server_polarion.models import Attachment, Comment, WorkItemSummary
from mcp_server_polarion.tools._shared.parse import (
    _parse_attachment,
    _parse_comment,
    extract_created_full_ids,
    extract_created_short_ids,
    extract_relationship_id,
    extract_relationship_ids,
    extract_short_id,
    parse_attachments_page,
    parse_comments_page,
    parse_enum_option,
    parse_hyperlinks,
    parse_included_user_name_map,
    parse_included_work_item_map,
    parse_test_record_detail,
    parse_test_record_summaries,
    parse_test_record_summary_kwargs,
    parse_test_run_detail,
    parse_test_run_summaries,
    parse_test_run_summary_kwargs,
    parse_work_item_detail,
    parse_work_item_summaries,
    parse_work_item_summary_kwargs,
    split_module_id,
    summary_to_back_link,
)


class TestExtractRelationshipId:
    """Tests for `extract_relationship_id`."""

    def test_returns_data_id(self) -> None:
        rels = {"author": {"data": {"id": "proj/jdoe", "type": "users"}}}
        assert extract_relationship_id(rels, "author") == "proj/jdoe"

    def test_absent_relationship_is_empty(self) -> None:
        assert extract_relationship_id({}, "author") == ""

    def test_to_many_data_list_is_empty(self) -> None:
        # To-many `data` is a list, not dict → no scalar id.
        rels = {"assignee": {"data": [{"id": "proj/u1"}]}}
        assert extract_relationship_id(rels, "assignee") == ""

    def test_non_dict_relationship_is_empty(self) -> None:
        assert extract_relationship_id({"author": "nope"}, "author") == ""


class TestExtractRelationshipIds:
    """Tests for `extract_relationship_ids`."""

    def test_preserves_declaration_order(self) -> None:
        rels = {"assignee": {"data": [{"id": "p/u2"}, {"id": "p/u1"}]}}
        assert extract_relationship_ids(rels, "assignee") == ["p/u2", "p/u1"]

    def test_missing_data_is_empty(self) -> None:
        assert extract_relationship_ids({"assignee": {}}, "assignee") == []

    def test_non_list_data_is_empty(self) -> None:
        assert extract_relationship_ids({"assignee": {"data": {}}}, "assignee") == []

    def test_skips_non_dict_and_empty_id_entries(self) -> None:
        rels = {"assignee": {"data": [{"id": "p/u1"}, "x", {"id": ""}, {}]}}
        assert extract_relationship_ids(rels, "assignee") == ["p/u1"]


class TestSplitModuleId:
    """Tests for `split_module_id`."""

    def test_three_segments(self) -> None:
        assert split_module_id("proj/Design/Spec") == ("Design", "Spec")

    def test_document_name_keeps_extra_slashes(self) -> None:
        # `doc` may contain `/`; only first two splits structural.
        assert split_module_id("proj/Design/Spec/v2") == ("Design", "Spec/v2")

    def test_under_three_segments_is_empty(self) -> None:
        assert split_module_id("proj/Design") == ("", "")

    def test_empty_is_empty(self) -> None:
        assert split_module_id("") == ("", "")


class TestExtractShortId:
    """Tests for `extract_short_id`."""

    def test_strips_path_prefix(self) -> None:
        assert extract_short_id("proj/MCPT-001") == "MCPT-001"

    def test_takes_last_segment_only(self) -> None:
        assert extract_short_id("a/b/c/MCPT-9") == "MCPT-9"

    def test_no_slash_returns_input(self) -> None:
        assert extract_short_id("MCPT-001") == "MCPT-001"


class TestExtractCreatedShortIds:
    """Tests for `extract_created_short_ids` (bulk-create 201 echo)."""

    def test_extracts_short_ids_in_order(self) -> None:
        response: dict[str, object] = {
            "data": [
                {"type": "workitems", "id": "MyProj/MCPT-042"},
                {"type": "testruns", "id": "MyProj/TR-043"},
            ]
        }
        assert extract_created_short_ids(
            response, expected_count=2, list_tool="list_work_items"
        ) == ["MCPT-042", "TR-043"]

    def test_short_echo_raises_with_counts_and_list_tool(self) -> None:
        response: dict[str, object] = {
            "data": [{"type": "workitems", "id": "MyProj/MCPT-1"}]
        }
        with pytest.raises(
            RuntimeError, match=r"1 ids for 2 submitted.*list_work_items"
        ):
            extract_created_short_ids(
                response, expected_count=2, list_tool="list_work_items"
            )

    def test_data_missing_raises_zero_count(self) -> None:
        with pytest.raises(RuntimeError, match="0 ids for 1 submitted"):
            extract_created_short_ids({}, expected_count=1, list_tool="list_x")

    def test_data_not_a_list_raises_zero_count(self) -> None:
        with pytest.raises(RuntimeError, match="0 ids for 1 submitted"):
            extract_created_short_ids(
                {"data": {"id": "MyProj/MCPT-1"}},
                expected_count=1,
                list_tool="list_x",
            )

    def test_skips_entries_missing_id_or_not_dict(self) -> None:
        response: dict[str, object] = {
            "data": [
                {"type": "workitems", "id": "MyProj/MCPT-1"},
                {"type": "workitems"},
                "not a dict",
            ]
        }
        assert extract_created_short_ids(
            response, expected_count=1, list_tool="list_x"
        ) == ["MCPT-1"]

    def test_skipped_entries_shrink_echo_below_expected(self) -> None:
        # Malformed entries drop from echo -- guard count them missing.
        response: dict[str, object] = {
            "data": [
                {"type": "workitems", "id": "MyProj/MCPT-1"},
                {"type": "workitems"},
            ]
        }
        with pytest.raises(RuntimeError, match="1 ids for 2 submitted"):
            extract_created_short_ids(response, expected_count=2, list_tool="list_x")


class TestExtractCreatedFullIds:
    """Tests for `extract_created_full_ids` (composite 5-segment ids)."""

    def test_extracts_full_ids_verbatim_in_order(self) -> None:
        # 5-segment testrecord id: project/testRun/testCaseProject/testCaseId/
        # iteration -- extract_created_short_ids would rsplit to bare "0".
        response: dict[str, object] = {
            "data": [
                {
                    "type": "testrecords",
                    "id": "MCP_Test_Project/run1/MCP_Test_Project/MCPT-568/0",
                },
                {
                    "type": "testrecords",
                    "id": "MCP_Test_Project/run1/MCP_Test_Project/MCPT-569/1",
                },
            ]
        }
        assert extract_created_full_ids(
            response, expected_count=2, list_tool="list_test_records"
        ) == [
            "MCP_Test_Project/run1/MCP_Test_Project/MCPT-568/0",
            "MCP_Test_Project/run1/MCP_Test_Project/MCPT-569/1",
        ]

    def test_short_echo_raises_with_counts_and_list_tool(self) -> None:
        response: dict[str, object] = {
            "data": [{"type": "testrecords", "id": "p/r/p/WI-1/0"}]
        }
        with pytest.raises(
            RuntimeError, match=r"1 ids for 2 submitted.*list_test_records"
        ):
            extract_created_full_ids(
                response, expected_count=2, list_tool="list_test_records"
            )

    def test_data_missing_raises_zero_count(self) -> None:
        with pytest.raises(RuntimeError, match="0 ids for 1 submitted"):
            extract_created_full_ids({}, expected_count=1, list_tool="list_x")

    def test_data_not_a_list_raises_zero_count(self) -> None:
        with pytest.raises(RuntimeError, match="0 ids for 1 submitted"):
            extract_created_full_ids(
                {"data": {"id": "p/r/p/WI-1/0"}},
                expected_count=1,
                list_tool="list_x",
            )

    def test_skips_entries_missing_id_or_not_dict(self) -> None:
        response: dict[str, object] = {
            "data": [
                {"type": "testrecords", "id": "p/r/p/WI-1/0"},
                {"type": "testrecords"},
                "not a dict",
            ]
        }
        assert extract_created_full_ids(
            response, expected_count=1, list_tool="list_x"
        ) == ["p/r/p/WI-1/0"]


class TestParseIncludedWorkItemMap:
    """Tests for `parse_included_work_item_map`."""

    def test_maps_only_workitems_resources(self) -> None:
        response: dict[str, object] = {
            "included": [
                {"type": "workitems", "id": "proj/WI-1", "attributes": {"title": "A"}},
                {"type": "users", "id": "proj/u1"},
            ]
        }
        result = parse_included_work_item_map(response)
        assert set(result) == {"proj/WI-1"}
        assert result["proj/WI-1"]["attributes"] == {"title": "A"}

    def test_missing_included_is_empty(self) -> None:
        assert parse_included_work_item_map({}) == {}


class TestParseIncludedUserNameMap:
    """Tests for `parse_included_user_name_map`."""

    def test_maps_user_id_to_name(self) -> None:
        response: dict[str, object] = {
            "included": [
                {"type": "users", "id": "proj/jdoe", "attributes": {"name": "J Doe"}},
            ]
        }
        assert parse_included_user_name_map(response) == {"proj/jdoe": "J Doe"}

    def test_skips_empty_user_id(self) -> None:
        # "" key would join absent-author "" → phantom editor; must drop.
        response: dict[str, object] = {
            "included": [{"type": "users", "id": "", "attributes": {"name": "Ghost"}}]
        }
        assert parse_included_user_name_map(response) == {}

    def test_non_dict_attributes_yield_empty_name(self) -> None:
        response: dict[str, object] = {
            "included": [{"type": "users", "id": "proj/u1", "attributes": None}]
        }
        assert parse_included_user_name_map(response) == {"proj/u1": ""}


class TestParseWorkItemSummaryKwargs:
    """Tests for `parse_work_item_summary_kwargs`."""

    def test_splits_module_and_resolves_author(self) -> None:
        item: dict[str, object] = {
            "id": "proj/MCPT-1",
            "attributes": {"title": "T", "type": "task", "status": "open"},
            "relationships": {
                "module": {"data": {"id": "proj/Design/Spec"}},
                "author": {"data": {"id": "proj/jdoe"}},
            },
        }
        kwargs = parse_work_item_summary_kwargs(item, {"proj/jdoe": "J Doe"})
        assert kwargs["id"] == "MCPT-1"
        assert kwargs["space_id"] == "Design"
        assert kwargs["document_name"] == "Spec"
        assert kwargs["author_name"] == "J Doe"

    def test_author_present_but_unresolved_yields_blank_name(self) -> None:
        # Author relationship present but id absent from user_names map -> "".
        item: dict[str, object] = {
            "id": "proj/MCPT-3",
            "attributes": {"title": "T", "type": "task", "status": "open"},
            "relationships": {"author": {"data": {"id": "proj/ghost"}}},
        }
        kwargs = parse_work_item_summary_kwargs(item, {"proj/jdoe": "J Doe"})
        assert kwargs["author_name"] == ""

    def test_non_dict_attributes_and_relationships_default_blank(self) -> None:
        kwargs = parse_work_item_summary_kwargs(
            {"id": "proj/MCPT-2", "attributes": None, "relationships": None}
        )
        assert kwargs["id"] == "MCPT-2"
        assert kwargs["title"] == ""
        assert kwargs["space_id"] == ""
        assert kwargs["author_name"] == ""


class TestParseHyperlinks:
    """Tests for `parse_hyperlinks`."""

    def test_parses_entries(self) -> None:
        value = [{"role": "ref", "title": "Spec", "uri": "https://x"}]
        links = parse_hyperlinks(value)
        assert len(links) == 1
        assert links[0].role == "ref"
        assert links[0].uri == "https://x"

    def test_skips_uri_less_entries(self) -> None:
        value = [{"role": "ref"}, {"role": "ref", "uri": "https://x"}]
        assert [link.uri for link in parse_hyperlinks(value)] == ["https://x"]

    def test_non_list_is_empty(self) -> None:
        assert parse_hyperlinks(None) == []

    def test_skips_non_dict_entries(self) -> None:
        assert parse_hyperlinks(["nope", {"uri": "https://x"}])[0].uri == "https://x"


class TestParseWorkItemDetail:
    """Tests for `parse_work_item_detail`."""

    def test_passes_description_html_verbatim(self) -> None:
        item: dict[str, object] = {
            "id": "proj/MCPT-1",
            "attributes": {
                "title": "T",
                "type": "task",
                "status": "open",
                "description": {"type": "text/html", "value": "<p>raw</p>"},
                "riskLevel": "high",
            },
            "relationships": {
                "author": {"data": {"id": "proj/jdoe"}},
                "assignee": {"data": [{"id": "proj/alice"}, {"id": "proj/bob"}]},
            },
        }
        detail = parse_work_item_detail(item, project_id="proj")
        assert detail.description_html == "<p>raw</p>"
        assert detail.author_id == "jdoe"
        assert detail.author_name == ""
        assert detail.assignee_ids == ["alice", "bob"]
        assert detail.assignee_names == ["", ""]
        assert detail.custom_fields == {"riskLevel": "high"}

    def test_user_names_resolve_author_name(self) -> None:
        item: dict[str, object] = {
            "id": "proj/MCPT-1",
            "attributes": {"title": "T", "type": "task", "status": "open"},
            "relationships": {"author": {"data": {"id": "proj/jdoe"}}},
        }
        detail = parse_work_item_detail(
            item, project_id="proj", user_names={"proj/jdoe": "J Doe"}
        )
        assert detail.author_id == "jdoe"
        assert detail.author_name == "J Doe"

    def test_user_names_resolve_assignee_names_index_paired(self) -> None:
        # bob unresolved -> "" at same index; keeps pairing with assignee_ids.
        item: dict[str, object] = {
            "id": "proj/MCPT-1",
            "attributes": {"title": "T", "type": "task", "status": "open"},
            "relationships": {
                "assignee": {"data": [{"id": "proj/alice"}, {"id": "proj/bob"}]},
            },
        }
        detail = parse_work_item_detail(
            item, project_id="proj", user_names={"proj/alice": "Alice A"}
        )
        assert detail.assignee_ids == ["alice", "bob"]
        assert detail.assignee_names == ["Alice A", ""]

    def test_fallback_id_used_when_id_missing(self) -> None:
        item: dict[str, object] = {
            "attributes": {"title": "T", "type": "task", "status": "open"},
        }
        detail = parse_work_item_detail(item, project_id="proj", fallback_id="MCPT-9")
        assert detail.id == "MCPT-9"


class TestSummaryToBackLink:
    """Tests for `summary_to_back_link`."""

    def test_lifts_with_no_role_and_back_direction(self) -> None:
        summary = WorkItemSummary(
            id="MCPT-1",
            title="T",
            type="task",
            status="open",
            space_id="Design",
            document_name="Spec",
        )
        link = summary_to_back_link(summary)
        assert link.role is None
        assert link.direction == "back"
        assert link.suspect is False
        assert link.id == "MCPT-1"
        assert link.space_id == "Design"


class TestParseWorkItemSummaries:
    """Tests for `parse_work_item_summaries`."""

    def test_parses_each_resource(self) -> None:
        data = [
            {
                "id": "proj/MCPT-1",
                "attributes": {"title": "A", "type": "t", "status": "s"},
            },
            {
                "id": "proj/MCPT-2",
                "attributes": {"title": "B", "type": "t", "status": "s"},
            },
        ]
        assert [s.id for s in parse_work_item_summaries(data)] == ["MCPT-1", "MCPT-2"]

    def test_non_list_is_empty(self) -> None:
        assert parse_work_item_summaries(None) == []

    def test_skips_non_dict_entries(self) -> None:
        data = [
            "nope",
            {
                "id": "proj/MCPT-1",
                "attributes": {"title": "A", "type": "t", "status": "s"},
            },
        ]
        assert [s.id for s in parse_work_item_summaries(data)] == ["MCPT-1"]


class TestParseTestRunSummaries:
    """Tests for `parse_test_run_summaries` and its kwargs helper."""

    def test_non_dict_attributes_and_relationships_default_empty(self) -> None:
        kwargs = parse_test_run_summary_kwargs(
            {"id": "proj/TR-1", "attributes": [], "relationships": "nope"},
            user_names={},
        )
        assert kwargs["id"] == "TR-1"
        assert kwargs["title"] == ""
        assert kwargs["author_name"] == ""
        assert kwargs["group_id"] == ""
        assert kwargs["template_id"] == ""

    def test_author_name_resolved(self) -> None:
        kwargs = parse_test_run_summary_kwargs(
            {
                "id": "proj/TR-1",
                "attributes": {"title": "A", "type": "t", "status": "s"},
                "relationships": {"author": {"data": {"id": "proj/jdoe"}}},
            },
            user_names={"proj/jdoe": "J Doe"},
        )
        assert kwargs["author_name"] == "J Doe"

    def test_group_id_and_template_id_populate(self) -> None:
        kwargs = parse_test_run_summary_kwargs(
            {
                "id": "proj/TR-1",
                "attributes": {"groupId": "Release-2.5"},
                "relationships": {
                    "template": {"data": {"id": "proj/TR-tmpl"}},
                },
            },
            user_names={},
        )
        assert kwargs["group_id"] == "Release-2.5"
        assert kwargs["template_id"] == "TR-tmpl"

    def test_non_list_data_is_empty(self) -> None:
        assert parse_test_run_summaries({"data": None}) == []

    def test_skips_non_dict_entries(self) -> None:
        response = {
            "data": [
                "nope",
                {
                    "id": "proj/TR-2",
                    "attributes": {"title": "A", "type": "t", "status": "s"},
                },
            ]
        }
        assert [s.id for s in parse_test_run_summaries(response)] == ["TR-2"]


class TestParseTestRecordSummaries:
    """Tests for `parse_test_record_summaries` and its kwargs helper."""

    def test_full_record_resolves_relationships(self) -> None:
        kwargs = parse_test_record_summary_kwargs(
            {
                "id": "proj/TR-1/proj/TC-42/0",
                "attributes": {
                    "executed": "2026-06-01T10:00:00Z",
                    "duration": 12.5,
                    "result": "failed",
                    "iteration": 3,
                },
                "relationships": {
                    "testCase": {"data": {"id": "proj/TC-42"}},
                    "executedBy": {"data": {"id": "proj/jdoe"}},
                    "defect": {"data": {"id": "proj/DEF-7"}},
                },
            },
            user_names={"proj/jdoe": "J Doe"},
        )
        # Full ids kept -- no extract_short_id on work-item targets.
        # id verbatim -- update_test_records copy it whole (as record_id).
        assert kwargs["id"] == "proj/TR-1/proj/TC-42/0"
        assert kwargs["test_case_id"] == "proj/TC-42"
        assert kwargs["defect_id"] == "proj/DEF-7"
        assert kwargs["executed_by_name"] == "J Doe"
        assert kwargs["iteration"] == 3
        assert kwargs["duration"] == 12.5
        assert kwargs["result"] == "failed"
        assert kwargs["executed"] == "2026-06-01T10:00:00Z"

    def test_non_dict_attributes_and_relationships_default_empty(self) -> None:
        kwargs = parse_test_record_summary_kwargs(
            {"id": "proj/TR-1/proj/TC-1/0", "attributes": [], "relationships": "nope"},
            user_names={},
        )
        assert kwargs["test_case_id"] == ""
        assert kwargs["result"] == ""
        assert kwargs["executed"] == ""
        assert kwargs["duration"] == 0.0
        assert kwargs["iteration"] == 0
        assert kwargs["executed_by_name"] == ""
        assert kwargs["defect_id"] == ""

    def test_non_numeric_duration_and_iteration_default_zero(self) -> None:
        kwargs = parse_test_record_summary_kwargs(
            {
                "id": "proj/TR-1/proj/TC-1/0",
                "attributes": {"duration": "fast", "iteration": True},
            },
            user_names={},
        )
        # bool is int subclass -- reject as iteration.
        assert kwargs["duration"] == 0.0
        assert kwargs["iteration"] == 0

    def test_int_duration_coerced_to_float(self) -> None:
        kwargs = parse_test_record_summary_kwargs(
            {"id": "proj/TR-1/proj/TC-1/0", "attributes": {"duration": 7}},
            user_names={},
        )
        assert kwargs["duration"] == 7.0

    def test_non_list_data_is_empty(self) -> None:
        assert parse_test_record_summaries({"data": None}) == []

    def test_skips_non_dict_entries(self) -> None:
        response = {
            "data": [
                "nope",
                {
                    "id": "proj/TR-1/proj/TC-2/0",
                    "relationships": {"testCase": {"data": {"id": "proj/TC-2"}}},
                },
            ]
        }
        parsed = parse_test_record_summaries(response)
        assert [r.test_case_id for r in parsed] == ["proj/TC-2"]
        assert [r.id for r in parsed] == ["proj/TR-1/proj/TC-2/0"]


class TestParseTestRunDetail:
    """Tests for `parse_test_run_detail`."""

    def test_passes_content_html_verbatim_and_extracts_customs(self) -> None:
        item: dict[str, object] = {
            "id": "proj/TR-1",
            "attributes": {
                "title": "T",
                "type": "manual",
                "status": "open",
                "created": "2026-06-01T08:00:00Z",
                "query": "type:testcase",
                "selectTestCasesBy": "staticQueryResult",
                "homePageContent": {"type": "text/html", "value": "<p>raw</p>"},
                "myCustomField": "x",
            },
            "relationships": {"author": {"data": {"id": "proj/jdoe"}}},
        }
        detail = parse_test_run_detail(item, project_id="proj")
        assert detail.content_html == "<p>raw</p>"
        assert detail.created == "2026-06-01T08:00:00Z"
        assert detail.query == "type:testcase"
        assert detail.select_test_cases_by == "staticQueryResult"
        assert detail.use_report_from_template is False
        assert detail.project_id == "proj"
        assert detail.author_id == "jdoe"
        assert detail.author_name == ""
        assert detail.custom_fields == {"myCustomField": "x"}

    def test_use_report_from_template_true_with_absent_body(self) -> None:
        # Polarion omit homePageContent when report inherit from template.
        item: dict[str, object] = {
            "id": "proj/TR-1",
            "attributes": {"title": "T", "useReportFromTemplate": True},
        }
        detail = parse_test_run_detail(item, project_id="proj")
        assert detail.use_report_from_template is True
        assert detail.content_html == ""

    def test_user_names_resolve_author_name(self) -> None:
        item: dict[str, object] = {
            "id": "proj/TR-1",
            "attributes": {"title": "T"},
            "relationships": {"author": {"data": {"id": "proj/jdoe"}}},
        }
        detail = parse_test_run_detail(
            item, project_id="proj", user_names={"proj/jdoe": "J Doe"}
        )
        assert detail.author_id == "jdoe"
        assert detail.author_name == "J Doe"

    def test_document_relationship_splits_space_and_name(self) -> None:
        # Doc names may contain '/' — split_module_id keep tail intact.
        item: dict[str, object] = {
            "id": "proj/TR-1",
            "attributes": {"title": "T"},
            "relationships": {
                "document": {"data": {"id": "proj/Testing/Auth/Plan"}},
            },
        }
        detail = parse_test_run_detail(item, project_id="proj")
        assert detail.space_id == "Testing"
        assert detail.document_name == "Auth/Plan"

    def test_no_document_relationship_defaults_empty(self) -> None:
        item: dict[str, object] = {"id": "proj/TR-1", "attributes": {"title": "T"}}
        detail = parse_test_run_detail(item, project_id="proj")
        assert detail.space_id == ""
        assert detail.document_name == ""

    def test_non_dict_home_page_content_defaults_empty(self) -> None:
        item: dict[str, object] = {
            "id": "proj/TR-1",
            "attributes": {"title": "T", "homePageContent": "nope"},
        }
        detail = parse_test_run_detail(item, project_id="proj")
        assert detail.content_html == ""

    def test_fallback_id_used_when_id_missing(self) -> None:
        item: dict[str, object] = {"attributes": {"title": "T"}}
        detail = parse_test_run_detail(item, project_id="proj", fallback_id="TR-9")
        assert detail.id == "TR-9"

    def test_non_dict_attributes_and_relationships_default_empty(self) -> None:
        item: dict[str, object] = {
            "id": "proj/TR-1",
            "attributes": [],
            "relationships": "nope",
        }
        detail = parse_test_run_detail(item, project_id="proj")
        assert detail.id == "TR-1"
        assert detail.content_html == ""
        assert detail.author_id == ""
        assert detail.custom_fields == {}


class TestParseTestRecordDetail:
    """Tests for `parse_test_record_detail`."""

    def test_full_record_resolves_all_fields(self) -> None:
        item: dict[str, object] = {
            "id": "proj/TR-1/proj/TC-42/0",
            "attributes": {
                "executed": "2026-06-01T10:00:00Z",
                "duration": 12.5,
                "result": "failed",
                "iteration": 3,
                "testCaseRevision": "42",
                "comment": {"type": "text/html", "value": "<p>note</p>"},
            },
            "relationships": {
                "testCase": {"data": {"id": "proj/TC-42"}},
                "executedBy": {"data": {"id": "proj/jdoe"}},
                "defect": {"data": {"id": "proj/DEF-7"}},
            },
        }
        detail = parse_test_record_detail(
            item,
            project_id="proj",
            test_run_id="TR-1",
            user_names={"proj/jdoe": "J Doe"},
        )
        assert detail.project_id == "proj"
        assert detail.test_run_id == "TR-1"
        assert detail.test_case_id == "proj/TC-42"
        assert detail.iteration == 3
        assert detail.result == "failed"
        assert detail.executed == "2026-06-01T10:00:00Z"
        assert detail.duration == 12.5
        assert detail.defect_id == "proj/DEF-7"
        # Short id output, parity TestRunDetail.author_id; name resolve off full id.
        assert detail.executed_by_id == "jdoe"
        assert detail.executed_by_name == "J Doe"
        assert detail.test_case_revision == "42"
        assert detail.comment_html == "<p>note</p>"

    def test_plain_text_comment_value_passed_verbatim(self) -> None:
        item: dict[str, object] = {
            "id": "proj/TR-1/proj/TC-1/0",
            "attributes": {"comment": {"type": "text/plain", "value": "plain note"}},
        }
        detail = parse_test_record_detail(
            item, project_id="proj", test_run_id="TR-1", user_names={}
        )
        assert detail.comment_html == "plain note"

    def test_comment_absent_defaults_empty(self) -> None:
        item: dict[str, object] = {
            "id": "proj/TR-1/proj/TC-1/0",
            "attributes": {},
        }
        detail = parse_test_record_detail(
            item, project_id="proj", test_run_id="TR-1", user_names={}
        )
        assert detail.comment_html == ""

    def test_comment_non_dict_defaults_empty(self) -> None:
        item: dict[str, object] = {
            "id": "proj/TR-1/proj/TC-1/0",
            "attributes": {"comment": "nope"},
        }
        detail = parse_test_record_detail(
            item, project_id="proj", test_run_id="TR-1", user_names={}
        )
        assert detail.comment_html == ""

    def test_relationships_block_omitted_defaults_empty_ids(self) -> None:
        item: dict[str, object] = {
            "id": "proj/TR-1/proj/TC-1/0",
            "attributes": {},
        }
        detail = parse_test_record_detail(
            item, project_id="proj", test_run_id="TR-1", user_names={}
        )
        assert detail.test_case_id == ""
        assert detail.executed_by_id == ""
        assert detail.defect_id == ""

    def test_executed_by_data_null_defaults_empty(self) -> None:
        item: dict[str, object] = {
            "id": "proj/TR-1/proj/TC-1/0",
            "attributes": {},
            "relationships": {"executedBy": {"data": None}},
        }
        detail = parse_test_record_detail(
            item, project_id="proj", test_run_id="TR-1", user_names={}
        )
        assert detail.executed_by_id == ""
        assert detail.executed_by_name == ""

    def test_non_dict_attributes_default_empty(self) -> None:
        item: dict[str, object] = {
            "id": "proj/TR-1/proj/TC-1/0",
            "attributes": [],
            "relationships": "nope",
        }
        detail = parse_test_record_detail(
            item, project_id="proj", test_run_id="TR-1", user_names={}
        )
        assert detail.test_case_revision == ""
        assert detail.comment_html == ""
        assert detail.result == ""
        assert detail.duration == 0.0

    def test_bool_iteration_rejected_to_zero(self) -> None:
        item: dict[str, object] = {
            "id": "proj/TR-1/proj/TC-1/0",
            "attributes": {"iteration": True},
        }
        detail = parse_test_record_detail(
            item, project_id="proj", test_run_id="TR-1", user_names={}
        )
        assert detail.iteration == 0

    def test_user_names_miss_keeps_id_blanks_name(self) -> None:
        item: dict[str, object] = {
            "id": "proj/TR-1/proj/TC-1/0",
            "attributes": {},
            "relationships": {"executedBy": {"data": {"id": "proj/jdoe"}}},
        }
        detail = parse_test_record_detail(
            item, project_id="proj", test_run_id="TR-1", user_names={}
        )
        assert detail.executed_by_id == "jdoe"
        assert detail.executed_by_name == ""


class TestParseComment:
    """Tests for `_parse_comment` text-format and id handling."""

    def test_html_format_and_short_ids(self) -> None:
        item: dict[str, object] = {
            "id": "proj/WI-1/cmt-1",
            "attributes": {
                "created": "2026-01-01",
                "resolved": True,
                "title": "Heading",
                "text": {"type": "text/html", "value": "<p>hi</p>"},
            },
            "relationships": {
                "author": {"data": {"id": "proj/jdoe"}},
                "childComments": {"data": [{"id": "proj/WI-1/cmt-2"}]},
            },
        }
        comment = _parse_comment(item, {"proj/jdoe": "J Doe"})
        assert comment.id == "cmt-1"
        assert comment.text == "<p>hi</p>"
        assert comment.text_format == "text/html"
        assert comment.resolved is True
        assert comment.author_name == "J Doe"
        assert comment.child_comment_ids == ["cmt-2"]

    def test_author_name_empty_when_unresolved(self) -> None:
        item: dict[str, object] = {
            "id": "proj/WI-1/cmt-1",
            "attributes": {"created": "x"},
            "relationships": {"author": {"data": {"id": "proj/jdoe"}}},
        }
        comment = _parse_comment(item, {})
        assert comment.author_name == ""

    def test_plain_format_honored(self) -> None:
        item: dict[str, object] = {
            "id": "proj/WI-1/cmt-1",
            "attributes": {
                "created": "x",
                "text": {"type": "text/plain", "value": "hi"},
            },
        }
        assert _parse_comment(item, {}).text_format == "text/plain"

    def test_unknown_format_falls_back_to_html(self) -> None:
        item: dict[str, object] = {
            "id": "proj/WI-1/cmt-1",
            "attributes": {
                "created": "x",
                "text": {"type": "text/weird", "value": "hi"},
            },
        }
        assert _parse_comment(item, {}).text_format == "text/html"

    def test_absent_author_name_empty(self) -> None:
        item: dict[str, object] = {
            "id": "proj/WI-1/cmt-1",
            "attributes": {"created": "x"},
        }
        assert _parse_comment(item, {}).author_name == ""


class TestParseCommentsPage:
    """Tests for `parse_comments_page`."""

    def test_wraps_parsed_comments(self) -> None:
        response: dict[str, object] = {
            "data": [{"id": "proj/WI-1/cmt-1", "attributes": {"created": "x"}}],
            "meta": {"totalCount": 1},
        }
        page = parse_comments_page(response, page_number=1, page_size=10)
        assert page.total_count == 1
        assert page.has_more is False
        assert isinstance(page.items[0], Comment)
        assert page.items[0].id == "cmt-1"

    def test_non_list_data_yields_empty_page(self) -> None:
        page = parse_comments_page({"data": None}, page_number=1, page_size=10)
        assert page.items == []
        assert page.total_count == 0


class TestParseAttachment:
    """Tests for `_parse_attachment` attribute/relationship extraction."""

    def test_full_attributes_happy_path(self) -> None:
        item: dict[str, object] = {
            "id": "proj/space/doc/1-file.png",
            "attributes": {
                "id": "1-file.png",
                "fileName": "1-file.png",
                "title": "file",
                "updated": "2026-05-12T12:27:38.294Z",
                "length": 17834,
            },
            "relationships": {"author": {"data": {"id": "proj/jdoe"}}},
        }
        attachment = _parse_attachment(item, {"proj/jdoe": "J Doe"})
        assert attachment.id == "1-file.png"
        assert attachment.file_name == "1-file.png"
        assert attachment.title == "file"
        assert attachment.updated == "2026-05-12T12:27:38.294Z"
        assert attachment.length == 17834
        assert attachment.author_name == "J Doe"

    def test_missing_attributes_defaults(self) -> None:
        item: dict[str, object] = {"id": "proj/space/doc/1-file.png"}
        attachment = _parse_attachment(item, {})
        assert attachment.id == ""
        assert attachment.file_name == ""
        assert attachment.title == ""
        assert attachment.length == 0
        assert attachment.updated == ""
        assert attachment.author_name == ""

    def test_missing_relationships_author_name_empty(self) -> None:
        item: dict[str, object] = {
            "id": "proj/space/doc/1-file.png",
            "attributes": {"id": "1-file.png"},
        }
        assert _parse_attachment(item, {"proj/jdoe": "J Doe"}).author_name == ""

    def test_unknown_author_id_not_in_included_map(self) -> None:
        item: dict[str, object] = {
            "id": "proj/space/doc/1-file.png",
            "attributes": {"id": "1-file.png"},
            "relationships": {"author": {"data": {"id": "proj/other"}}},
        }
        assert _parse_attachment(item, {"proj/jdoe": "J Doe"}).author_name == ""

    def test_non_int_length_falls_back_to_zero(self) -> None:
        item: dict[str, object] = {
            "id": "proj/space/doc/1-file.png",
            "attributes": {"id": "1-file.png", "length": "not-a-number"},
        }
        assert _parse_attachment(item, {}).length == 0


class TestParseAttachmentsPage:
    """Tests for `parse_attachments_page`."""

    def test_wraps_parsed_attachments(self) -> None:
        response: dict[str, object] = {
            "data": [
                {
                    "id": "proj/space/doc/1-file.png",
                    "attributes": {"id": "1-file.png", "length": 17834},
                    "relationships": {"author": {"data": {"id": "proj/jdoe"}}},
                }
            ],
            "included": [
                {
                    "type": "users",
                    "id": "proj/jdoe",
                    "attributes": {"name": "J Doe"},
                }
            ],
            "meta": {"totalCount": 1},
        }
        page = parse_attachments_page(response, page_number=1, page_size=10)
        assert page.total_count == 1
        assert page.has_more is False
        assert isinstance(page.items[0], Attachment)
        assert page.items[0].id == "1-file.png"
        assert page.items[0].author_name == "J Doe"

    def test_empty_data_yields_empty_page(self) -> None:
        page = parse_attachments_page({"data": []}, page_number=1, page_size=10)
        assert page.items == []
        assert page.total_count == 0

    def test_non_list_data_yields_empty_page(self) -> None:
        page = parse_attachments_page({"data": None}, page_number=1, page_size=10)
        assert page.items == []
        assert page.total_count == 0


class TestParseEnumOption:
    """Tests for `parse_enum_option` bool coercion."""

    def test_coerces_non_bool_flags_to_false(self) -> None:
        # Non-bool flag values default to False rather than raise.
        option = parse_enum_option(
            {"id": "open", "name": "Open", "default": "yes", "hidden": 1}
        )
        assert option.id == "open"
        assert option.name == "Open"
        assert option.default is False
        assert option.hidden is False

    def test_honors_bool_flags(self) -> None:
        option = parse_enum_option({"id": "done", "name": "Done", "terminal": True})
        assert option.terminal is True
