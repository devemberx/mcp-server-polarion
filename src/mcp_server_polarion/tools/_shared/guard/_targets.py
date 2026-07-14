"""Work-item target existence shared by write guards (link targets,
test-record defects): chunked ``id:(...)`` queries + confirmed-positive
cache. Polarion never validate relationship targets -- nonexistent id
store as silent dangling reference (HTTP 201).
"""

from __future__ import annotations

from mcp_server_polarion.core.client import PolarionClient
from mcp_server_polarion.core.exceptions import PolarionNotFoundError
from mcp_server_polarion.tools._shared.cache import (
    get_cached_confirmed_work_item,
    store_cached_confirmed_work_item,
)
from mcp_server_polarion.tools._shared.guard._http import (
    GUARD_PAGE_SIZE,
    guarded_get,
)
from mcp_server_polarion.tools._shared.helpers import encode_path_segment, safe_str
from mcp_server_polarion.tools._shared.parse import extract_short_id


async def existing_target_ids(
    client: PolarionClient,
    project_id: str,
    target_ids: frozenset[str],
) -> frozenset[str]:
    """Subset of *target_ids* existing in *project_id*, via chunked
    ``id:(...)`` queries. 404 (project missing) propagate to caller;
    auth/other failures translate fail-closed.
    """
    ordered = sorted(target_ids)
    found: set[str] = set()
    for start in range(0, len(ordered), GUARD_PAGE_SIZE):
        chunk = ordered[start : start + GUARD_PAGE_SIZE]
        params: dict[str, str | int] = {
            "query": f"id:({' '.join(chunk)})",
            "fields[workitems]": "id",
            "page[size]": GUARD_PAGE_SIZE,
            "page[number]": 1,
        }
        path = f"/projects/{encode_path_segment(project_id)}/workitems"
        response = await guarded_get(
            client, path, params, what="link targets", project_id=project_id
        )
        data = response.get("data", [])
        if isinstance(data, list):
            for entry in data:
                if isinstance(entry, dict):
                    found.add(extract_short_id(safe_str(entry.get("id", ""))))
    return frozenset(found)


async def missing_work_item_targets(
    client: PolarionClient,
    by_project: dict[str, set[str]],
) -> list[str]:
    """``"Proj/WI"`` ids from *by_project* not existing in Polarion.
    Confirmed-existing ids cache across calls (server-load requirement) --
    never negatives, a missing WI may be created later. Only cache misses
    reach Polarion, grouped one ``id:(...)`` query per project.
    Project-level 404 = whole group missing (fail closed).
    """
    missing: list[str] = []
    for proj, requested in by_project.items():
        to_query = {
            wi for wi in requested if get_cached_confirmed_work_item(proj, wi) is None
        }
        if not to_query:
            continue
        try:
            existing = await existing_target_ids(client, proj, frozenset(to_query))
        except PolarionNotFoundError:
            missing.extend(f"{proj}/{wi}" for wi in sorted(to_query))
            continue
        for wi in existing:
            store_cached_confirmed_work_item(proj, wi)
        missing.extend(f"{proj}/{wi}" for wi in sorted(to_query - existing))
    return missing
