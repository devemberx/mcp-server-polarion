"""Core cross-cutting helpers for ``tools`` (not public API): client lookup,
string coercion, path encoding, option-list formatting, lucene-id guarding.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from typing import Final
from urllib.parse import quote

from fastmcp import Context

from mcp_server_polarion.core.client import PolarionClient

# Ceiling for option lists in guard errors: showing the full set beats a
# list_*_enum_options re-call, but a pathological enum must not flood context.
OPTION_LIST_LIMIT: Final[int] = 50


def get_client(ctx: Context) -> PolarionClient:
    """Extract the active ``PolarionClient`` from the lifespan context."""
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


def format_option_list(options: Iterable[str], limit: int = OPTION_LIST_LIMIT) -> str:
    """Sorted option list for an error message; past *limit*, truncates to the
    first *limit* items plus a ``(+N more)`` suffix.
    """
    ordered = sorted(options)
    if len(ordered) <= limit:
        return repr(ordered)
    return f"{repr(ordered[:limit])[:-1]}, ...] (+{len(ordered) - limit} more)"


def safe_str(value: object) -> str:
    """Convert a value to ``str``, returning ``""`` for ``None``."""
    if value is None:
        return ""
    return str(value)


def encode_path_segment(segment: str) -> str:
    """URL-encode a single path segment (e.g. a document name with spaces)."""
    return quote(segment, safe="")


# Thin guard before Lucene substitution, not a format validator.
_WORK_ITEM_ID_PATTERN: Final[re.Pattern[str]] = re.compile(r"^[A-Za-z0-9_-]+$")


def validate_work_item_id_for_lucene(work_item_id: str) -> None:
    """Reject ids outside ``[A-Za-z0-9_-]`` — Lucene treats punctuation as
    operators, so an unescaped id could reshape the query.
    """
    if not _WORK_ITEM_ID_PATTERN.match(work_item_id):
        msg = (
            f"work_item_id '{work_item_id}' contains characters outside "
            "[A-Za-z0-9_-]; cannot embed safely in a Lucene query."
        )
        raise ValueError(msg)


__all__: list[str] = [
    "OPTION_LIST_LIMIT",
    "encode_path_segment",
    "format_option_list",
    "get_client",
    "safe_str",
    "validate_work_item_id_for_lucene",
]
