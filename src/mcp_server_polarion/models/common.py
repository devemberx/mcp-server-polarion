"""Shared wrappers, enums, and constants used across model groups."""

from __future__ import annotations

from typing import Final

from pydantic import BaseModel

# Recursive JSON-safe alias for internal payload builders. Result models expose
# previews as `Mapping[str, object]`, not `dict[str, JsonValue]`: the alias'
# `$defs/JsonValue` self-reference breaks FastMCP's `json_schema_to_type`
# (unresolved `ForwardRef('Root')`, noisy errors on every write).
type JsonValue = (
    str | int | float | bool | None | list[JsonValue] | dict[str, JsonValue]
)

# Per-item cap blocking a prompt-injected multi-MB body. Real bodies stay
# ~30 KB; 2 MiB leaves ~70x headroom. Bulk requests bound by item count, not
# this constant alone.
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
