"""Shared helpers for MCP tool implementations.

Internal module used by ``tools.read`` (and future ``tools.write``).
Every function here is intentionally private (``_``-prefixed) to the
``tools`` package — they are **not** part of the public API.
"""

from __future__ import annotations

from typing import Final
from urllib.parse import quote

from fastmcp import Context

from mcp_server_polarion.core.client import PolarionClient
from mcp_server_polarion.models import WorkItemSummary

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# Default page size — Polarion caps at 100.
DEFAULT_PAGE_SIZE: Final[int] = 100

# Sparse fieldsets for list / detail endpoints.
WI_LIST_FIELDS: Final[str] = "title,type,status"
WI_DETAIL_FIELDS: Final[str] = "title,description,type,status"

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
    lifespan_ctx: dict[str, object] = ctx.request_context.lifespan_context  # type: ignore[union-attr]
    client = lifespan_ctx["polarion_client"]
    if not isinstance(client, PolarionClient):  # pragma: no cover
        msg = "polarion_client is not a PolarionClient instance"
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
        attrs = item.get("attributes", {})
        if not isinstance(attrs, dict):
            attrs = {}

        # Extract ID from JSON:API id
        # (format: "projectId/WI-001").
        raw_id = safe_str(item.get("id", ""))
        wi_id = raw_id.split("/", maxsplit=1)[-1] if "/" in raw_id else raw_id

        items.append(
            WorkItemSummary(
                id=wi_id,
                title=safe_str(attrs.get("title", "")),
                type=safe_str(attrs.get("type", "")),
                status=safe_str(attrs.get("status", "")),
            )
        )
    return items


__all__: list[str] = [
    "DEFAULT_PAGE_SIZE",
    "WI_DETAIL_FIELDS",
    "WI_LIST_FIELDS",
    "build_included_workitem_map",
    "encode_path_segment",
    "extract_relationship_id",
    "extract_total_count",
    "get_client",
    "parse_work_item_summaries",
    "safe_str",
]
