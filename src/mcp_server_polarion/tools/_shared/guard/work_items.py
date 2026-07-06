"""Work-item write guards: enum args, custom-field keys/values, bulk id/type
resolution.
"""

from __future__ import annotations

from collections.abc import Iterable

from mcp_server_polarion.core.client import PolarionClient
from mcp_server_polarion.core.exceptions import (
    PolarionAuthError,
    PolarionError,
    PolarionNotFoundError,
)
from mcp_server_polarion.tools._shared.cache import (
    get_work_item_custom_keys,
    invalidate_work_item_custom_keys,
    store_work_item_custom_keys,
)
from mcp_server_polarion.tools._shared.custom_fields import (
    STANDARD_WORK_ITEM_ATTRIBUTES,
)
from mcp_server_polarion.tools._shared.fields import WORK_ITEM_DETAIL_FIELDS
from mcp_server_polarion.tools._shared.guard._custom_keys import (
    check_custom_keys,
    custom_keys_from_data_list,
)
from mcp_server_polarion.tools._shared.guard._errors import (
    unauthorized_write_block,
    unreachable_write_block,
)
from mcp_server_polarion.tools._shared.guard._http import (
    GUARD_PAGE_SIZE,
    guarded_get,
    paged_responses,
)
from mcp_server_polarion.tools._shared.guard.enums import (
    check_custom_field_enum_values,
    check_enum,
)
from mcp_server_polarion.tools._shared.helpers import (
    encode_path_segment,
    format_option_list,
    safe_str,
)
from mcp_server_polarion.tools._shared.parse import extract_short_id
from mcp_server_polarion.tools._shared.sql import one_item_per_custom_field_sql


async def guard_work_item_enums(  # noqa: PLR0913
    client: PolarionClient,
    project_id: str,
    work_item_type: str,
    *,
    type: str | None = None,
    status: str | None = None,
    severity: str | None = None,
    priority: str | None = None,
    resolution: str | None = None,
) -> None:
    """Validate supplied work-item enum args against ``getAvailableOptions``.

    ``work_item_type`` scopes status/severity/resolution/priority (``'~'`` =
    type-agnostic); ``type`` checked first so an invalid type raises before
    being reused as the scoping axis.
    """
    if type is not None and type != "":
        await check_enum(client, project_id, "workitems", "type", "~", type)
    if status is not None and status != "":
        await check_enum(
            client, project_id, "workitems", "status", work_item_type, status
        )
    if severity is not None and severity != "":
        await check_enum(
            client, project_id, "workitems", "severity", work_item_type, severity
        )
    if priority is not None and priority != "":
        await check_enum(
            client, project_id, "workitems", "priority", work_item_type, priority
        )
    if resolution is not None and resolution != "":
        await check_enum(
            client, project_id, "workitems", "resolution", work_item_type, resolution
        )


async def _fetch_work_item_type_custom_keys(
    client: PolarionClient,
    project_id: str,
    type_id: str,
) -> frozenset[str]:
    """Union of custom-field keys sampled from existing items of a type.

    MIN-per-key SQL, paged for >100 distinct keys. SQL rejection fails closed —
    a partial Lucene sample would silently false-reject real keys. Cached even
    if empty.
    """
    path = f"/projects/{encode_path_segment(project_id)}/workitems"
    base_params: dict[str, str | int] = {
        "query": one_item_per_custom_field_sql(project_id, type_id),
        "fields[workitems]": WORK_ITEM_DETAIL_FIELDS,
    }
    keys: set[str] = set()
    try:
        async for response in paged_responses(client, path, base_params):
            keys.update(
                custom_keys_from_data_list(response, STANDARD_WORK_ITEM_ATTRIBUTES)
            )
    except PolarionAuthError as exc:
        raise unauthorized_write_block("custom_fields keys", project_id) from exc
    except PolarionError as exc:
        raise unreachable_write_block("custom_fields keys", project_id, exc) from exc

    result = frozenset(keys)
    store_work_item_custom_keys(project_id, type_id, result)
    return result


async def _check_work_item_custom_keys(
    client: PolarionClient,
    project_id: str,
    work_item_type: str,
    custom_fields: dict[str, object],
) -> None:
    await check_custom_keys(
        custom_fields,
        get_cached=lambda: get_work_item_custom_keys(project_id, work_item_type),
        invalidate=lambda: invalidate_work_item_custom_keys(project_id, work_item_type),
        fetch=lambda: _fetch_work_item_type_custom_keys(
            client, project_id, work_item_type
        ),
        scope=f"work_item_type '{work_item_type}'",
        discovery_tool="sample of existing items",
        empty_schema_error=(
            f"Cannot verify custom_fields {format_option_list(custom_fields)} for "
            f"work_item_type '{work_item_type}' in project '{project_id}': no existing "
            f"item of this type has custom fields populated, so the schema can't be "
            f"sampled. Refusing the write -- an unknown key ghosts silently (invisible "
            f"to UI/Lucene). Do not create or edit items to work around this; ask the "
            f"user to confirm these custom-field ids exist for this type."
        ),
    )


async def guard_work_item_custom_fields(
    client: PolarionClient,
    project_id: str,
    work_item_type: str,
    custom_fields: dict[str, object],
) -> None:
    """Validate ``custom_fields`` keys then enum-typed values before a write.

    Keys-first order keeps ghost keys out of the enum probe's long-lived 404
    cache. Wrong key, option id, or value shape → ``ValueError``; fail-closed
    otherwise.
    """
    if not custom_fields:
        return
    await _check_work_item_custom_keys(
        client, project_id, work_item_type, custom_fields
    )
    await check_custom_field_enum_values(
        client, project_id, "workitems", work_item_type, custom_fields
    )


async def resolve_work_item_types(
    client: PolarionClient,
    project_id: str,
    work_item_ids: Iterable[str],
) -> dict[str, str]:
    """Existence check plus short id -> type map for a bulk batch, via chunked
    ``id:(...)`` queries (enum guards scope options by type). Fail-closed:
    raises ``ValueError`` naming every missing id before any write.
    """
    requested = sorted({wi for wi in work_item_ids if wi})
    if not requested:
        return {}

    resolved: dict[str, str] = {}
    path = f"/projects/{encode_path_segment(project_id)}/workitems"
    for start in range(0, len(requested), GUARD_PAGE_SIZE):
        chunk = requested[start : start + GUARD_PAGE_SIZE]
        params: dict[str, str | int] = {
            "query": f"id:({' '.join(chunk)})",
            "fields[workitems]": "id,type",
            "page[size]": GUARD_PAGE_SIZE,
            "page[number]": 1,
        }
        try:
            response = await guarded_get(
                client,
                path,
                params,
                what="work item existence",
                project_id=project_id,
                propagate_not_found=True,
            )
        except PolarionNotFoundError as exc:
            raise ValueError(
                f"Project '{project_id}' not found. Use `list_projects` to "
                "discover valid project IDs."
            ) from exc
        data = response.get("data", [])
        if not isinstance(data, list):
            continue
        for entry in data:
            if not isinstance(entry, dict):
                continue
            attributes = entry.get("attributes")
            resolved[extract_short_id(safe_str(entry.get("id", "")))] = (
                safe_str(attributes.get("type", ""))
                if isinstance(attributes, dict)
                else ""
            )

    missing = sorted(set(requested) - resolved.keys())
    if missing:
        raise ValueError(
            f"Work item(s) {format_option_list(missing)} not found in project "
            f"'{project_id}'. Use `list_work_items` to discover valid IDs."
        )
    return resolved
