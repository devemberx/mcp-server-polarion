"""Enum option fetch + validation: ``getAvailableOptions`` (work items /
documents) and project-level enumerations (roles, testrun enums).

Naming: ``field_*`` = ``getAvailableOptions``, scoped per field + work item
type; ``enum_*`` = ``/projects/{p}/enumerations/``, scoped per enum name.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Iterable, Mapping
from functools import partial

from mcp_server_polarion.core.client import PolarionClient
from mcp_server_polarion.core.exceptions import PolarionNotFoundError
from mcp_server_polarion.tools._shared.cache import (
    Resource,
    get_cached_enum_option_ids,
    get_cached_field_options,
    invalidate_enum_option_ids,
    invalidate_field_options,
    store_cached_enum_option_ids,
    store_cached_field_options,
)
from mcp_server_polarion.tools._shared.guard._http import (
    GUARD_PAGE_SIZE,
    guarded_get,
)
from mcp_server_polarion.tools._shared.guard._revalidate import resolve_with_refetch
from mcp_server_polarion.tools._shared.helpers import (
    encode_path_segment,
    format_option_list,
)

logger = logging.getLogger("mcp_server_polarion.tools._shared.guard.enums")


_FIELD_DISCOVERY_TOOL: dict[Resource, str] = {
    "workitems": "list_work_item_enum_options",
    "documents": "list_document_enum_options",
}


async def fetch_field_options(
    client: PolarionClient,
    project_id: str,
    resource: Resource,
    field_id: str,
    type_id: str,
) -> Mapping[str, str]:
    """Option id → display name for ``(project, resource, field, type)``;
    cached, fail-closed, 404 defer (empty mapping).

    Name = what the portal show for the option (work item type ``testcase``
    → ``Test Case``). Option served without one map to ``""``.
    """
    cached = get_cached_field_options(project_id, resource, field_id, type_id)
    if cached is not None:
        return cached
    return await _fetch_field_options_uncached(
        client, project_id, resource, field_id, type_id
    )


async def _fetch_field_options_uncached(
    client: PolarionClient,
    project_id: str,
    resource: Resource,
    field_id: str,
    type_id: str,
) -> Mapping[str, str]:
    """Request + parse + store, bypassing any cached entry — refetch seam for
    :func:`resolve_with_refetch`.
    """
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
        response = await guarded_get(
            client, path, params, what=f"{field_id} options", project_id=project_id
        )
    except PolarionNotFoundError:
        # 404 = non-enum field or endpoint absent; cache empty set (long TTL —
        # stale worst case = same deferral).
        logger.warning(
            "getAvailableOptions returned 404 for field=%s (resource=%s, "
            "project=%s); skipping enum validation for this field -- the "
            "endpoint or field is unsupported here, so there is nothing to "
            "validate against.",
            field_id,
            resource,
            project_id,
        )
        store_cached_field_options(
            project_id, resource, field_id, type_id, {}, not_found=True
        )
        return {}

    data = response.get("data", [])
    options: dict[str, str] = {}
    if isinstance(data, list):
        for entry in data:
            if not isinstance(entry, dict):
                continue
            opt_id = entry.get("id")
            if isinstance(opt_id, str) and opt_id:
                name = entry.get("name")
                options[opt_id] = name if isinstance(name, str) else ""

    store_cached_field_options(project_id, resource, field_id, type_id, options)
    return options


async def _options_for_check(  # noqa: PLR0913
    client: PolarionClient,
    project_id: str,
    resource: Resource,
    field_id: str,
    type_id: str,
    accepts: Callable[[Mapping[str, str]], bool],
) -> Mapping[str, str]:
    """Option set to judge against: stale cache refetch once before it may
    reject, so admin-added option never block a legitimate write.
    """
    return await resolve_with_refetch(
        get_cached=lambda: get_cached_field_options(
            project_id, resource, field_id, type_id
        ),
        invalidate=lambda: invalidate_field_options(
            project_id, resource, field_id, type_id
        ),
        fetch=lambda: _fetch_field_options_uncached(
            client, project_id, resource, field_id, type_id
        ),
        accepts=accepts,
    )


async def check_field_value(  # noqa: PLR0913
    client: PolarionClient,
    project_id: str,
    resource: Resource,
    field_id: str,
    type_id: str,
    value: str,
) -> None:
    # Empty mapping = successful no-options fetch; defer rather than false-positive.
    options = await _options_for_check(
        client,
        project_id,
        resource,
        field_id,
        type_id,
        lambda known: not known or value in known,
    )
    if not options or value in options:
        return
    raise ValueError(
        f"{field_id}='{value}' is not a valid {field_id} option in "
        f"project '{project_id}' for {resource} type '{type_id}'. "
        f"Valid options: {format_option_list(options.keys())}. "
        f"Unknown ids ghost silently (never match Lucene) -- call "
        f"{_FIELD_DISCOVERY_TOOL[resource]} first."
    )


async def fetch_enum_option_ids(
    client: PolarionClient,
    project_id: str,
    enum_name: str,
    context: str = "~",
) -> frozenset[str]:
    """Valid option ids for project-level enum not in ``getAvailableOptions``
    (link/hyperlink role, testrun type/status). ``context`` = enumeration
    context path segment (``testing`` for testrun enums; ``~`` does NOT
    resolve them). Response ``data`` = dict (not list), options at
    ``data.attributes.options[].id``. Cached for the default guard TTL
    (404 included); fail-closed.
    """
    cached = get_cached_enum_option_ids(project_id, _enum_cache_key(context, enum_name))
    if cached is not None:
        return cached
    return await _fetch_enum_option_ids_uncached(client, project_id, enum_name, context)


def _enum_cache_key(context: str, enum_name: str) -> str:
    """Cache key folding both path segments — same enum name resolve to
    different option sets per context.
    """
    return f"{context}/{enum_name}"


async def _fetch_enum_option_ids_uncached(
    client: PolarionClient,
    project_id: str,
    enum_name: str,
    context: str,
) -> frozenset[str]:
    """Request + parse + store, bypassing any cached entry — refetch seam for
    :func:`resolve_with_refetch`.
    """
    cache_key = _enum_cache_key(context, enum_name)
    path = (
        f"/projects/{encode_path_segment(project_id)}"
        f"/enumerations/{encode_path_segment(context)}"
        f"/{encode_path_segment(enum_name)}/~"
    )
    try:
        response = await guarded_get(
            client,
            path,
            {"fields[enumerations]": "@all"},
            what=f"{enum_name} options",
            project_id=project_id,
        )
    except PolarionNotFoundError:
        logger.warning(
            "enumeration '%s' returned 404 for project=%s; skipping "
            "validation against it -- the enumeration is unsupported here, so "
            "there is nothing to validate against.",
            enum_name,
            project_id,
        )
        store_cached_enum_option_ids(project_id, cache_key, frozenset())
        return frozenset()

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
    store_cached_enum_option_ids(project_id, cache_key, option_ids)
    return option_ids


async def check_enum_values(  # noqa: PLR0913
    client: PolarionClient,
    project_id: str,
    enum_name: str,
    values: Iterable[str],
    *,
    field_label: str,
    discovery_hint: str,
    context: str = "~",
) -> None:
    requested = {value for value in values if value}
    if not requested:
        return

    cache_key = _enum_cache_key(context, enum_name)
    option_ids = await resolve_with_refetch(
        get_cached=lambda: get_cached_enum_option_ids(project_id, cache_key),
        invalidate=lambda: invalidate_enum_option_ids(project_id, cache_key),
        fetch=lambda: _fetch_enum_option_ids_uncached(
            client, project_id, enum_name, context
        ),
        accepts=lambda known: not known or requested <= known,
    )
    # Empty set = no options / enum unsupported; defer.
    if not option_ids:
        return

    unknown = sorted(requested - option_ids)
    if unknown:
        raise ValueError(
            f"{field_label} id(s) {format_option_list(unknown)} are not valid "
            f"in project '{project_id}'. "
            f"Valid options: {format_option_list(option_ids)}. "
            f"An unknown {field_label} ghosts silently (never matches Lucene) "
            f"-- {discovery_hint}"
        )


def _bad_custom_enum_value(  # noqa: PLR0913
    field_id: str,
    value: object,
    options: Mapping[str, str],
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
        f"Valid options: {format_option_list(options.keys())}. "
        f"Unknown enum values ghost silently (invisible to UI/Lucene) -- call "
        f"{_FIELD_DISCOVERY_TOOL[resource]} first."
    )


def _custom_value_known(value: object, options: Mapping[str, str]) -> bool:
    """Whether *value* already satisfy *options*. Shape error count as known —
    refetch cannot turn a non-string into a valid option id.
    """
    if not options:
        return True
    if isinstance(value, str):
        return value in options
    if isinstance(value, list):
        return all(element in options for element in value if isinstance(element, str))
    return True


async def check_custom_field_enum_values(
    client: PolarionClient,
    project_id: str,
    resource: Resource,
    type_id: str,
    custom_fields: dict[str, object],
) -> None:
    """Validate enum-typed ``custom_fields`` values against ``getAvailableOptions``.

    Non-empty option set prove field is enum (endpoint = only API mapping
    key → options) → value must be option-id string or list of them; empty
    set defer. Arity unchecked — endpoint can't distinguish single/multi-enum,
    wrong arity 400 loudly at Polarion, only wrong option-id strings ghost.
    """
    for field_id in sorted(custom_fields):
        value = custom_fields[field_id]
        # Payload builders drop empty values — nothing to validate, skip probe.
        if value is None or value in ("", []):
            continue
        options = await _options_for_check(
            client,
            project_id,
            resource,
            field_id,
            type_id,
            partial(_custom_value_known, value),
        )
        if not options:
            continue
        if isinstance(value, str):
            if value not in options:
                raise _bad_custom_enum_value(
                    field_id, value, options, project_id, resource, type_id
                )
        elif isinstance(value, list):
            for element in value:
                if not isinstance(element, str):
                    raise _bad_custom_enum_value(
                        field_id,
                        element,
                        options,
                        project_id,
                        resource,
                        type_id,
                        shape=True,
                    )
                if element not in options:
                    raise _bad_custom_enum_value(
                        field_id, element, options, project_id, resource, type_id
                    )
        elif value is not None:
            raise _bad_custom_enum_value(
                field_id, value, options, project_id, resource, type_id, shape=True
            )
