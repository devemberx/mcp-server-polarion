"""Test-record write guards: ``result`` enum, defect-target existence.
Server 201s both silently when wrong (verbatim bogus result stored;
dangling defect id stored, WI type unchecked) -- live-verified.
"""

from __future__ import annotations

from collections.abc import Iterable

from mcp_server_polarion.core.client import PolarionClient
from mcp_server_polarion.core.exceptions import PolarionNotFoundError
from mcp_server_polarion.tools._shared.cache import (
    get_cached_confirmed_work_item,
    store_cached_confirmed_work_item,
)
from mcp_server_polarion.tools._shared.guard.enums import check_project_enum_roles
from mcp_server_polarion.tools._shared.guard.links import _existing_target_ids
from mcp_server_polarion.tools._shared.helpers import format_option_list


async def guard_test_record_results(
    client: PolarionClient,
    project_id: str,
    results: Iterable[str],
) -> None:
    """Validate ``result`` against ``testing``-context ``test-result``
    enum -- unknown value stores verbatim (HTTP 201), ghosting silently
    against Lucene/UI result filters.
    """
    await check_project_enum_roles(
        client,
        project_id,
        "test-result",
        results,
        field_label="result",
        discovery_hint="use list_test_records to see values in use.",
        context="testing",
    )


async def guard_test_record_defect_targets(
    client: PolarionClient,
    project_id: str,
    defect_ids: Iterable[str],
) -> None:
    """Reject defect targets that don't exist -- Polarion silently 201s a
    dangling defect id (HTTP 201, target WI type unchecked too, live-verified).
    *defect_ids* = full ``"Proj/WI"`` ids (bare id falls back to *project_id*).
    Confirmed-existing ids cache across calls (server-load requirement) --
    never negatives, a missing WI may be created later. Only cache misses
    reach Polarion, grouped one ``id:(...)`` query per project.
    """
    by_project: dict[str, set[str]] = {}
    for defect_id in defect_ids:
        proj, sep, wi = defect_id.partition("/")
        if not sep:
            proj, wi = project_id, defect_id
        by_project.setdefault(proj, set()).add(wi)

    missing: list[str] = []
    for proj, requested in by_project.items():
        to_query = {
            wi for wi in requested if get_cached_confirmed_work_item(proj, wi) is None
        }
        if not to_query:
            continue
        try:
            existing = await _existing_target_ids(client, proj, frozenset(to_query))
        except PolarionNotFoundError:
            missing.extend(f"{proj}/{wi}" for wi in sorted(to_query))
            continue
        for wi in existing:
            store_cached_confirmed_work_item(proj, wi)
        missing.extend(f"{proj}/{wi}" for wi in sorted(to_query - existing))

    if missing:
        raise ValueError(
            f"Defect target work item(s) {format_option_list(missing)} do not "
            f"exist. A nonexistent target stores as a silent dangling defect "
            f"link (HTTP 201, target type unchecked) -- use list_work_items to "
            f"find valid target ids first."
        )
