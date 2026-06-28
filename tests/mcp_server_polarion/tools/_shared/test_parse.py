"""Direct tests for JSON:API parsers worth pinning beyond the transitive
per-tool coverage — relationship/id extraction edge cases, raw-HTML passthrough,
comment text-format branches, and the phantom-editor skip.
"""

from __future__ import annotations

from mcp_server_polarion.models import Comment, WorkItemSummary
from mcp_server_polarion.tools._shared.parse import (
    _parse_comment,
    extract_relationship_id,
    extract_relationship_ids,
    extract_short_id,
    parse_comments_page,
    parse_enum_option,
    parse_hyperlinks,
    parse_included_user_name_map,
    parse_included_work_item_map,
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
        # A to-many `data` is a list, not a dict → no scalar id.
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
        # `doc` may contain `/`; only the first two splits are structural.
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
        # "" key would join with absent-author "" → phantom editor; must be dropped.
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

    def test_splits_module_and_shortens_assignees(self) -> None:
        item: dict[str, object] = {
            "id": "proj/MCPT-1",
            "attributes": {"title": "T", "type": "task", "status": "open"},
            "relationships": {
                "module": {"data": {"id": "proj/Design/Spec"}},
                "assignee": {"data": [{"id": "proj/jdoe"}]},
            },
        }
        kwargs = parse_work_item_summary_kwargs(item)
        assert kwargs["id"] == "MCPT-1"
        assert kwargs["space_id"] == "Design"
        assert kwargs["document_name"] == "Spec"
        assert kwargs["assignee_ids"] == ["jdoe"]

    def test_non_dict_attributes_and_relationships_default_blank(self) -> None:
        kwargs = parse_work_item_summary_kwargs(
            {"id": "proj/MCPT-2", "attributes": None, "relationships": None}
        )
        assert kwargs["id"] == "MCPT-2"
        assert kwargs["title"] == ""
        assert kwargs["space_id"] == ""
        assert kwargs["assignee_ids"] == []


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
            "relationships": {"author": {"data": {"id": "proj/jdoe"}}},
        }
        detail = parse_work_item_detail(item, project_id="proj")
        assert detail.description_html == "<p>raw</p>"
        assert detail.author_id == "jdoe"
        assert detail.custom_fields == {"riskLevel": "high"}

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
        comment = _parse_comment(item)
        assert comment.id == "cmt-1"
        assert comment.text == "<p>hi</p>"
        assert comment.text_format == "text/html"
        assert comment.resolved is True
        assert comment.author_id == "jdoe"
        assert comment.child_comment_ids == ["cmt-2"]

    def test_plain_format_honored(self) -> None:
        item: dict[str, object] = {
            "id": "proj/WI-1/cmt-1",
            "attributes": {
                "created": "x",
                "text": {"type": "text/plain", "value": "hi"},
            },
        }
        assert _parse_comment(item).text_format == "text/plain"

    def test_unknown_format_falls_back_to_html(self) -> None:
        item: dict[str, object] = {
            "id": "proj/WI-1/cmt-1",
            "attributes": {
                "created": "x",
                "text": {"type": "text/weird", "value": "hi"},
            },
        }
        assert _parse_comment(item).text_format == "text/html"

    def test_absent_author_is_none(self) -> None:
        item: dict[str, object] = {
            "id": "proj/WI-1/cmt-1",
            "attributes": {"created": "x"},
        }
        assert _parse_comment(item).author_id is None


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


class TestParseEnumOption:
    """Tests for `parse_enum_option` bool coercion."""

    def test_coerces_non_bool_flags_to_false(self) -> None:
        # Non-bool flag values default to False rather than raising.
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
