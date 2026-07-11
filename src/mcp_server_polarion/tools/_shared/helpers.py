"""Core cross-cutting helpers for ``tools`` (not public API): client lookup,
string coercion, path encoding, option-list formatting, lucene-id guarding.
"""

from __future__ import annotations

import re
from collections import Counter
from collections.abc import Iterable, Iterator
from contextlib import contextmanager
from typing import Final
from urllib.parse import quote

from fastmcp import Context

from mcp_server_polarion.core.client import PolarionClient

# Ceiling for option lists in guard errors: full set beat list_*_enum_options
# re-call, but pathological enum must not flood context.
OPTION_LIST_LIMIT: Final[int] = 50


def get_client(ctx: Context) -> PolarionClient:
    """Active ``PolarionClient`` from lifespan context."""
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
    """Sorted option list for error message; past *limit*, truncate to
    first *limit* items + ``(+N more)`` suffix.
    """
    ordered = sorted(options)
    if len(ordered) <= limit:
        return repr(ordered)
    return f"{repr(ordered[:limit])[:-1]}, ...] (+{len(ordered) - limit} more)"


def safe_str(value: object) -> str:
    """``str(value)``; ``""`` for ``None``."""
    if value is None:
        return ""
    return str(value)


def safe_float(value: object) -> float:
    """``float(value)`` for numbers; ``0.0`` otherwise. bool excluded —
    int subclass, would read as 1.0.
    """
    if isinstance(value, int | float) and not isinstance(value, bool):
        return float(value)
    return 0.0


def encode_path_segment(segment: str) -> str:
    """URL-encode single path segment (e.g. document name with spaces)."""
    return quote(segment, safe="")


# Thin guard before Lucene substitution, not a format validator.
_WORK_ITEM_ID_PATTERN: Final[re.Pattern[str]] = re.compile(r"^[A-Za-z0-9_-]+$")


def validate_work_item_id_for_lucene(work_item_id: str) -> None:
    """Reject ids outside ``[A-Za-z0-9_-]`` — Lucene treat punctuation as
    operators; unescaped id could reshape query.
    """
    if not _WORK_ITEM_ID_PATTERN.match(work_item_id):
        msg = (
            f"work_item_id '{work_item_id}' contains characters outside "
            "[A-Za-z0-9_-]; cannot embed safely in a Lucene query."
        )
        raise ValueError(msg)


def ensure_unique_ids(ids: Iterable[str], *, label: str) -> None:
    """Reject duplicate ids in one bulk batch — server apply order for
    duplicate resources in single request undefined.
    """
    duplicates = sorted(id_ for id_, count in Counter(ids).items() if count > 1)
    if duplicates:
        msg = (
            f"Duplicate {label}(s) {duplicates} in one batch; merge the "
            f"changes for each id into a single item."
        )
        raise ValueError(msg)


@contextmanager
def reraise_with_item_context(index: int, item_id: str) -> Iterator[None]:
    """Prefix per-item guard ``ValueError`` with batch position + id so
    bulk-tool errors name offending item; other exceptions (project-level
    auth/backend failures) pass through unwrapped.
    """
    try:
        yield
    except ValueError as exc:
        msg = f"items[{index}] ('{item_id}'): {exc}"
        raise ValueError(msg) from exc


__all__: list[str] = [
    "OPTION_LIST_LIMIT",
    "encode_path_segment",
    "ensure_unique_ids",
    "format_option_list",
    "get_client",
    "reraise_with_item_context",
    "safe_float",
    "safe_str",
    "validate_work_item_id_for_lucene",
]
