"""Direct tests for pagination math — `build_page` total/has_more derivation
and `compute_has_more` branch coverage. Behavior shared by every list tool.
"""

from __future__ import annotations

from mcp_server_polarion.models import PaginatedResult, ProjectSummary
from mcp_server_polarion.tools._shared.pagination import build_page, compute_has_more


class TestBuildPage:
    """Tests for `build_page(items, response, page_number, page_size)`."""

    def test_meta_total_count_honored(self) -> None:
        response: dict[str, object] = {"meta": {"totalCount": 50}}
        page = build_page(["a", "b"], response, page_number=1, page_size=10)
        assert page.total_count == 50
        assert page.page == 1
        assert page.page_size == 10
        assert page.has_more is True  # 50 > 1 * 10

    def test_meta_total_count_exhausted_page(self) -> None:
        response: dict[str, object] = {"meta": {"totalCount": 2}}
        page = build_page(["a", "b"], response, page_number=1, page_size=10)
        assert page.total_count == 2
        assert page.has_more is False  # 2 <= 1 * 10

    def test_missing_total_offset_fallback_on_nonempty_page(self) -> None:
        # No meta.totalCount → estimate from offset + len on a non-empty page.
        page = build_page(["x", "y", "z"], {}, page_number=2, page_size=10)
        assert page.total_count == 13  # (2 - 1) * 10 + 3
        # raw_total stays 0 → no links.next, 3 != page_size → no more.
        assert page.has_more is False

    def test_empty_page_yields_zero_total_no_fallback(self) -> None:
        page = build_page([], {}, page_number=1, page_size=10)
        assert page.total_count == 0
        assert page.has_more is False

    def test_has_more_via_links_next(self) -> None:
        response: dict[str, object] = {"links": {"next": "/x?page=2"}}
        page = build_page(["a"] * 10, response, page_number=1, page_size=10)
        assert page.has_more is True

    def test_has_more_via_full_page_heuristic(self) -> None:
        # No meta, no links, but a full page implies another may follow.
        page = build_page(["a"] * 10, {}, page_number=1, page_size=10)
        assert page.has_more is True

    def test_generic_round_trips_model(self) -> None:
        # Exercises the PaginatedResult[T] subscript at construction.
        items = [ProjectSummary(id="p1", name="Proj 1", active=True)]
        page = build_page(items, {"meta": {"totalCount": 1}}, 1, 10)
        assert isinstance(page, PaginatedResult)
        assert page.items[0].id == "p1"
        assert page.items[0].name == "Proj 1"
        assert page.total_count == 1
        assert page.has_more is False


class TestComputeHasMore:
    """Tests for `compute_has_more(response, total, page, size, items_count)`."""

    def test_reliable_total_more_pages(self) -> None:
        assert compute_has_more({}, 50, 1, 10, 10) is True

    def test_reliable_total_last_page(self) -> None:
        assert compute_has_more({}, 50, 5, 10, 10) is False

    def test_zero_total_falls_to_links_next(self) -> None:
        assert compute_has_more({"links": {"next": "/x"}}, 0, 1, 10, 3) is True

    def test_zero_total_full_page_heuristic(self) -> None:
        assert compute_has_more({}, 0, 1, 10, 10) is True

    def test_zero_total_partial_page_no_more(self) -> None:
        assert compute_has_more({}, 0, 1, 10, 3) is False
