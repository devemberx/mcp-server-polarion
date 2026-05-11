"""Direct unit tests for shared tool helpers.

The bulk of `_helpers.py` is exercised transitively through `test_read.py`
and `test_write.py` against `mock_client` fixtures. This module adds
focused tests for helpers whose contract is worth pinning directly —
currently `extract_custom_fields` (read-side) and `merge_custom_fields`
(write-side), whose allowlist semantics drive how custom Polarion fields
flow between the LLM and the Polarion REST API in both directions.
"""

from __future__ import annotations

import pytest

from mcp_server_polarion.models import JsonValue
from mcp_server_polarion.tools._helpers import (
    STANDARD_DOCUMENT_ATTRS,
    STANDARD_WORKITEM_ATTRS,
    extract_custom_fields,
    merge_custom_fields,
)


class TestExtractCustomFields:
    """Tests for `extract_custom_fields(attrs, standard)`."""

    def test_returns_only_non_standard_keys(self) -> None:
        attrs: dict[str, object] = {
            "title": "T",
            "type": "task",
            "status": "open",
            "riskLevel": "high",
            "effortHours": 8.0,
        }
        assert extract_custom_fields(attrs, STANDARD_WORKITEM_ATTRS) == {
            "riskLevel": "high",
            "effortHours": 8.0,
        }

    def test_empty_when_attrs_are_all_standard(self) -> None:
        attrs: dict[str, object] = {
            "title": "T",
            "type": "task",
            "status": "open",
            "priority": "50.0",
        }
        assert extract_custom_fields(attrs, STANDARD_WORKITEM_ATTRS) == {}

    def test_empty_attrs_dict(self) -> None:
        assert extract_custom_fields({}, STANDARD_WORKITEM_ATTRS) == {}

    def test_preserves_rich_text_value_verbatim(self) -> None:
        # Rich-text Polarion fields arrive as {type, value} dicts; the
        # helper must NOT convert them to Markdown, so they round-trip
        # back to Polarion unchanged on a future PATCH.
        rich = {"type": "text/html", "value": "<p>x</p>"}
        attrs: dict[str, object] = {"title": "T", "reviewerNote": rich}
        result = extract_custom_fields(attrs, STANDARD_WORKITEM_ATTRS)
        assert result == {"reviewerNote": rich}
        # Same object identity — no defensive copy.
        assert result["reviewerNote"] is rich

    def test_preserves_heterogeneous_value_types(self) -> None:
        attrs: dict[str, object] = {
            "title": "T",
            "custom_str": "s",
            "custom_int": 42,
            "custom_float": 1.5,
            "custom_bool": True,
            "custom_list": [1, 2, 3],
            "custom_dict": {"nested": "value"},
        }
        assert extract_custom_fields(attrs, STANDARD_WORKITEM_ATTRS) == {
            "custom_str": "s",
            "custom_int": 42,
            "custom_float": 1.5,
            "custom_bool": True,
            "custom_list": [1, 2, 3],
            "custom_dict": {"nested": "value"},
        }

    def test_document_allowlist_filters_document_attrs(self) -> None:
        # Verifies the same helper works for documents with the
        # document-specific allowlist; document-only standard keys
        # (e.g. homePageContent, moduleFolder) must be filtered out.
        attrs: dict[str, object] = {
            "title": "Doc",
            "type": "req_specification",
            "status": "draft",
            "homePageContent": {"type": "text/html", "value": "<p/>"},
            "moduleFolder": "Design",
            # Customs on this project's documents:
            "documentVersion": "1.0",
            "complianceLevel": "L3",
        }
        assert extract_custom_fields(attrs, STANDARD_DOCUMENT_ATTRS) == {
            "documentVersion": "1.0",
            "complianceLevel": "L3",
        }

    def test_allowlist_swap_changes_classification(self) -> None:
        # A key that is standard for one resource type may be custom on
        # another. ``autoSuspect`` is a standard document attribute but
        # NOT a standard WI attribute — so swapping the allowlist flips
        # how it's classified.
        attrs: dict[str, object] = {"autoSuspect": False}
        assert extract_custom_fields(attrs, STANDARD_DOCUMENT_ATTRS) == {}
        assert extract_custom_fields(attrs, STANDARD_WORKITEM_ATTRS) == {
            "autoSuspect": False,
        }


class TestMergeCustomFields:
    """Tests for `merge_custom_fields(attributes, customs, standard)`."""

    def test_merges_into_empty_attributes(self) -> None:
        attrs: dict[str, JsonValue] = {}
        merge_custom_fields(
            attrs,
            {"riskLevel": "high", "effortHours": 8.0},
            STANDARD_WORKITEM_ATTRS,
        )
        assert attrs == {"riskLevel": "high", "effortHours": 8.0}

    def test_merges_into_pre_populated_attributes(self) -> None:
        # Existing standard fields stay; customs are appended.
        attrs: dict[str, JsonValue] = {"title": "T", "status": "open"}
        merge_custom_fields(attrs, {"riskLevel": "high"}, STANDARD_WORKITEM_ATTRS)
        assert attrs == {"title": "T", "status": "open", "riskLevel": "high"}

    def test_none_customs_is_noop(self) -> None:
        attrs: dict[str, JsonValue] = {"title": "T"}
        merge_custom_fields(attrs, None, STANDARD_WORKITEM_ATTRS)
        assert attrs == {"title": "T"}

    def test_empty_dict_customs_is_noop(self) -> None:
        attrs: dict[str, JsonValue] = {"title": "T"}
        merge_custom_fields(attrs, {}, STANDARD_WORKITEM_ATTRS)
        assert attrs == {"title": "T"}

    def test_skips_none_values_inside_dict(self) -> None:
        # ``None`` values are skipped (clearing is not supported in
        # this phase); falsy non-``None`` values pass through verbatim
        # because they may be meaningful custom-field values.
        attrs: dict[str, JsonValue] = {}
        merge_custom_fields(
            attrs,
            {
                "skip_me": None,
                "empty_string": "",
                "zero": 0,
                "false": False,
                "empty_list": [],
            },
            STANDARD_WORKITEM_ATTRS,
        )
        assert attrs == {
            "empty_string": "",
            "zero": 0,
            "false": False,
            "empty_list": [],
        }
        assert "skip_me" not in attrs

    def test_rich_text_dict_passes_through_identity(self) -> None:
        # The {type, value} dict must round-trip unchanged — same
        # object identity, no defensive copy.
        rich = {"type": "text/html", "value": "<p>x</p>"}
        attrs: dict[str, JsonValue] = {}
        merge_custom_fields(
            attrs,
            {"reviewerNote": rich},
            STANDARD_WORKITEM_ATTRS,
        )
        assert attrs["reviewerNote"] is rich

    def test_collision_with_standard_attr_raises(self) -> None:
        # Keys that overlap with the standard allowlist would silently
        # overwrite an explicit standard parameter; raise at the tool
        # boundary so the caller gets an actionable message.
        with pytest.raises(ValueError, match="custom_fields keys collide"):
            merge_custom_fields(
                {},
                {"title": "y"},
                STANDARD_WORKITEM_ATTRS,
            )

    def test_collision_message_lists_offending_keys_sorted(self) -> None:
        # Multiple collisions are reported together in deterministic
        # order to make the diagnostic predictable.
        with pytest.raises(ValueError) as exc_info:
            merge_custom_fields(
                {},
                {"title": "x", "riskLevel": "high", "status": "y"},
                STANDARD_WORKITEM_ATTRS,
            )
        assert "['status', 'title']" in str(exc_info.value)

    def test_document_allowlist_recognises_document_collisions(self) -> None:
        # ``moduleFolder`` is a standard document attribute but NOT a
        # standard WI attribute — same key, different verdicts depending
        # on the allowlist passed.
        merge_custom_fields(
            {}, {"moduleFolder": "Design"}, STANDARD_WORKITEM_ATTRS
        )  # OK for WIs
        with pytest.raises(ValueError, match="moduleFolder"):
            merge_custom_fields(
                {},
                {"moduleFolder": "Design"},
                STANDARD_DOCUMENT_ATTRS,
            )

    def test_returns_none_mutates_in_place(self) -> None:
        # Style contract: helper mutates ``attributes`` and returns
        # nothing, matching the rest of the ``_build_*_payload`` style.
        attrs: dict[str, JsonValue] = {}
        result = merge_custom_fields(
            attrs, {"riskLevel": "high"}, STANDARD_WORKITEM_ATTRS
        )
        assert result is None
        assert attrs == {"riskLevel": "high"}
