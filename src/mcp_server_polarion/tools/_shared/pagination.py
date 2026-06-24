"""Pagination math for list tools: total-count extraction, has-more decision,
and the shared ``make_page`` wrapper every list response ends in.
"""

from __future__ import annotations

from typing import Final

from mcp_server_polarion.models import PaginatedResult

# Polarion enforces a hard cap of 100 server-side.
DEFAULT_PAGE_SIZE: Final[int] = 100


def extract_total_count(response: dict[str, object]) -> int:
    """Return ``meta.totalCount`` from a JSON:API response, or 0 if missing."""
    meta = response.get("meta")
    if isinstance(meta, dict):
        total = meta.get("totalCount", 0)
        if isinstance(total, int):
            return total
    return 0


def has_links_next(response: dict[str, object]) -> bool:
    """Return whether the JSON:API response carries a ``links.next`` key."""
    links = response.get("links")
    if isinstance(links, dict):
        return "next" in links
    return False


def compute_has_more(
    response: dict[str, object],
    total: int,
    page_number: int,
    page_size: int,
    items_count: int,
) -> bool:
    """Whether more pages exist: ``total`` when reliable (>0), else
    ``links.next`` (Polarion sometimes omits ``meta.totalCount``), else
    full-page heuristic.
    """
    if total > 0:
        return total > page_number * page_size
    if has_links_next(response):
        return True
    return items_count == page_size


def make_page[T](
    items: list[T],
    response: dict[str, object],
    page_number: int,
    page_size: int,
) -> PaginatedResult[T]:
    """Wrap parsed items in a ``PaginatedResult``. ``total`` falls back to an
    offset estimate when Polarion omits ``meta.totalCount`` (non-empty page
    only, else out-of-range pages inflate it); ``has_more`` via ``compute_has_more``.
    """
    raw_total = extract_total_count(response)
    total = raw_total
    if total <= 0 and items:
        total = (page_number - 1) * page_size + len(items)
    return PaginatedResult[T](
        items=items,
        total_count=total,
        page=page_number,
        page_size=page_size,
        has_more=compute_has_more(
            response, raw_total, page_number, page_size, len(items)
        ),
    )


__all__: list[str] = [
    "DEFAULT_PAGE_SIZE",
    "compute_has_more",
    "extract_total_count",
    "has_links_next",
    "make_page",
]
