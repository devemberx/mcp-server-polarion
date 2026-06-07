"""Shared wrappers, enums, and constants used across model groups."""

from __future__ import annotations

from typing import Final

from pydantic import BaseModel, Field

# Recursive JSON-safe alias for internal payload builders. Result-model
# fields elsewhere intentionally surface payload previews as
# `Mapping[str, object]` instead of `dict[str, JsonValue]`: the recursive
# alias emits a `$defs/JsonValue` self-reference that the FastMCP client's
# `json_schema_to_type` cannot rebuild, producing an unresolved
# `ForwardRef('Root')` TypeAdapter and noisy errors on every write call.
type JsonValue = (
    str | int | float | bool | None | list[JsonValue] | dict[str, JsonValue]
)

# Caps a single body payload so a prompt-injected caller cannot ship a
# multi-megabyte blob to Polarion. Observed real document bodies stay under
# ~30 KB, so 2 MiB leaves ~70x headroom. This is a per-item bound; a bulk
# ``create_work_items`` batch can carry it once per item, so the aggregate
# request is bounded by item count, not by this constant alone.
MAX_BODY_HTML_LEN: Final[int] = 2_000_000


class PaginatedResult[T](BaseModel):
    """Paginated response wrapper used by all list tools."""

    items: list[T]
    total_count: int
    page: int
    page_size: int
    has_more: bool = False


class EnumOption(BaseModel):
    """Single enum option returned by ``list_*_enum_options``.

    Surfaces only what an LLM needs to pick a value before a write.
    """

    id: str = Field(description="Option id; pass verbatim to write tools.")
    name: str
    description: str = Field(default="", description="Empty when Polarion has none.")
    default: bool = False
    hidden: bool = Field(
        default=False, description="Hidden in the UI; avoid selecting."
    )
    terminal: bool = Field(
        default=False, description="For status fields, a workflow end-state."
    )
