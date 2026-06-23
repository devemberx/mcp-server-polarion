"""Direct tests for core helpers worth pinning beyond transitive per-tool
coverage — `format_option_list` rendering semantics.
"""

from __future__ import annotations

from mcp_server_polarion.tools._shared.helpers import (
    OPTION_LIST_LIMIT,
    format_option_list,
)


class TestFormatOptionList:
    """Tests for `format_option_list(options, limit)`."""

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
