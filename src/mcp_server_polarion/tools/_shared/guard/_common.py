"""Paging constant and custom-field key helpers shared by guard submodules."""

from __future__ import annotations

from mcp_server_polarion.tools._shared.helpers import format_option_list

GUARD_PAGE_SIZE: int = 100


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
