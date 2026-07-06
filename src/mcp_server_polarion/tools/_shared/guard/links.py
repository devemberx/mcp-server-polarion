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
from mcp_server_polarion.tools._shared.guard._common import GUARD_PAGE_SIZE
from mcp_server_polarion.tools._shared.guard._errors import (
    unauthorized_write_block,
    unreachable_write_block,
)
from mcp_server_polarion.tools._shared.guard.enums import check_project_enum_roles
from mcp_server_polarion.tools._shared.helpers import (
    encode_path_segment,
    format_option_list,
    safe_str,
)
from mcp_server_polarion.tools._shared.parse import extract_short_id

logger = logging.getLogger("mcp_server_polarion.tools._shared.guard.links")


async def guard_work_item_link_roles(
    client: PolarionClient,
    project_id: str,
    roles: Iterable[str],
) -> None:
    """Reject link roles not in ``workitem-link-role`` — an unknown role stores
    verbatim (HTTP 201) as a ghost link.
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
    """Reject hyperlink roles not in the project's ``hyperlink-role`` enum
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


async def _existing_target_ids(
    client: PolarionClient,
    project_id: str,
    target_ids: frozenset[str],
) -> frozenset[str]:
    """Subset of *target_ids* that exist in *project_id*, via chunked
    ``id:(...)`` queries. 404 (project missing) propagates to caller.
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
        response = await client.get(path, params=params)
        data = response.get("data", [])
        if isinstance(data, list):
            for entry in data:
                if isinstance(entry, dict):
                    found.add(extract_short_id(safe_str(entry.get("id", ""))))
    return frozenset(found)


async def guard_work_item_link_targets(
    client: PolarionClient,
    source_project_id: str,
    links: list[WorkItemLinkSpec],
) -> None:
    """Reject links whose target work item does not exist — Polarion stores a
    nonexistent target as a silent dangling link (HTTP 201, empty
    title/type/status). One ``id:(...)`` query per target project.
    """
    by_project: dict[str, set[str]] = {}
    for spec in links:
        target_project = spec.target_project_id or source_project_id
        by_project.setdefault(target_project, set()).add(spec.target_work_item_id)

    missing: list[str] = []
    for project_id, requested in by_project.items():
        try:
            existing = await _existing_target_ids(
                client, project_id, frozenset(requested)
            )
        except PolarionNotFoundError:
            missing.extend(f"{project_id}/{wi}" for wi in sorted(requested))
            continue
        except PolarionAuthError as exc:
            raise unauthorized_write_block("link targets", project_id) from exc
        except PolarionError as exc:
            raise unreachable_write_block("link targets", project_id, exc) from exc
        missing.extend(f"{project_id}/{wi}" for wi in sorted(requested - existing))

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
    """Composite ids of every outgoing link on the source work item — each
    ``data[].id`` is the 5-segment composite the delete payload reconstructs,
    so it is set-membership-testable directly. 404 propagates.
    """
    path = (
        f"/projects/{encode_path_segment(project_id)}"
        f"/workitems/{encode_path_segment(work_item_id)}/linkedworkitems"
    )
    found: set[str] = set()
    page_number = 1
    while True:
        params: dict[str, str | int] = {
            "fields[linkedworkitems]": "id",
            "page[size]": GUARD_PAGE_SIZE,
            "page[number]": page_number,
        }
        response = await client.get(path, params=params)
        data = response.get("data", [])
        if not isinstance(data, list):
            break
        for entry in data:
            if isinstance(entry, dict):
                link_id = entry.get("id")
                if isinstance(link_id, str) and link_id:
                    found.add(link_id)
        if len(data) < GUARD_PAGE_SIZE:
            break
        page_number += 1
    return frozenset(found)


async def partition_delete_links(
    client: PolarionClient,
    project_id: str,
    work_item_id: str,
    link_ids: list[str],
) -> tuple[list[str], list[str]]:
    """Pre-read existing links and split *link_ids* into ``(matched, not_found)``
    — the only way to surface the no-ops Polarion's 204 hides. Fail-closed:
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
        raise PermissionError(
            "Cannot read existing work item links -- check your POLARION_TOKEN "
            "permissions."
        ) from exc
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
