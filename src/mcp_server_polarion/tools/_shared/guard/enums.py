"""Enum option fetch + validation: ``getAvailableOptions`` (work items /
documents) and project-level enumerations (roles, testrun enums).
"""

from __future__ import annotations

import logging
from collections.abc import Iterable

from mcp_server_polarion.core.client import PolarionClient
from mcp_server_polarion.core.exceptions import (
    PolarionAuthError,
    PolarionError,
    PolarionNotFoundError,
)
from mcp_server_polarion.tools._shared.cache import (
    Resource,
    get_cached_enum_options,
    get_cached_project_enum,
    store_cached_enum_options,
    store_cached_project_enum,
)
from mcp_server_polarion.tools._shared.guard._common import GUARD_PAGE_SIZE
from mcp_server_polarion.tools._shared.guard._errors import (
    unauthorized_write_block,
    unreachable_write_block,
)
from mcp_server_polarion.tools._shared.helpers import (
    encode_path_segment,
    format_option_list,
)

logger = logging.getLogger("mcp_server_polarion.tools._shared.guard.enums")


_ENUM_DISCOVERY_TOOL: dict[Resource, str] = {
    "workitems": "list_work_item_enum_options",
    "documents": "list_document_enum_options",
}


async def fetch_enum_option_ids(
    client: PolarionClient,
    project_id: str,
    resource: Resource,
    field_id: str,
    type_id: str,
) -> frozenset[str]:
    """Valid option ids for ``(project, resource, field, type)``; cached,
    fail-closed, 404 defers (empty set).
    """
    cached = get_cached_enum_options(project_id, resource, field_id, type_id)
    if cached is not None:
        return cached

    path = (
        f"/projects/{encode_path_segment(project_id)}"
        f"/{resource}/fields/{encode_path_segment(field_id)}"
        "/actions/getAvailableOptions"
    )
    params: dict[str, str | int] = {
        "type": type_id,
        "page[size]": GUARD_PAGE_SIZE,
        "page[number]": 1,
    }
    try:
        response = await client.get(path, params=params)
    except PolarionNotFoundError:
        # 404 = non-enum field or endpoint absent; cache empty set (long TTL —
        # stale worst case is the same deferral).
        logger.warning(
            "getAvailableOptions returned 404 for field=%s (resource=%s, "
            "project=%s); skipping enum validation for this field -- the "
            "endpoint or field is unsupported here, so there is nothing to "
            "validate against.",
            field_id,
            resource,
            project_id,
        )
        store_cached_enum_options(
            project_id, resource, field_id, type_id, frozenset(), not_found=True
        )
        return frozenset()
    except PolarionAuthError as exc:
        raise unauthorized_write_block(f"{field_id} options", project_id) from exc
    except PolarionError as exc:
        raise unreachable_write_block(f"{field_id} options", project_id, exc) from exc

    data = response.get("data", [])
    ids: set[str] = set()
    if isinstance(data, list):
        for entry in data:
            if not isinstance(entry, dict):
                continue
            opt_id = entry.get("id")
            if isinstance(opt_id, str) and opt_id:
                ids.add(opt_id)

    option_ids = frozenset(ids)
    store_cached_enum_options(project_id, resource, field_id, type_id, option_ids)
    return option_ids


async def check_enum(  # noqa: PLR0913
    client: PolarionClient,
    project_id: str,
    resource: Resource,
    field_id: str,
    type_id: str,
    value: str,
) -> None:
    option_ids = await fetch_enum_option_ids(
        client, project_id, resource, field_id, type_id
    )
    # Empty set = successful no-options fetch; defer rather than false-positive.
    if not option_ids or value in option_ids:
        return
    raise ValueError(
        f"{field_id}='{value}' is not a valid {field_id} option in "
        f"project '{project_id}' for {resource} type '{type_id}'. "
        f"Valid options: {format_option_list(option_ids)}. "
        f"Unknown ids ghost silently (never match Lucene) -- call "
        f"{_ENUM_DISCOVERY_TOOL[resource]} first."
    )


async def fetch_project_enum_option_ids(
    client: PolarionClient,
    project_id: str,
    enum_name: str,
    context: str = "~",
) -> frozenset[str]:
    """Valid option ids for a project-level enum not in ``getAvailableOptions``
    (link/hyperlink role, testrun type/status). ``context`` is the enumeration
    context path segment (``testing`` for testrun enums; ``~`` does NOT
    resolve them). Response ``data`` is a dict (not list), options at
    ``data.attributes.options[].id``. Cached; fail-closed like
    :func:`fetch_enum_option_ids`.
    """
    cache_key = f"{context}/{enum_name}"
    cached = get_cached_project_enum(project_id, cache_key)
    if cached is not None:
        return cached

    path = (
        f"/projects/{encode_path_segment(project_id)}"
        f"/enumerations/{encode_path_segment(context)}"
        f"/{encode_path_segment(enum_name)}/~"
    )
    try:
        response = await client.get(path, params={"fields[enumerations]": "@all"})
    except PolarionNotFoundError:
        logger.warning(
            "enumeration '%s' returned 404 for project=%s; skipping role "
            "validation -- the enumeration is unsupported here, so there is "
            "nothing to validate against.",
            enum_name,
            project_id,
        )
        store_cached_project_enum(project_id, cache_key, frozenset())
        return frozenset()
    except PolarionAuthError as exc:
        raise unauthorized_write_block(f"{enum_name} options", project_id) from exc
    except PolarionError as exc:
        raise unreachable_write_block(f"{enum_name} options", project_id, exc) from exc

    ids: set[str] = set()
    data = response.get("data", {})
    if isinstance(data, dict):
        attributes = data.get("attributes")
        options = attributes.get("options") if isinstance(attributes, dict) else None
        if isinstance(options, list):
            for entry in options:
                if not isinstance(entry, dict):
                    continue
                opt_id = entry.get("id")
                if isinstance(opt_id, str) and opt_id:
                    ids.add(opt_id)

    option_ids = frozenset(ids)
    store_cached_project_enum(project_id, cache_key, option_ids)
    return option_ids


async def check_project_enum_roles(  # noqa: PLR0913
    client: PolarionClient,
    project_id: str,
    enum_name: str,
    roles: Iterable[str],
    *,
    field_label: str,
    discovery_hint: str,
    context: str = "~",
) -> None:
    requested = {role for role in roles if role}
    if not requested:
        return

    option_ids = await fetch_project_enum_option_ids(
        client, project_id, enum_name, context
    )
    # Empty set = no options / enum unsupported; defer.
    if not option_ids:
        return

    unknown = sorted(requested - option_ids)
    if unknown:
        raise ValueError(
            f"{field_label} id(s) {unknown} are not valid in project "
            f"'{project_id}'. Valid options: {format_option_list(option_ids)}. "
            f"An unknown {field_label} ghosts silently (never matches Lucene) "
            f"-- {discovery_hint}"
        )


def _bad_custom_enum_value(  # noqa: PLR0913
    field_id: str,
    value: object,
    option_ids: frozenset[str],
    project_id: str,
    resource: Resource,
    type_id: str,
    *,
    shape: bool = False,
) -> ValueError:
    problem = (
        f"custom_fields['{field_id}'] is an enumeration field but got "
        f"{type(value).__name__} {value!r} -- enum values are option-id "
        f"strings (or lists of them)"
        if shape
        else f"custom_fields['{field_id}']={value!r} is not a valid option"
    )
    return ValueError(
        f"{problem} in project '{project_id}' for {resource} type '{type_id}'. "
        f"Valid options: {format_option_list(option_ids)}. "
        f"Unknown enum values ghost silently (invisible to UI/Lucene) -- call "
        f"{_ENUM_DISCOVERY_TOOL[resource]} first."
    )


async def check_custom_field_enum_values(
    client: PolarionClient,
    project_id: str,
    resource: Resource,
    type_id: str,
    custom_fields: dict[str, object],
) -> None:
    """Validate enum-typed ``custom_fields`` values against ``getAvailableOptions``.

    Non-empty option set proves the field is an enum (the endpoint is the only
    API mapping key → options) → value must be an option-id string or list of
    them; empty set defers. Arity unchecked — endpoint can't distinguish
    single/multi-enum, but wrong arity 400s loudly at Polarion, so only wrong
    option-id strings ghost.
    """
    for field_id in sorted(custom_fields):
        value = custom_fields[field_id]
        # Payload builders drop empty values — nothing to validate, skip probe.
        if value is None or value in ("", []):
            continue
        option_ids = await fetch_enum_option_ids(
            client, project_id, resource, field_id, type_id
        )
        if not option_ids:
            continue
        if isinstance(value, str):
            if value not in option_ids:
                raise _bad_custom_enum_value(
                    field_id, value, option_ids, project_id, resource, type_id
                )
        elif isinstance(value, list):
            for element in value:
                if not isinstance(element, str):
                    raise _bad_custom_enum_value(
                        field_id,
                        element,
                        option_ids,
                        project_id,
                        resource,
                        type_id,
                        shape=True,
                    )
                if element not in option_ids:
                    raise _bad_custom_enum_value(
                        field_id, element, option_ids, project_id, resource, type_id
                    )
        elif value is not None:
            raise _bad_custom_enum_value(
                field_id, value, option_ids, project_id, resource, type_id, shape=True
            )
