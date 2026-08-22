"""Custom-field key validation shared by work-item / document / test-run
guards: one control-flow engine over axis-supplied cache + fetch closures —
sampling strategies differ per axis, check algorithm must not.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable

from mcp_server_polarion.tools._shared.guard._revalidate import resolve_with_refetch
from mcp_server_polarion.tools._shared.helpers import format_option_list


async def check_custom_keys(  # noqa: PLR0913
    custom_fields: dict[str, object],
    *,
    get_cached: Callable[[], frozenset[str] | None],
    invalidate: Callable[[], None],
    fetch: Callable[[], Awaitable[frozenset[str]]],
    scope: str,
    discovery_tool: str,
    empty_schema_error: str,
) -> None:
    """Reject ``custom_fields`` keys absent from axis's sampled schema.

    Unknown key vs *cached* schema force one fresh re-fetch before reject;
    empty schema fail closed with ``RuntimeError(empty_schema_error)`` (ghost
    write unrecoverable).
    """

    def known_keys(schema: frozenset[str]) -> bool:
        return all(key in schema for key in custom_fields)

    schema = await resolve_with_refetch(
        get_cached=get_cached,
        invalidate=invalidate,
        fetch=fetch,
        accepts=known_keys,
    )
    if known_keys(schema):
        return

    if not schema:
        raise RuntimeError(empty_schema_error)

    reject_unknown_custom_keys(
        custom_fields, schema, scope=scope, discovery_tool=discovery_tool
    )


def reject_unknown_custom_keys(
    custom_fields: dict[str, object],
    known: frozenset[str],
    *,
    scope: str,
    discovery_tool: str,
) -> None:
    unknown = sorted(k for k in custom_fields if k not in known)
    if unknown:
        raise ValueError(
            f"custom_fields key(s) {unknown} were not present in any prior "
            f"{discovery_tool} for {scope}. Known keys: {format_option_list(known)}. "
            f"Unknown keys persist as silent ghost attributes -- fetch a sample "
            f"first to discover the project's real custom-field ids."
        )


def custom_keys_from_data_list(
    data: list[object], allowlist: frozenset[str]
) -> frozenset[str]:
    """Non-allowlisted attribute keys across a page's (pre-narrowed) entries."""
    keys: set[str] = set()
    for entry in data:
        if not isinstance(entry, dict):
            continue
        attrs = entry.get("attributes")
        if isinstance(attrs, dict):
            keys.update(k for k in attrs if isinstance(k, str) and k not in allowlist)
    return frozenset(keys)
