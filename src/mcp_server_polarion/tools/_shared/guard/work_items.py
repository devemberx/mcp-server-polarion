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
from mcp_server_polarion.tools._shared.guard._common import (
    _GUARD_PAGE_SIZE,
    _custom_keys_from_data_list,
    _reject_unknown_custom_keys,
)
from mcp_server_polarion.tools._shared.guard._errors import (
    _unauthorized_write_block,
    _unreachable_write_block,
)
from mcp_server_polarion.tools._shared.guard.enums import (
    _check_custom_field_enum_values,
    _check_enum,
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
        await _check_enum(client, project_id, "workitems", "type", "~", type)
    if status is not None and status != "":
        await _check_enum(
            client, project_id, "workitems", "status", work_item_type, status
        )
    if severity is not None and severity != "":
        await _check_enum(
            client, project_id, "workitems", "severity", work_item_type, severity
        )
    if priority is not None and priority != "":
        await _check_enum(
            client, project_id, "workitems", "priority", work_item_type, priority
        )
    if resolution is not None and resolution != "":
        await _check_enum(
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
        "page[size]": _GUARD_PAGE_SIZE,
    }
    keys: set[str] = set()
    page_number = 1
    while True:
        try:
            response = await client.get(
                path, params={**base_params, "page[number]": page_number}
            )
        except PolarionAuthError as exc:
            raise _unauthorized_write_block("custom_fields keys", project_id) from exc
        except PolarionError as exc:
            raise _unreachable_write_block(
                "custom_fields keys", project_id, exc
            ) from exc
        data = response.get("data", [])
        if not isinstance(data, list):
            break
        keys.update(
            _custom_keys_from_data_list(response, STANDARD_WORK_ITEM_ATTRIBUTES)
        )
        if len(data) < _GUARD_PAGE_SIZE:
            break
        page_number += 1

    result = frozenset(keys)
    store_work_item_custom_keys(project_id, type_id, result)
    return result


async def _check_work_item_custom_keys(
    client: PolarionClient,
    project_id: str,
    work_item_type: str,
    custom_fields: dict[str, object],
) -> None:
    """Reject ``custom_fields`` keys absent from the type's sampled schema.

    Unknown key vs *cached* schema forces one fresh re-fetch before rejecting;
    empty schema fails closed (ghost write unrecoverable).
    """
    schema = get_work_item_custom_keys(project_id, work_item_type)
    fetched_fresh = schema is None
    if schema is None:
        schema = await _fetch_work_item_type_custom_keys(
            client, project_id, work_item_type
        )

    if all(key in schema for key in custom_fields):
        return

    # Unknown key may be admin-added since caching; refetch once before rejecting.
    if not fetched_fresh:
        invalidate_work_item_custom_keys(project_id, work_item_type)
        schema = await _fetch_work_item_type_custom_keys(
            client, project_id, work_item_type
        )

    if not schema:
        raise RuntimeError(
            f"Cannot verify custom_fields {format_option_list(custom_fields)} for "
            f"work_item_type '{work_item_type}' in project '{project_id}': no existing "
            f"item of this type has custom fields populated, so the schema can't be "
            f"sampled. Refusing the write -- an unknown key ghosts silently (invisible "
            f"to UI/Lucene). Do not create or edit items to work around this; ask the "
            f"user to confirm these custom-field ids exist for this type."
        )

    _reject_unknown_custom_keys(
        custom_fields,
        schema,
        scope=f"work_item_type '{work_item_type}'",
        discovery_tool="sample of existing items",
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
    await _check_custom_field_enum_values(
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
    for start in range(0, len(requested), _GUARD_PAGE_SIZE):
        chunk = requested[start : start + _GUARD_PAGE_SIZE]
        params: dict[str, str | int] = {
            "query": f"id:({' '.join(chunk)})",
            "fields[workitems]": "id,type",
            "page[size]": _GUARD_PAGE_SIZE,
            "page[number]": 1,
        }
        try:
            response = await client.get(path, params=params)
        except PolarionNotFoundError as exc:
            raise ValueError(
                f"Project '{project_id}' not found. Use `list_projects` to "
                "discover valid project IDs."
            ) from exc
        except PolarionAuthError as exc:
            raise _unauthorized_write_block("work item existence", project_id) from exc
        except PolarionError as exc:
            raise _unreachable_write_block(
                "work item existence", project_id, exc
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
