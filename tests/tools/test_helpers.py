"""Direct unit tests for shared tool helpers.

The bulk of `_helpers.py` is exercised transitively through `test_read.py`
and `test_write.py` against `mock_client` fixtures. This module adds
focused tests for helpers whose contract is worth pinning directly —
currently just `extract_custom_fields`, whose allowlist semantics drive
how custom Polarion fields flow back to the LLM.
"""

from __future__ import annotations

from mcp_server_polarion.tools._helpers import (
    STANDARD_DOCUMENT_ATTRS,
    STANDARD_WORKITEM_ATTRS,
    extract_custom_fields,
)


class TestExtractCustomFields:
    """Tests for `extract_custom_fields(attrs, standard)`."""

    def test_returns_only_non_standard_keys(self) -> None:
        attrs: dict[str, object] = {
            "title": "T",
            "type": "task",
            "status": "open",
            "asil": "B",
            "requirement_id": "REQ-1",
        }
        assert extract_custom_fields(attrs, STANDARD_WORKITEM_ATTRS) == {
            "asil": "B",
            "requirement_id": "REQ-1",
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
        attrs: dict[str, object] = {"title": "T", "verification_criteria": rich}
        result = extract_custom_fields(attrs, STANDARD_WORKITEM_ATTRS)
        assert result == {"verification_criteria": rich}
        # Same object identity — no defensive copy.
        assert result["verification_criteria"] is rich

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
            "version": "0.0",
            "baseline_name": "release-2026Q1",
        }
        assert extract_custom_fields(attrs, STANDARD_DOCUMENT_ATTRS) == {
            "version": "0.0",
            "baseline_name": "release-2026Q1",
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
