"""Shared helpers for MCP tool implementations.

Internal module used by ``tools.read`` (and future ``tools.write``).
The helpers defined here are for internal use within the ``tools``
package and are **not** part of the public package API.
"""

from __future__ import annotations

from typing import Final, TypedDict
from urllib.parse import quote

from fastmcp import Context

from mcp_server_polarion.core.client import PolarionClient
from mcp_server_polarion.models import WorkItemSummary


class WorkItemSummaryKwargs(TypedDict):
    """Kwargs shape produced by ``build_work_item_summary_kwargs``."""

    id: str
    title: str
    type: str
    status: str
    priority: str
    updated: str
    space_id: str
    document_name: str
    assignee_ids: list[str]

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# Default page size — Polarion caps at 100.
DEFAULT_PAGE_SIZE: Final[int] = 100

# Sparse fieldsets for list / detail endpoints.
WI_LIST_FIELDS: Final[str] = "title,type,status,priority,updated"
WI_DETAIL_FIELDS: Final[str] = "title,description,type,status,priority,updated"

# ---------------------------------------------------------------------------
# Functions
# ---------------------------------------------------------------------------


def get_client(ctx: Context) -> PolarionClient:
    """Extract ``PolarionClient`` from the lifespan context.

    Args:
        ctx: FastMCP tool context.

    Returns:
        The active ``PolarionClient`` instance.
    """
    lifespan_ctx = ctx.lifespan_context
    if "polarion_client" not in lifespan_ctx:  # pragma: no cover
        msg = "polarion_client is missing from lifespan_context"
        raise TypeError(msg)

    client = lifespan_ctx["polarion_client"]
    if not isinstance(client, PolarionClient):  # pragma: no cover
        msg = (
            "polarion_client is not a PolarionClient instance"
            f" (got {type(client).__name__})"
        )
        raise TypeError(msg)
    return client


def safe_str(value: object) -> str:
    """Convert a value to ``str``, returning ``""`` for ``None``."""
    if value is None:
        return ""
    return str(value)


def extract_total_count(response: dict[str, object]) -> int:
    """Extract ``meta.totalCount`` from a JSON:API response.

    Args:
        response: Decoded JSON:API response.

    Returns:
        The total count, or 0 if the field is missing.
    """
    meta = response.get("meta")
    if isinstance(meta, dict):
        total = meta.get("totalCount", 0)
        if isinstance(total, int):
            return total
    return 0


def has_links_next(response: dict[str, object]) -> bool:
    """Check whether the JSON:API response contains a ``links.next`` key.

    Args:
        response: Decoded JSON:API response.

    Returns:
        ``True`` if the server indicates a next page exists.
    """
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
    """Determine whether more pages exist after the current one.

    Uses ``total`` when it is reliable (> 0).  When ``total`` is 0
    (Polarion sometimes omits ``meta.totalCount``), falls back to
    ``links.next`` if present, otherwise to a heuristic based on
    whether the current page is full.

    Args:
        response: Decoded JSON:API response (used for ``links.next``).
        total: Resolved total count (may be 0 if unknown).
        page_number: Current 1-based page number.
        page_size: Requested page size.
        items_count: Number of items returned on this page.

    Returns:
        ``True`` if additional pages likely exist.
    """
    if total > 0:
        return total > page_number * page_size
    # totalCount unavailable — prefer links.next, else heuristic.
    if has_links_next(response):
        return True
    return items_count == page_size


def encode_path_segment(segment: str) -> str:
    """URL-encode a single path segment (e.g. document name with spaces).

    Args:
        segment: Raw path segment string.

    Returns:
        URL-encoded segment safe for use in URL paths.
    """
    return quote(segment, safe="")


def build_included_workitem_map(
    response: dict[str, object],
) -> dict[str, dict[str, object]]:
    """Build a lookup dict of included work items from a JSON:API response.

    Args:
        response: Decoded JSON:API response with an ``included`` array.

    Returns:
        Mapping from full work-item ID to the included resource dict.
    """
    wi_map: dict[str, dict[str, object]] = {}
    included = response.get("included", [])
    if isinstance(included, list):
        for inc in included:
            if isinstance(inc, dict) and inc.get("type") == "workitems":
                wi_map[safe_str(inc.get("id", ""))] = inc
    return wi_map


def extract_relationship_id(
    rels: dict[str, object],
    rel_name: str,
) -> str:
    """Extract the ``data.id`` of a named relationship.

    Args:
        rels: The ``relationships`` dict of a JSON:API resource.
        rel_name: Relationship key (e.g. ``'nextPart'``).

    Returns:
        The related resource ID, or ``""`` if absent.
    """
    rel = rels.get(rel_name, {})
    if isinstance(rel, dict):
        inner = rel.get("data")
        if isinstance(inner, dict):
            return safe_str(inner.get("id", ""))
    return ""


def extract_relationship_ids(
    rels: dict[str, object],
    rel_name: str,
) -> list[str]:
    """Extract the ``data[].id`` list of a to-many relationship.

    Args:
        rels: The ``relationships`` dict of a JSON:API resource.
        rel_name: Relationship key (e.g. ``'assignee'``).

    Returns:
        List of related resource IDs in declaration order. Empty list
        when the relationship is absent or its data array is empty.
    """
    rel = rels.get(rel_name, {})
    if not isinstance(rel, dict):
        return []
    data = rel.get("data")
    if not isinstance(data, list):
        return []
    ids: list[str] = []
    for entry in data:
        if isinstance(entry, dict):
            entry_id = safe_str(entry.get("id", ""))
            if entry_id:
                ids.append(entry_id)
    return ids


def split_module_id(module_full_id: str) -> tuple[str, str]:
    """Split a module relationship ID into (space_id, document_name).

    The Polarion module ID has the format
    ``{projectId}/{spaceId}/{documentName}`` where ``documentName`` may
    itself contain ``/`` segments. Returns ``("", "")`` when the ID does
    not have at least three segments.
    """
    if not module_full_id:
        return ("", "")
    parts = module_full_id.split("/", 2)
    expected_segments = 3
    if len(parts) < expected_segments:
        return ("", "")
    return (parts[1], parts[2])


def extract_short_id(full_id: str) -> str:
    """Strip the project / path prefix from a JSON:API ID.

    For ``"projectId/MCPT-001"`` returns ``"MCPT-001"``.
    For ``"alice"`` (no slashes) returns ``"alice"`` unchanged.
    """
    if "/" not in full_id:
        return full_id
    return full_id.rsplit("/", maxsplit=1)[-1]


def build_work_item_summary_kwargs(
    item: dict[str, object],
) -> WorkItemSummaryKwargs:
    """Extract ``WorkItemSummary`` kwargs from a single JSON:API resource.

    Centralises the attribute + relationship parsing used by both list
    and detail endpoints so that ``WorkItemDetail`` stays a strict
    superset of ``WorkItemSummary``.

    Args:
        item: A single JSON:API resource object (``data`` element).

    Returns:
        Dict suitable for ``WorkItemSummary(**kwargs)`` /
        ``WorkItemDetail(**kwargs, description=..., project_id=...)``.
    """
    attrs = item.get("attributes", {})
    if not isinstance(attrs, dict):
        attrs = {}
    rels = item.get("relationships", {})
    if not isinstance(rels, dict):
        rels = {}

    module_id = extract_relationship_id(rels, "module")
    space_id, document_name = split_module_id(module_id)
    assignee_ids = [
        extract_short_id(uid) for uid in extract_relationship_ids(rels, "assignee")
    ]

    return {
        "id": extract_short_id(safe_str(item.get("id", ""))),
        "title": safe_str(attrs.get("title", "")),
        "type": safe_str(attrs.get("type", "")),
        "status": safe_str(attrs.get("status", "")),
        "priority": safe_str(attrs.get("priority", "")),
        "updated": safe_str(attrs.get("updated", "")),
        "space_id": space_id,
        "document_name": document_name,
        "assignee_ids": assignee_ids,
    }


def parse_work_item_summaries(
    data: object,
) -> list[WorkItemSummary]:
    """Parse a JSON:API ``data`` array into ``WorkItemSummary`` models.

    Args:
        data: The ``data`` field from a JSON:API response.

    Returns:
        List of parsed ``WorkItemSummary`` instances.
    """
    items: list[WorkItemSummary] = []
    if not isinstance(data, list):
        return items

    for item in data:
        if not isinstance(item, dict):
            continue
        items.append(WorkItemSummary(**build_work_item_summary_kwargs(item)))
    return items


__all__: list[str] = [
    "DEFAULT_PAGE_SIZE",
    "WI_DETAIL_FIELDS",
    "WI_LIST_FIELDS",
    "build_included_workitem_map",
    "build_work_item_summary_kwargs",
    "compute_has_more",
    "encode_path_segment",
    "extract_relationship_id",
    "extract_relationship_ids",
    "extract_short_id",
    "extract_total_count",
    "get_client",
    "has_links_next",
    "parse_work_item_summaries",
    "safe_str",
    "split_module_id",
]
