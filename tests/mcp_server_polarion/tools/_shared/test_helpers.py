"""Direct tests for core helpers worth pinning beyond transitive per-tool
coverage — `format_option_list` rendering, `safe_str` coercion, the slash-encoding
contract of `encode_path_segment`, the Lucene-id guard, and `get_client` lookup.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from mcp_server_polarion.tools._shared.helpers import (
    OPTION_LIST_LIMIT,
    encode_path_segment,
    format_option_list,
    get_client,
    safe_str,
    validate_work_item_id_for_lucene,
)


class TestFormatOptionList:
    """Tests for `format_option_list`."""

    def test_empty_matches_repr_sorted(self) -> None:
        assert format_option_list([]) == repr([])

    def test_single_matches_repr_sorted(self) -> None:
        assert format_option_list(["a"]) == repr(["a"])

    def test_under_limit_identical_to_repr_sorted(self) -> None:
        # Byte-identical to the old `sorted(option_ids)` interpolation.
        ids = ["3", "1", "2", "4"]
        assert format_option_list(ids) == repr(["1", "2", "3", "4"])

    def test_exactly_at_limit_has_no_suffix(self) -> None:
        ids = [f"{i:03d}" for i in range(OPTION_LIST_LIMIT)]
        result = format_option_list(ids)
        assert result == repr(sorted(ids))
        assert "more)" not in result

    def test_over_limit_shows_cap_plus_count(self) -> None:
        ids = [f"{i:03d}" for i in range(OPTION_LIST_LIMIT + 10)]
        result = format_option_list(ids)
        # First `limit` sorted ids present, 51st absent, count of the rest shown.
        assert "'000'" in result
        assert f"'{OPTION_LIST_LIMIT - 1:03d}'" in result
        assert f"'{OPTION_LIST_LIMIT:03d}'" not in result
        assert "(+10 more)" in result

    def test_accepts_frozenset(self) -> None:
        assert format_option_list(frozenset({"b", "a"})) == repr(["a", "b"])

    def test_custom_limit(self) -> None:
        result = format_option_list(["a", "b", "c"], limit=2)
        assert "'a'" in result
        assert "'b'" in result
        assert "'c'" not in result
        assert "(+1 more)" in result


class TestSafeStr:
    """Tests for `safe_str`."""

    def test_none_becomes_empty_string(self) -> None:
        # None → "" so absent attributes don't surface as the literal "None".
        assert safe_str(None) == ""

    def test_str_passes_through(self) -> None:
        assert safe_str("hi") == "hi"

    def test_empty_str_stays_empty(self) -> None:
        assert safe_str("") == ""

    def test_non_str_coerced(self) -> None:
        assert safe_str(42) == "42"
        assert safe_str(1.5) == "1.5"
        assert safe_str(False) == "False"


class TestEncodePathSegment:
    """Tests for `encode_path_segment`."""

    def test_encodes_spaces(self) -> None:
        assert encode_path_segment("My Doc") == "My%20Doc"

    def test_encodes_slash(self) -> None:
        # safe="" is the whole point: a document name with "/" must not split
        # into extra path segments.
        assert encode_path_segment("a/b") == "a%2Fb"

    def test_plain_segment_unchanged(self) -> None:
        assert encode_path_segment("MCPT-001") == "MCPT-001"

    def test_empty_segment(self) -> None:
        assert encode_path_segment("") == ""


class TestValidateWorkItemIdForLucene:
    """Tests for `validate_work_item_id_for_lucene`."""

    def test_accepts_alphanumeric_hyphen_underscore(self) -> None:
        # Returns None (no raise) for the allowed character set.
        assert validate_work_item_id_for_lucene("MCPT-001_a9") is None

    @pytest.mark.parametrize(
        "bad_id",
        [
            "MCPT 001",  # space
            "MCPT:001",  # lucene field operator
            "MCPT*",  # wildcard
            'MCPT"x',  # quote
            "MCPT/001",  # slash
            "",  # empty (pattern needs 1+)
        ],
    )
    def test_rejects_lucene_unsafe_ids(self, bad_id: str) -> None:
        with pytest.raises(ValueError, match="outside"):
            validate_work_item_id_for_lucene(bad_id)


class TestGetClient:
    """Tests for `get_client` (error paths are pragma-excluded)."""

    def test_returns_injected_client(self, mock_ctx: MagicMock) -> None:
        client = mock_ctx.lifespan_context["polarion_client"]
        assert get_client(mock_ctx) is client
