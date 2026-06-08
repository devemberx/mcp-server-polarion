"""Direct unit tests for shared tool helpers.

The bulk of `_shared/helpers.py` is exercised transitively through the per-tool
tests against `mock_client` fixtures. This module adds
focused tests for helpers whose contract is worth pinning directly —
currently `extract_custom_fields` (read-side) and `merge_custom_fields`
(write-side), whose allowlist semantics drive how custom Polarion fields
flow between the LLM and the Polarion REST API in both directions.
"""

from __future__ import annotations

import pytest

from mcp_server_polarion.models import JsonValue
from mcp_server_polarion.tools._shared.helpers import (
    STANDARD_DOCUMENT_ATTRIBUTES,
    STANDARD_WORK_ITEM_ATTRIBUTES,
    extract_custom_fields,
    merge_custom_fields,
)


class TestExtractCustomFields:
    """Tests for `extract_custom_fields(attributes, standard)`."""

    def test_returns_only_non_standard_keys(self) -> None:
        attributes: dict[str, object] = {
            "title": "T",
            "type": "task",
            "status": "open",
            "riskLevel": "high",
            "effortHours": 8.0,
        }
        assert extract_custom_fields(attributes, STANDARD_WORK_ITEM_ATTRIBUTES) == {
            "riskLevel": "high",
            "effortHours": 8.0,
        }

    def test_empty_when_attrs_are_all_standard(self) -> None:
        attributes: dict[str, object] = {
            "title": "T",
            "type": "task",
            "status": "open",
            "priority": "50.0",
        }
        assert extract_custom_fields(attributes, STANDARD_WORK_ITEM_ATTRIBUTES) == {}

    def test_empty_attrs_dict(self) -> None:
        assert extract_custom_fields({}, STANDARD_WORK_ITEM_ATTRIBUTES) == {}

    def test_preserves_rich_text_value_verbatim(self) -> None:
        # Rich-text {type, value} dicts stay verbatim to round-trip on PATCH.
        rich = {"type": "text/html", "value": "<p>x</p>"}
        attributes: dict[str, object] = {"title": "T", "reviewerNote": rich}
        result = extract_custom_fields(attributes, STANDARD_WORK_ITEM_ATTRIBUTES)
        assert result == {"reviewerNote": rich}
        # Same object identity, no defensive copy.
        assert result["reviewerNote"] is rich

    def test_preserves_heterogeneous_value_types(self) -> None:
        attributes: dict[str, object] = {
            "title": "T",
            "custom_str": "s",
            "custom_int": 42,
            "custom_float": 1.5,
            "custom_bool": True,
            "custom_list": [1, 2, 3],
            "custom_dict": {"nested": "value"},
        }
        assert extract_custom_fields(attributes, STANDARD_WORK_ITEM_ATTRIBUTES) == {
            "custom_str": "s",
            "custom_int": 42,
            "custom_float": 1.5,
            "custom_bool": True,
            "custom_list": [1, 2, 3],
            "custom_dict": {"nested": "value"},
        }

    def test_document_allowlist_filters_document_attrs(self) -> None:
        # Document allowlist filters document-only keys (homePageContent, moduleFolder).
        attributes: dict[str, object] = {
            "title": "Doc",
            "type": "req_specification",
            "status": "draft",
            "homePageContent": {"type": "text/html", "value": "<p/>"},
            "moduleFolder": "Design",
            # Customs on this project's documents:
            "documentVersion": "1.0",
            "complianceLevel": "L3",
        }
        assert extract_custom_fields(attributes, STANDARD_DOCUMENT_ATTRIBUTES) == {
            "documentVersion": "1.0",
            "complianceLevel": "L3",
        }

    def test_allowlist_swap_changes_classification(self) -> None:
        # autoSuspect is standard for documents but custom for work items;
        # swapping the allowlist flips its classification.
        attributes: dict[str, object] = {"autoSuspect": False}
        assert extract_custom_fields(attributes, STANDARD_DOCUMENT_ATTRIBUTES) == {}
        assert extract_custom_fields(attributes, STANDARD_WORK_ITEM_ATTRIBUTES) == {
            "autoSuspect": False,
        }


class TestMergeCustomFields:
    """Tests for `merge_custom_fields(attributes, customs, standard)`."""

    def test_merges_into_empty_attributes(self) -> None:
        attributes: dict[str, JsonValue] = {}
        merge_custom_fields(
            attributes,
            {"riskLevel": "high", "effortHours": 8.0},
            STANDARD_WORK_ITEM_ATTRIBUTES,
        )
        assert attributes == {"riskLevel": "high", "effortHours": 8.0}

    def test_merges_into_pre_populated_attributes(self) -> None:
        # Existing standard fields stay; customs are appended.
        attributes: dict[str, JsonValue] = {"title": "T", "status": "open"}
        merge_custom_fields(
            attributes, {"riskLevel": "high"}, STANDARD_WORK_ITEM_ATTRIBUTES
        )
        assert attributes == {"title": "T", "status": "open", "riskLevel": "high"}

    def test_none_customs_is_noop(self) -> None:
        attributes: dict[str, JsonValue] = {"title": "T"}
        merge_custom_fields(attributes, None, STANDARD_WORK_ITEM_ATTRIBUTES)
        assert attributes == {"title": "T"}

    def test_empty_dict_customs_is_noop(self) -> None:
        attributes: dict[str, JsonValue] = {"title": "T"}
        merge_custom_fields(attributes, {}, STANDARD_WORK_ITEM_ATTRIBUTES)
        assert attributes == {"title": "T"}

    def test_skips_none_values_inside_dict(self) -> None:
        # None values skip (no clearing); other falsy values pass through.
        attributes: dict[str, JsonValue] = {}
        merge_custom_fields(
            attributes,
            {
                "skip_me": None,
                "empty_string": "",
                "zero": 0,
                "false": False,
                "empty_list": [],
            },
            STANDARD_WORK_ITEM_ATTRIBUTES,
        )
        assert attributes == {
            "empty_string": "",
            "zero": 0,
            "false": False,
            "empty_list": [],
        }
        assert "skip_me" not in attributes

    def test_rich_text_dict_passes_through_identity(self) -> None:
        # {type, value} dict round-trips by identity, no defensive copy.
        rich = {"type": "text/html", "value": "<p>x</p>"}
        attributes: dict[str, JsonValue] = {}
        merge_custom_fields(
            attributes,
            {"reviewerNote": rich},
            STANDARD_WORK_ITEM_ATTRIBUTES,
        )
        assert attributes["reviewerNote"] is rich

    def test_collision_with_standard_attr_raises(self) -> None:
        # A custom key overlapping the allowlist would shadow a standard param.
        with pytest.raises(ValueError, match="custom_fields keys collide"):
            merge_custom_fields(
                {},
                {"title": "y"},
                STANDARD_WORK_ITEM_ATTRIBUTES,
            )

    def test_collision_message_lists_offending_keys_sorted(self) -> None:
        # Collisions reported sorted for a predictable message.
        with pytest.raises(ValueError) as exc_info:
            merge_custom_fields(
                {},
                {"title": "x", "riskLevel": "high", "status": "y"},
                STANDARD_WORK_ITEM_ATTRIBUTES,
            )
        assert "['status', 'title']" in str(exc_info.value)

    def test_document_allowlist_recognises_document_collisions(self) -> None:
        # moduleFolder is standard for documents but custom for work items.
        merge_custom_fields(
            {}, {"moduleFolder": "Design"}, STANDARD_WORK_ITEM_ATTRIBUTES
        )  # OK for work items
        with pytest.raises(ValueError, match="moduleFolder"):
            merge_custom_fields(
                {},
                {"moduleFolder": "Design"},
                STANDARD_DOCUMENT_ATTRIBUTES,
            )

    def test_returns_none_mutates_in_place(self) -> None:
        # Mutates attributes in place and returns None, like _build_*_payload.
        attributes: dict[str, JsonValue] = {}
        result = merge_custom_fields(
            attributes, {"riskLevel": "high"}, STANDARD_WORK_ITEM_ATTRIBUTES
        )
        assert result is None
        assert attributes == {"riskLevel": "high"}
