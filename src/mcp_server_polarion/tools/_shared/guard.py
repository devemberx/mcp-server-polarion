"""Pre-write guards: Polarion persists unknown enum ids / custom-field keys as
silent ghosts (HTTP 200, invisible to UI and Lucene), so each guard fetches the
real options and raises before the write. Fail-closed — validation error blocks
the write (auth → ``PermissionError``, else ``RuntimeError``); only a
*successful* empty option set and a 404 defer to Polarion. Caching in
:mod:`...tools._shared.cache`.
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
from mcp_server_polarion.models import WorkItemLinkSpec
from mcp_server_polarion.tools._shared.cache import (
    Resource,
    get_cached_enum_options,
    get_cached_project_enum,
    get_document_type_custom_keys,
    get_test_run_custom_keys,
    get_work_item_custom_keys,
    invalidate_document_type_custom_keys,
    invalidate_test_run_custom_keys,
    invalidate_work_item_custom_keys,
    store_cached_enum_options,
    store_cached_project_enum,
    store_document_type_custom_keys,
    store_test_run_custom_keys,
    store_work_item_custom_keys,
)
from mcp_server_polarion.tools._shared.custom_fields import (
    STANDARD_DOCUMENT_ATTRIBUTES,
    STANDARD_TEST_RUN_ATTRIBUTES,
    STANDARD_WORK_ITEM_ATTRIBUTES,
)
from mcp_server_polarion.tools._shared.fields import (
    DOCUMENT_DETAIL_FIELDS,
    WORK_ITEM_DETAIL_FIELDS,
)
from mcp_server_polarion.tools._shared.helpers import (
    encode_path_segment,
    format_option_list,
    safe_str,
)
from mcp_server_polarion.tools._shared.parse import extract_short_id
from mcp_server_polarion.tools._shared.sql import (
    one_heading_per_document_sql,
    one_item_per_custom_field_sql,
)

logger = logging.getLogger("mcp_server_polarion.tools._shared.guard")

_GUARD_PAGE_SIZE: int = 100

_ENUM_DISCOVERY_TOOL: dict[Resource, str] = {
    "workitems": "list_work_item_enum_options",
    "documents": "list_document_enum_options",
}


def _unreachable_write_block(
    what: str, project_id: str, exc: PolarionError
) -> RuntimeError:
    logger.warning(
        "guard blocking write: could not validate %s for project=%s (%s)",
        what,
        project_id,
        exc.message,
    )
    return RuntimeError(
        f"Cannot validate {what} for project '{project_id}': validation request "
        f"failed ({exc.message}). Refusing the write -- unknown ids/keys persist "
        f"as silent ghosts (invisible to UI/Lucene, never error). Retry once "
        f"Polarion is reachable."
    )


def _unauthorized_write_block(what: str, project_id: str) -> PermissionError:
    """Mirrors the tool layer's ``PolarionAuthError -> PermissionError``
    (fixable token scope, not a backend to retry).
    """
    logger.warning(
        "guard blocking write: not authorized to validate %s for project=%s",
        what,
        project_id,
    )
    return PermissionError(
        f"Cannot validate {what} for project '{project_id}': POLARION_TOKEN lacks "
        f"permission for the validation request. Refusing the write -- check the "
        f"token's permissions."
    )


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
        "page[size]": _GUARD_PAGE_SIZE,
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
        raise _unauthorized_write_block(f"{field_id} options", project_id) from exc
    except PolarionError as exc:
        raise _unreachable_write_block(f"{field_id} options", project_id, exc) from exc

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


async def _check_enum(  # noqa: PLR0913
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
        raise _unauthorized_write_block(f"{enum_name} options", project_id) from exc
    except PolarionError as exc:
        raise _unreachable_write_block(f"{enum_name} options", project_id, exc) from exc

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


async def _check_project_enum_roles(  # noqa: PLR0913
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


async def _check_custom_field_enum_values(
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


def _reject_unknown_custom_keys(
    custom_fields: dict[str, object],
    known: frozenset[str],
    *,
    scope: str,
    discovery_tool: str,
) -> None:
    unknown = sorted(k for k in custom_fields if k not in known)
    if unknown:
        raise ValueError(
            f"custom_fields key(s) {unknown} were not present in any prior "
            f"{discovery_tool} for {scope}. Known keys: {format_option_list(known)}. "
            f"Unknown keys persist as silent ghost attributes -- fetch a sample "
            f"first to discover the project's real custom-field ids."
        )


def _custom_keys_from_data_list(
    response: dict[str, object], allowlist: frozenset[str]
) -> frozenset[str]:
    keys: set[str] = set()
    data = response.get("data", [])
    if isinstance(data, list):
        for entry in data:
            if not isinstance(entry, dict):
                continue
            attrs = entry.get("attributes")
            if isinstance(attrs, dict):
                keys.update(
                    k for k in attrs if isinstance(k, str) and k not in allowlist
                )
    return frozenset(keys)


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


async def guard_document_enums(
    client: PolarionClient,
    project_id: str,
    document_type: str,
    *,
    type: str | None = None,
    status: str | None = None,
) -> None:
    """Validate every supplied document enum arg against ``getAvailableOptions``."""
    if type is not None and type != "":
        await _check_enum(client, project_id, "documents", "type", "~", type)
    if status is not None and status != "":
        await _check_enum(
            client, project_id, "documents", "status", document_type, status
        )


async def _fetch_document_type_custom_keys(
    client: PolarionClient,
    project_id: str,
    document_type: str,
) -> frozenset[str]:
    """Sample the project's documents and return *document_type*'s key schema.

    Heading-discovery SQL + ``include=module`` surfaces each document's type and
    inline customs — works on every build, unlike ``GET /documents``. All types'
    schemas are stored (later writes hit cache); target type stored even when
    empty so a no-customs type fails closed without re-probing. Headingless
    documents are invisible to this sample.
    """
    path = f"/projects/{encode_path_segment(project_id)}/workitems"
    base_params: dict[str, str | int] = {
        "query": one_heading_per_document_sql(project_id),
        "include": "module",
        "fields[workitems]": "module",
        "fields[documents]": DOCUMENT_DETAIL_FIELDS,
        "page[size]": _GUARD_PAGE_SIZE,
    }
    by_type: dict[str, set[str]] = {}
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
        included = response.get("included", [])
        if isinstance(included, list):
            for entry in included:
                if not isinstance(entry, dict) or entry.get("type") != "documents":
                    continue
                attrs = entry.get("attributes")
                if not isinstance(attrs, dict):
                    continue
                dtype = attrs.get("type")
                if not isinstance(dtype, str) or not dtype:
                    continue
                keys = by_type.setdefault(dtype, set())
                keys.update(
                    k
                    for k in attrs
                    if isinstance(k, str) and k not in STANDARD_DOCUMENT_ATTRIBUTES
                )
        if len(data) < _GUARD_PAGE_SIZE:
            break
        page_number += 1

    by_type.setdefault(document_type, set())
    for dtype, keys in by_type.items():
        store_document_type_custom_keys(project_id, dtype, frozenset(keys))
    return frozenset(by_type[document_type])


async def _check_document_custom_keys(
    client: PolarionClient,
    project_id: str,
    document_type: str,
    custom_fields: dict[str, object],
) -> None:
    """Document-axis mirror of :func:`_check_work_item_custom_keys`."""
    schema = get_document_type_custom_keys(project_id, document_type)
    fetched_fresh = schema is None
    if schema is None:
        schema = await _fetch_document_type_custom_keys(
            client, project_id, document_type
        )

    if all(key in schema for key in custom_fields):
        return

    # Unknown key may be admin-added since caching; refetch once before rejecting.
    if not fetched_fresh:
        invalidate_document_type_custom_keys(project_id, document_type)
        schema = await _fetch_document_type_custom_keys(
            client, project_id, document_type
        )

    if not schema:
        raise RuntimeError(
            f"Cannot verify custom_fields {format_option_list(custom_fields)} for "
            f"document type '{document_type}' in project '{project_id}': no existing "
            f"document of this type has custom fields populated, so the schema can't "
            f"be sampled. Refusing the write -- an unknown key ghosts silently "
            f"(invisible to UI/Lucene). Do not create or edit documents to work around "
            f"this; ask the user to confirm these custom-field ids exist for this type."
        )

    _reject_unknown_custom_keys(
        custom_fields,
        schema,
        scope=f"document type '{document_type}'",
        discovery_tool="sample of existing documents",
    )


async def guard_document_custom_fields(
    client: PolarionClient,
    project_id: str,
    document_type: str,
    custom_fields: dict[str, object],
) -> None:
    """Document-axis mirror of :func:`guard_work_item_custom_fields`."""
    if not custom_fields:
        return
    await _check_document_custom_keys(client, project_id, document_type, custom_fields)
    await _check_custom_field_enum_values(
        client, project_id, "documents", document_type, custom_fields
    )


async def guard_test_run_enums(
    client: PolarionClient,
    project_id: str,
    *,
    type: str | None = None,
    status: str | None = None,
) -> None:
    """Validate test-run ``type``/``status`` against the ``testing``-context
    project enumerations — testruns have no ``getAvailableOptions`` endpoint,
    and the ``~`` wildcard context does not resolve these enums.
    """
    await _check_project_enum_roles(
        client,
        project_id,
        "testrun-type",
        [type] if type else [],
        field_label="test run type",
        discovery_hint="use list_test_runs to see values in use.",
        context="testing",
    )
    await _check_project_enum_roles(
        client,
        project_id,
        "testrun-status",
        [status] if status else [],
        field_label="test run status",
        discovery_hint="use list_test_runs to see values in use.",
        context="testing",
    )


async def guard_test_run_templates(
    client: PolarionClient,
    project_id: str,
    template_ids: Iterable[str],
) -> None:
    """Resolve each template id before the write — Polarion doesn't validate
    relationship targets. Run instances are rejected too: ``isTemplate`` is
    served only (as ``true``) on templates, absent on instances.
    """
    for template_id in sorted({t for t in template_ids if t}):
        path = (
            f"/projects/{encode_path_segment(project_id)}"
            f"/testruns/{encode_path_segment(template_id)}"
        )
        try:
            response = await client.get(
                path, params={"fields[testruns]": "id,isTemplate"}
            )
        except PolarionNotFoundError as exc:
            raise ValueError(
                f"Test run template '{template_id}' not found in project "
                f"'{project_id}'. Use list_test_runs(templates=True) to "
                f"discover template ids."
            ) from exc
        except PolarionAuthError as exc:
            raise _unauthorized_write_block("test run templates", project_id) from exc
        except PolarionError as exc:
            raise _unreachable_write_block(
                "test run templates", project_id, exc
            ) from exc

        data = response.get("data", {})
        attributes = data.get("attributes") if isinstance(data, dict) else None
        is_template = (
            attributes.get("isTemplate") if isinstance(attributes, dict) else None
        )
        if is_template is not True:
            raise ValueError(
                f"Test run '{template_id}' in project '{project_id}' is a run "
                f"instance, not a template. Use list_test_runs(templates=True) "
                f"to discover template ids."
            )


async def _fetch_test_run_custom_keys(
    client: PolarionClient,
    project_id: str,
) -> frozenset[str]:
    """Union of custom-field keys sampled from the project's runs and
    templates — testrun custom fields are project config (no type axis, no
    SQL needed). Cached even if empty.
    """
    path = f"/projects/{encode_path_segment(project_id)}/testruns"
    keys: set[str] = set()
    for templates in (False, True):
        base_params: dict[str, str | int] = {
            "fields[testruns]": "@all",
            "page[size]": _GUARD_PAGE_SIZE,
        }
        if templates:
            base_params["templates"] = "true"
        page_number = 1
        while True:
            try:
                response = await client.get(
                    path, params={**base_params, "page[number]": page_number}
                )
            except PolarionAuthError as exc:
                raise _unauthorized_write_block(
                    "custom_fields keys", project_id
                ) from exc
            except PolarionError as exc:
                raise _unreachable_write_block(
                    "custom_fields keys", project_id, exc
                ) from exc
            data = response.get("data", [])
            if not isinstance(data, list):
                break
            keys.update(
                _custom_keys_from_data_list(response, STANDARD_TEST_RUN_ATTRIBUTES)
            )
            if len(data) < _GUARD_PAGE_SIZE:
                break
            page_number += 1

    result = frozenset(keys)
    store_test_run_custom_keys(project_id, result)
    return result


async def _check_test_run_custom_keys(
    client: PolarionClient,
    project_id: str,
    custom_fields: dict[str, object],
) -> None:
    """Test-run mirror of :func:`_check_work_item_custom_keys` (project scope)."""
    schema = get_test_run_custom_keys(project_id)
    fetched_fresh = schema is None
    if schema is None:
        schema = await _fetch_test_run_custom_keys(client, project_id)

    if all(key in schema for key in custom_fields):
        return

    # Unknown key may be admin-added since caching; refetch once before rejecting.
    if not fetched_fresh:
        invalidate_test_run_custom_keys(project_id)
        schema = await _fetch_test_run_custom_keys(client, project_id)

    if not schema:
        raise RuntimeError(
            f"Cannot verify custom_fields {format_option_list(custom_fields)} for "
            f"test runs in project '{project_id}': no existing test run has custom "
            f"fields populated, so the schema can't be sampled. Refusing the write "
            f"-- an unknown key ghosts silently (invisible to UI/Lucene). Do not "
            f"create runs to work around this; ask the user to confirm these "
            f"custom-field ids exist for test runs."
        )

    _reject_unknown_custom_keys(
        custom_fields,
        schema,
        scope=f"test runs in project '{project_id}'",
        discovery_tool="sample of existing runs",
    )


async def guard_test_run_custom_fields(
    client: PolarionClient,
    project_id: str,
    custom_fields: dict[str, object],
) -> None:
    """Validate ``custom_fields`` keys before a test-run write. Keys only —
    testruns have no ``getAvailableOptions``, so enum-typed *values* defer to
    Polarion (wrong option ids there ghost; keys are the guardable axis).
    """
    if not custom_fields:
        return
    await _check_test_run_custom_keys(client, project_id, custom_fields)


async def guard_work_item_link_roles(
    client: PolarionClient,
    project_id: str,
    roles: Iterable[str],
) -> None:
    """Reject link roles not in ``workitem-link-role`` — an unknown role stores
    verbatim (HTTP 201) as a ghost link.
    """
    await _check_project_enum_roles(
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
    await _check_project_enum_roles(
        client,
        project_id,
        "hyperlink-role",
        roles,
        field_label="hyperlink role",
        discovery_hint=(
            "use a configured id such as 'ref_int' (internal) or 'ref_ext' (external)."
        ),
    )


async def _existing_work_item_types(
    client: PolarionClient,
    project_id: str,
    work_item_ids: frozenset[str],
) -> dict[str, str]:
    """id -> type for the subset of *work_item_ids* that exist in
    *project_id*, via chunked ``id:(...)`` queries; type is ``""`` when the
    attribute is absent. 404 (project missing) propagates to caller.
    """
    ordered = sorted(work_item_ids)
    found: dict[str, str] = {}
    path = f"/projects/{encode_path_segment(project_id)}/workitems"
    for start in range(0, len(ordered), _GUARD_PAGE_SIZE):
        chunk = ordered[start : start + _GUARD_PAGE_SIZE]
        params: dict[str, str | int] = {
            "query": f"id:({' '.join(chunk)})",
            "fields[workitems]": "id,type",
            "page[size]": _GUARD_PAGE_SIZE,
            "page[number]": 1,
        }
        response = await client.get(path, params=params)
        data = response.get("data", [])
        if isinstance(data, list):
            for entry in data:
                if not isinstance(entry, dict):
                    continue
                short_id = extract_short_id(safe_str(entry.get("id", "")))
                if not short_id:
                    continue
                attrs = entry.get("attributes")
                type_id = attrs.get("type") if isinstance(attrs, dict) else None
                found[short_id] = type_id if isinstance(type_id, str) else ""
    return found


async def guard_work_item_types(
    client: PolarionClient,
    project_id: str,
    work_item_ids: Iterable[str],
) -> dict[str, str]:
    """Fail-closed existence check; returns id -> type (enum guards scope by
    type). Raises ``ValueError`` naming every id that does not exist;
    ``PolarionNotFoundError`` (bad project) propagates to the caller.
    """
    requested = frozenset(wi for wi in work_item_ids if wi)
    try:
        resolved = await _existing_work_item_types(client, project_id, requested)
    except PolarionAuthError as exc:
        raise _unauthorized_write_block("work item existence", project_id) from exc
    except PolarionNotFoundError:
        raise
    except PolarionError as exc:
        raise _unreachable_write_block("work item existence", project_id, exc) from exc

    missing = sorted(requested - resolved.keys())
    if missing:
        raise ValueError(
            f"Work item(s) {format_option_list(missing)} not found in "
            f"project '{project_id}'. Use `list_work_items` to discover "
            "valid IDs."
        )
    return resolved


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
            existing = await _existing_work_item_types(
                client, project_id, frozenset(requested)
            )
        except PolarionNotFoundError:
            missing.extend(f"{project_id}/{wi}" for wi in sorted(requested))
            continue
        except PolarionAuthError as exc:
            raise _unauthorized_write_block("link targets", project_id) from exc
        except PolarionError as exc:
            raise _unreachable_write_block("link targets", project_id, exc) from exc
        missing.extend(
            f"{project_id}/{wi}" for wi in sorted(requested - existing.keys())
        )

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
            "page[size]": _GUARD_PAGE_SIZE,
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
        if len(data) < _GUARD_PAGE_SIZE:
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


__all__ = [
    "fetch_enum_option_ids",
    "fetch_project_enum_option_ids",
    "guard_document_custom_fields",
    "guard_document_enums",
    "guard_hyperlink_roles",
    "guard_test_run_custom_fields",
    "guard_test_run_enums",
    "guard_test_run_templates",
    "guard_work_item_custom_fields",
    "guard_work_item_enums",
    "guard_work_item_link_roles",
    "guard_work_item_link_targets",
    "guard_work_item_types",
    "partition_delete_links",
]
