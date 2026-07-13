"""Test-record write guards: result enum (``testing`` context), defect-target
existence.
"""

from __future__ import annotations

from collections.abc import Iterable

from mcp_server_polarion.core.client import PolarionClient
from mcp_server_polarion.core.exceptions import PolarionNotFoundError
from mcp_server_polarion.tools._shared.guard.enums import check_project_enum_roles
from mcp_server_polarion.tools._shared.guard.links import existing_target_ids
from mcp_server_polarion.tools._shared.helpers import format_option_list


async def guard_test_record_results(
    client: PolarionClient,
    project_id: str,
    results: Iterable[str],
) -> None:
    """Reject results not in ``testing/test-result`` enum -- test records
    have no ``getAvailableOptions``, and unknown result stores verbatim
    (HTTP 204) as ghost.
    """
    await check_project_enum_roles(
        client,
        project_id,
        "test-result",
        results,
        field_label="result",
        discovery_hint="use list_test_records to see values already in use.",
        context="testing",
    )


async def guard_test_record_defects(
    client: PolarionClient,
    defect_ids: Iterable[str],
) -> None:
    """Reject defect links whose target work item not exist -- Polarion
    store nonexistent target as silent dangling link (HTTP 204, no error).
    One ``id:(...)`` query per target project.
    """
    by_project: dict[str, set[str]] = {}
    for defect_id in defect_ids:
        project_id, _, work_item_id = defect_id.partition("/")
        by_project.setdefault(project_id, set()).add(work_item_id)

    missing: list[str] = []
    for project_id, requested in by_project.items():
        try:
            existing = await existing_target_ids(
                client, project_id, frozenset(requested)
            )
        except PolarionNotFoundError:
            missing.extend(f"{project_id}/{wi}" for wi in sorted(requested))
            continue
        missing.extend(f"{project_id}/{wi}" for wi in sorted(requested - existing))

    if missing:
        raise ValueError(
            f"Defect work item(s) {format_option_list(missing)} do not exist. "
            f"A nonexistent target stores as a silent dangling link (HTTP 204) "
            f"-- verify via get_work_item / create_work_items first."
        )
