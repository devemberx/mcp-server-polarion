"""Shared wrappers, enums, and constants used across model groups."""

from __future__ import annotations

from typing import Final

from pydantic import BaseModel

# Internal payload-builder alias only — result models expose previews as
# `Mapping[str, object]` because the recursive self-reference breaks FastMCP's
# `json_schema_to_type` (unresolved `ForwardRef('Root')`).
type JsonValue = (
    str | int | float | bool | None | list[JsonValue] | dict[str, JsonValue]
)

# Per-item cap against prompt-injected multi-MB bodies; real bodies ~30 KB.
MAX_BODY_HTML_LEN: Final[int] = 2_000_000


class PaginatedResult[T](BaseModel):
    """Paginated response wrapper used by all list tools."""

    items: list[T]
    total_count: int
    page: int
    page_size: int
    has_more: bool = False


class EnumOption(BaseModel):
    """Single enum option returned by ``list_*_enum_options``."""

    id: str
    name: str
    description: str = ""
    default: bool = False
    hidden: bool = False
    terminal: bool = False
