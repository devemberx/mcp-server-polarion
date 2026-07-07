"""Shared wrappers + constants across model groups."""

from __future__ import annotations

from typing import Final

from pydantic import BaseModel

# Payload-builder alias only — result models expose previews as
# `Mapping[str, object]`: recursive self-reference break FastMCP
# `json_schema_to_type` (unresolved `ForwardRef('Root')`).
type JsonValue = (
    str | int | float | bool | None | list[JsonValue] | dict[str, JsonValue]
)

# Per-item cap against prompt-injected multi-MB bodies; real bodies ~30 KB.
MAX_BODY_HTML_LEN: Final[int] = 2_000_000


class PaginatedResult[T](BaseModel):
    """Paginated response wrapper for all list tools."""

    items: list[T]
    total_count: int
    page: int
    page_size: int
    has_more: bool = False
