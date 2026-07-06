"""Custom-field key validation shared by the work-item / document / test-run
guards: one control-flow engine over axis-supplied cache and fetch closures —
the sampling strategies differ per axis, the check algorithm must not.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable

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
    """Reject ``custom_fields`` keys absent from the axis's sampled schema.

    Unknown key vs *cached* schema forces one fresh re-fetch before rejecting;
    empty schema fails closed with ``RuntimeError(empty_schema_error)`` (ghost
    write unrecoverable).
    """
    schema = get_cached()
    fetched_fresh = schema is None
    if schema is None:
        schema = await fetch()

    if all(key in schema for key in custom_fields):
        return

    # Unknown key may be admin-added since caching; refetch once before rejecting.
    if not fetched_fresh:
        invalidate()
        schema = await fetch()

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
    response: dict[str, object], allowlist: frozenset[str]
) -> frozenset[str]:
    keys: set[str] = set()
    data = response.get("data", [])
    if isinstance(data, list):
        for entry in data:
            if not isinstance(entry, dict):
                continue
            attrs = entry.get("attributes")
            if isinstance(attrs, dict):
                keys.update(
                    k for k in attrs if isinstance(k, str) and k not in allowlist
                )
    return frozenset(keys)
