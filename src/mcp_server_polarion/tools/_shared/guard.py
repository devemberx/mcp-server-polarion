"""Server-side write guards that prevent silent corruption on Polarion writes.

Polarion accepts unknown enum ids and custom-field keys and values verbatim
(HTTP 200) — they persist but never appear in the UI, Lucene, or reports,
with no error.
Docstring rules are unreliable (evals show LLMs ignore them), so each rule
becomes a deterministic precondition: before a write, fetch the real options
and raise if the supplied id/key is absent. Option ids and observed keys are
memoised in :mod:`...tools._shared.cache`; this module holds fetch + check.

Fail-closed: a validation request that errors after backoff blocks the write
(a ghost write is invisible and unrecoverable) — auth → ``PermissionError``,
else → ``RuntimeError``. Two lenient cases defer to Polarion: a *successful*
empty option set, and a 404 (endpoint/field unsupported — a wrong path makes
the subsequent write fail loudly anyway).
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
    get_work_item_custom_keys,
    invalidate_document_type_custom_keys,
    invalidate_work_item_custom_keys,
    store_cached_enum_options,
    store_cached_project_enum,
    store_document_type_custom_keys,
    store_work_item_custom_keys,
)
from mcp_server_polarion.tools._shared.helpers import (
    DOCUMENT_DETAIL_FIELDS,
    STANDARD_DOCUMENT_ATTRIBUTES,
    STANDARD_WORK_ITEM_ATTRIBUTES,
    WORK_ITEM_DETAIL_FIELDS,
    encode_path_segment,
    extract_short_id,
    safe_str,
)
from mcp_server_polarion.tools._shared.sql import (
    one_heading_per_document_sql,
    one_item_per_custom_field_sql,
)

logger = logging.getLogger("mcp_server_polarion.tools._shared.guard")

_GUARD_PAGE_SIZE: int = 100

# Resource -> the MCP tool that lists its enum options, for error messages.
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
        f"Cannot validate {what} against project '{project_id}' before writing: "
        f"Polarion validation request failed ({exc.message}). Refusing the write "
        f"-- unknown ids/keys persist as silent ghosts that never appear in "
        f"Polarion's UI or Lucene and are never reported as errors. Retry once "
        f"Polarion is reachable."
    )


def _unauthorized_write_block(what: str, project_id: str) -> PermissionError:
    """Mirror the tool layer's ``PolarionAuthError -> PermissionError``: a
    token-scope problem the caller can fix, not a backend to retry.
    """
    logger.warning(
        "guard blocking write: not authorized to validate %s for project=%s",
        what,
        project_id,
    )
    return PermissionError(
        f"Cannot validate {what} against project '{project_id}' before writing: "
        f"the POLARION_TOKEN lacks permission for the validation request. "
        f"Refusing the write -- check your token's permissions."
    )


async def fetch_enum_option_ids(
    client: PolarionClient,
    project_id: str,
    resource: Resource,
    field_id: str,
    type_id: str,
) -> frozenset[str]:
    """Return the valid option ids for ``(project, resource, field, type)``.

    Cached. Fail-closed: a reachable error raises, a 404 defers (empty set).
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
        # Endpoint/field unsupported (Polarion: "Field 'X' is not an
        # Enumeration field"): cache an empty set so _check_enum defers. The
        # long not_found TTL spares non-enum custom fields a re-probe on every
        # write; its stale worst case is this same deferral.
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
    # Empty set = successful "no options configured" (not the unreachable
    # failure, which already raised). Defer rather than false-positive.
    if not option_ids or value in option_ids:
        return
    raise ValueError(
        f"{field_id}='{value}' is not a valid {field_id} option in "
        f"project '{project_id}' for {resource} type '{type_id}'. "
        f"Valid options: {sorted(option_ids)}. "
        f"Polarion accepts unknown ids as silent ghosts that never match "
        f"Lucene queries -- call {_ENUM_DISCOVERY_TOOL[resource]} first."
    )


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
    """Validate every supplied work-item enum arg against ``getAvailableOptions``.

    ``work_item_type`` scopes status/severity/resolution/priority (``'~'`` =
    type-agnostic). ``type`` is checked first so an invalid type raises before
    being reused as the scoping axis. One GET per (field, type) on a miss.
    Raises ``ValueError`` (unknown id) or ``RuntimeError`` (unreachable).
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
        f"Valid options: {sorted(option_ids)}. "
        f"Polarion accepts unknown enum values as silent ghosts that never "
        f"appear in its UI or match Lucene queries -- call "
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

    The endpoint is the only API that both identifies a custom field as an
    enumeration and yields its option ids (404 = "not an Enumeration field");
    a non-empty option set therefore proves the field is an enum, so the value
    must be an option-id string or a list of them (multi-enum). Empty set
    (non-enum field, or enum with no options) defers to Polarion. One GET per
    key on a cache miss; 404s are cached long (``not_found`` TTL).
    """
    for field_id in sorted(custom_fields):
        option_ids = await fetch_enum_option_ids(
            client, project_id, resource, field_id, type_id
        )
        if not option_ids:
            continue
        value = custom_fields[field_id]
        if isinstance(value, str):
            if value and value not in option_ids:
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
            f"{discovery_tool} for {scope}. Known keys: {sorted(known)}. "
            f"Polarion accepts unknown keys as silent ghost attributes -- "
            f"fetch a sample first to discover the project's real custom-field ids."
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


async def _fetch_work_item_type_custom_keys(
    client: PolarionClient,
    project_id: str,
    type_id: str,
) -> frozenset[str]:
    """Sample existing items of a type and return their unioned custom-field keys.

    MIN-per-key SQL yields one item per distinct key; paged at 100 so a type with
    more than 100 distinct keys still returns the complete schema. SQL rejection
    fails closed (``RuntimeError``): a partial Lucene sample would silently
    false-reject real keys, so custom-field writes are blocked rather than
    validated against an incomplete schema. Result cached even if empty.
    Fail-closed: auth → ``PermissionError``, unreachable → ``RuntimeError``.
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
    """Reject ``custom_fields`` keys absent from the type's real schema.

    The type schema (cached per ``(project, type)`` from the MIN-per-key sample)
    is the sole source of truth. A key unknown against a *cached* schema forces
    one fresh re-fetch (admin-added field) before rejecting → ``ValueError``.
    Empty schema fails closed (``RuntimeError``): a ghost write is unrecoverable.
    Unreachable validation (incl. SQL rejection) → ``RuntimeError``.
    """
    schema = get_work_item_custom_keys(project_id, work_item_type)
    fetched_fresh = schema is None
    if schema is None:
        schema = await _fetch_work_item_type_custom_keys(
            client, project_id, work_item_type
        )

    if all(key in schema for key in custom_fields):
        return

    # An unknown key against a cached schema may be an admin-added field; refetch
    # once before rejecting. A just-fetched schema is already current, so skip.
    if not fetched_fresh:
        invalidate_work_item_custom_keys(project_id, work_item_type)
        schema = await _fetch_work_item_type_custom_keys(
            client, project_id, work_item_type
        )

    if not schema:
        raise RuntimeError(
            f"Cannot verify custom_fields for work_item_type '{work_item_type}' in "
            f"project '{project_id}': no existing item of this type has any custom "
            f"field populated, so its schema can't be inferred. Refusing the write "
            f"-- unknown keys persist as silent ghosts invisible to Polarion's UI "
            f"and Lucene. Save one item of this type with its custom fields filled "
            f"(web UI, template, or a key you are certain of), then retry."
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
    """Validate ``custom_fields`` keys and enum-typed values before a write.

    Keys first, against the type's sampled schema (unknown key →
    ``ValueError``; the order also keeps ghost keys out of the enum probe's
    long-lived 404 cache). Then each key's value via ``getAvailableOptions``:
    a non-empty option set makes the field an enum whose value must be a valid
    option-id string or list of them → ``ValueError`` on wrong id or shape.
    Fail-closed otherwise.
    """
    if not custom_fields:
        return
    await _check_work_item_custom_keys(
        client, project_id, work_item_type, custom_fields
    )
    await _check_custom_field_enum_values(
        client, project_id, "workitems", work_item_type, custom_fields
    )


async def _fetch_document_type_custom_keys(
    client: PolarionClient,
    project_id: str,
    document_type: str,
) -> frozenset[str]:
    """Sample the project's documents and return *document_type*'s key schema.

    A heading-discovery SQL (:func:`one_heading_per_document_sql`) paired with
    ``include=module&fields[documents]=@all`` returns one ``module`` resource per
    document in the ``included`` array, each carrying its type + inline customs.
    The ``module`` table can't be returned as a REST SQL resource directly, but
    ``include`` surfaces its attributes -- unlike the ``GET /documents`` endpoint,
    this path exists on every Polarion build. Customs are unioned per type and
    every type's schema is stored, so a later write of any type hits the cache.
    The target type is stored even when empty so a no-customs type fails closed
    without re-probing. Documents with no heading work item are invisible to this
    sample. Fail-closed: auth → ``PermissionError``, unreachable → ``RuntimeError``.
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
    """Reject ``custom_fields`` keys absent from the document type's real schema.

    Mirrors :func:`_check_work_item_custom_keys` on the document-type axis:
    the schema (cached per ``(project, document_type)`` from the heading +
    ``include=module`` sample) is the sole source of truth. A key unknown against
    a *cached* schema forces one fresh re-fetch (admin-added field) before
    rejecting → ``ValueError``. A type whose documents populate no custom field
    yields an empty schema and fails closed (``RuntimeError``).
    """
    schema = get_document_type_custom_keys(project_id, document_type)
    fetched_fresh = schema is None
    if schema is None:
        schema = await _fetch_document_type_custom_keys(
            client, project_id, document_type
        )

    if all(key in schema for key in custom_fields):
        return

    # An unknown key against a cached schema may be an admin-added field; refetch
    # once before rejecting. A just-fetched schema is already current, so skip.
    if not fetched_fresh:
        invalidate_document_type_custom_keys(project_id, document_type)
        schema = await _fetch_document_type_custom_keys(
            client, project_id, document_type
        )

    if not schema:
        raise RuntimeError(
            f"Cannot verify custom_fields for document type '{document_type}' in "
            f"project '{project_id}': no existing document of this type has any "
            f"custom field populated, so its schema can't be inferred. Refusing the "
            f"write -- unknown keys persist as silent ghosts invisible to Polarion's "
            f"UI and Lucene. Save one document of this type with its custom fields "
            f"filled, then retry."
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
    """Validate ``custom_fields`` keys and enum-typed values before a write.

    Document-axis mirror of :func:`guard_work_item_custom_fields`: keys against
    the document type's sampled schema, then enum-typed values against
    ``getAvailableOptions``.
    """
    if not custom_fields:
        return
    await _check_document_custom_keys(client, project_id, document_type, custom_fields)
    await _check_custom_field_enum_values(
        client, project_id, "documents", document_type, custom_fields
    )


async def _existing_target_ids(
    client: PolarionClient,
    project_id: str,
    target_ids: frozenset[str],
) -> frozenset[str]:
    """Return which *target_ids* exist in *project_id*, via ``id:(...)`` queries.

    Chunked at ``_GUARD_PAGE_SIZE`` (one query bounded by ``page[size]``). A 404
    means the project is missing; caller treats every target as missing.
    """
    ordered = sorted(target_ids)
    found: set[str] = set()
    for start in range(0, len(ordered), _GUARD_PAGE_SIZE):
        chunk = ordered[start : start + _GUARD_PAGE_SIZE]
        params: dict[str, str | int] = {
            "query": f"id:({' '.join(chunk)})",
            "fields[workitems]": "id",
            "page[size]": _GUARD_PAGE_SIZE,
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
    """Reject links whose target work item does not exist.

    A nonexistent target stores as a silent dangling link (HTTP 201, empty
    title/type/status). Groups targets by project, one ``id:(...)`` query each,
    raises ``ValueError`` listing any missing. Fail-closed: unreachable →
    ``RuntimeError``; 404 (project missing) → ``ValueError``.
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
            raise _unauthorized_write_block("link targets", project_id) from exc
        except PolarionError as exc:
            raise _unreachable_write_block("link targets", project_id, exc) from exc
        missing.extend(f"{project_id}/{wi}" for wi in sorted(requested - existing))

    if missing:
        raise ValueError(
            f"Link target work item(s) {sorted(missing)} do not exist. "
            f"Polarion accepts a nonexistent target as a silent dangling link "
            f"(HTTP 201) with empty title/type/status -- use list_work_items to "
            f"discover valid target ids before linking."
        )


async def fetch_project_enum_option_ids(
    client: PolarionClient,
    project_id: str,
    enum_name: str,
) -> frozenset[str]:
    """Return the valid option ids for a project-level enumeration.

    For enums not in ``getAvailableOptions`` (link/hyperlink role). Reads
    ``/projects/{p}/enumerations/~/{enum}/~``; unlike ``getAvailableOptions``
    (list ``data``), here ``data`` is a dict with options at
    ``data.attributes.options[].id``. Cached; fail-closed like
    :func:`fetch_enum_option_ids`.
    """
    cached = get_cached_project_enum(project_id, enum_name)
    if cached is not None:
        return cached

    path = (
        f"/projects/{encode_path_segment(project_id)}"
        f"/enumerations/~/{encode_path_segment(enum_name)}/~"
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
        store_cached_project_enum(project_id, enum_name, frozenset())
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
    store_cached_project_enum(project_id, enum_name, option_ids)
    return option_ids


async def _check_project_enum_roles(  # noqa: PLR0913
    client: PolarionClient,
    project_id: str,
    enum_name: str,
    roles: Iterable[str],
    *,
    field_label: str,
    discovery_hint: str,
) -> None:
    requested = {role for role in roles if role}
    if not requested:
        return

    option_ids = await fetch_project_enum_option_ids(client, project_id, enum_name)
    # Empty set = lenient "no options / enum unsupported" (already deferred).
    if not option_ids:
        return

    unknown = sorted(requested - option_ids)
    if unknown:
        raise ValueError(
            f"{field_label} id(s) {unknown} are not valid in project "
            f"'{project_id}'. Valid options: {sorted(option_ids)}. "
            f"Polarion accepts an unknown {field_label} as a silent ghost that "
            f"never matches Lucene queries -- {discovery_hint}"
        )


async def guard_work_item_link_roles(
    client: PolarionClient,
    project_id: str,
    roles: Iterable[str],
) -> None:
    """Reject ``create_work_item_links`` roles not in ``workitem-link-role``.

    An unknown role stores verbatim (HTTP 201) as a ghost link. Validates each
    role, raises ``ValueError`` (valid ids) on a miss; fail-closed otherwise.
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
    """Reject hyperlink roles not in the project's ``hyperlink-role`` enum.

    ``Hyperlink.role`` accepts only configured ids (typically ``ref_int`` /
    ``ref_ext``); an unknown role persists as a silent ghost. Raises
    ``ValueError`` on a miss; fail-closed if unreachable.
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


async def _existing_forward_link_ids(
    client: PolarionClient,
    project_id: str,
    work_item_id: str,
) -> frozenset[str]:
    """Return the composite ids of every outgoing link on the source work item.

    Pages ``/linkedworkitems`` (id only) until a short page. Each ``data[].id``
    is the 5-segment composite the delete payload reconstructs, so it is
    set-membership-testable directly. ``PolarionNotFoundError`` propagates.
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
    """Split requested delete refs into ``(matched, not_found)`` against reality.

    Pre-reads existing outgoing links and partitions *link_ids* (input order)
    into matched / unmatched — the only way to surface the no-ops the 204 hides
    (no-op is non-destructive, never raised). Fail-closed: missing source →
    ``ValueError``, auth → ``PermissionError``, else → ``RuntimeError``.
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
    "guard_work_item_custom_fields",
    "guard_work_item_enums",
    "guard_work_item_link_roles",
    "guard_work_item_link_targets",
    "partition_delete_links",
]
