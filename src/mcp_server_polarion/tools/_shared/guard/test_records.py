"""Test-record write guards: ``result`` enum, defect-target existence.
Server 201s both silently when wrong (verbatim bogus result stored;
dangling defect id stored, WI type unchecked) -- live-verified.
"""

from __future__ import annotations

from collections.abc import Iterable

from mcp_server_polarion.core.client import PolarionClient
from mcp_server_polarion.tools._shared.guard._targets import missing_work_item_targets
from mcp_server_polarion.tools._shared.guard.enums import check_project_enum_roles
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
    Existence + confirmed-positive caching shared via
    :func:`missing_work_item_targets`.
    """
    by_project: dict[str, set[str]] = {}
    for defect_id in defect_ids:
        proj, sep, wi = defect_id.partition("/")
        if not sep:
            proj, wi = project_id, defect_id
        by_project.setdefault(proj, set()).add(wi)

    missing = await missing_work_item_targets(client, by_project)
    if missing:
        raise ValueError(
            f"Defect target work item(s) {format_option_list(missing)} do not "
            f"exist. A nonexistent target stores as a silent dangling defect "
            f"link (HTTP 201, target type unchecked) -- use list_work_items to "
            f"find valid target ids first."
        )
