"""Link write guards: roles, target existence, delete-time link matching."""

from __future__ import annotations

import logging
from collections.abc import Iterable

from mcp_server_polarion.core.client import PolarionClient
from mcp_server_polarion.core.exceptions import (
    PolarionAuthError,
    PolarionError,
    PolarionNotFoundError,
)
from mcp_server_polarion.models import WorkItemLinkSpec
from mcp_server_polarion.tools._shared.errors import auth_error
from mcp_server_polarion.tools._shared.guard._http import paged_responses
from mcp_server_polarion.tools._shared.guard._targets import missing_work_item_targets
from mcp_server_polarion.tools._shared.guard.enums import check_project_enum_roles
from mcp_server_polarion.tools._shared.helpers import (
    encode_path_segment,
    format_option_list,
)

logger = logging.getLogger("mcp_server_polarion.tools._shared.guard.links")


async def guard_work_item_link_roles(
    client: PolarionClient,
    project_id: str,
    roles: Iterable[str],
) -> None:
    """Reject link roles not in ``workitem-link-role`` — unknown role store
    verbatim (HTTP 201) as ghost link.
    """
    await check_project_enum_roles(
        client,
        project_id,
        "workitem-link-role",
        roles,
        field_label="role",
        discovery_hint=(
            "read an existing link with list_work_item_links to see the "
            "project's configured roles."
        ),
    )


async def guard_hyperlink_roles(
    client: PolarionClient,
    project_id: str,
    roles: Iterable[str],
) -> None:
    """Reject hyperlink roles not in project ``hyperlink-role`` enum
    (typically ``ref_int``/``ref_ext``) — unknown roles ghost silently.
    """
    await check_project_enum_roles(
        client,
        project_id,
        "hyperlink-role",
        roles,
        field_label="hyperlink role",
        discovery_hint=(
            "use a configured id such as 'ref_int' (internal) or 'ref_ext' (external)."
        ),
    )


async def guard_work_item_link_targets(
    client: PolarionClient,
    source_project_id: str,
    links: list[WorkItemLinkSpec],
) -> None:
    """Reject links whose target work item not exist — Polarion store
    nonexistent target as silent dangling link (HTTP 201, empty
    title/type/status). Confirmed targets cache across calls; only cache
    misses reach Polarion, one ``id:(...)`` query per target project.
    """
    by_project: dict[str, set[str]] = {}
    for spec in links:
        target_project = spec.target_project_id or source_project_id
        by_project.setdefault(target_project, set()).add(spec.target_work_item_id)

    missing = await missing_work_item_targets(client, by_project)
    if missing:
        raise ValueError(
            f"Link target work item(s) {format_option_list(missing)} do not exist. "
            f"A nonexistent target stores as a silent dangling link (HTTP 201, empty "
            f"title/type/status) -- use list_work_items to find valid target ids first."
        )


async def _existing_forward_link_ids(
    client: PolarionClient,
    project_id: str,
    work_item_id: str,
) -> frozenset[str]:
    """Composite ids of every outgoing link on source work item — each
    ``data[].id`` = the 5-segment composite the delete payload reconstruct,
    so set-membership-testable directly. 404 propagate.
    """
    path = (
        f"/projects/{encode_path_segment(project_id)}"
        f"/workitems/{encode_path_segment(work_item_id)}/linkedworkitems"
    )
    base_params: dict[str, str | int] = {
        "fields[linkedworkitems]": "id",
    }
    found: set[str] = set()
    async for data, _response in paged_responses(client, path, base_params):
        for entry in data:
            if isinstance(entry, dict):
                link_id = entry.get("id")
                if isinstance(link_id, str) and link_id:
                    found.add(link_id)
    return frozenset(found)


async def partition_delete_links(
    client: PolarionClient,
    project_id: str,
    work_item_id: str,
    link_ids: list[str],
) -> tuple[list[str], list[str]]:
    """Pre-read existing links, split *link_ids* into ``(matched, not_found)``
    — only way to surface the no-ops Polarion's 204 hide. Fail-closed:
    missing source → ``ValueError``, auth → ``PermissionError``, else
    ``RuntimeError``.
    """
    try:
        existing = await _existing_forward_link_ids(client, project_id, work_item_id)
    except PolarionNotFoundError as exc:
        raise ValueError(
            f"Source work item '{work_item_id}' not found in project "
            f"'{project_id}'. Use `list_work_items` to discover valid IDs."
        ) from exc
    except PolarionAuthError as exc:
        raise auth_error("read existing work item links", exc) from exc
    except PolarionError as exc:
        logger.warning(
            "guard blocking delete: could not read existing links for "
            "project=%s work_item=%s (%s)",
            project_id,
            work_item_id,
            exc.message,
        )
        raise RuntimeError(
            f"Cannot read existing outgoing links for '{work_item_id}' in project "
            f"'{project_id}' before deleting: Polarion request failed "
            f"({exc.message}). Refusing the delete -- without the pre-read the "
            f"matched / no-op split would be unverifiable. Retry once Polarion is "
            f"reachable."
        ) from exc

    matched = [link_id for link_id in link_ids if link_id in existing]
    not_found = [link_id for link_id in link_ids if link_id not in existing]
    return matched, not_found
