"""Server-side write guards that prevent silent corruption on Polarion writes.

Polarion accepts unknown enum ids (``type`` / ``status`` / ``severity`` /
``priority`` / ``resolution`` on work items; ``type`` / ``status`` on
documents) and unknown
custom-field keys verbatim -- HTTP 200, the value persists, but the field
never appears in Polarion's UI, in Lucene, or in reports, and is never
reported as an error. Docstring-level rules ("call ``list_*_enum_options``
first") are unreliable: multi-model eval runs show LLMs ignore them. This
module turns each rule into a deterministic precondition -- before any write
carrying an enum-valued argument or custom-field keys, fetch the project's
actual options and raise if the supplied id/key is not among them.

Validated option ids and observed custom-field keys are memoised in
:mod:`mcp_server_polarion.tools._cache`; this module holds only the fetch and
check logic.

Fail-closed. If a validation request errors after the client's 429/5xx
backoff, the guard raises ``RuntimeError`` rather than letting the write
through: a ghost write is invisible in Polarion and unrecoverable, so an
unverifiable write is refused rather than risked. Two lenient cases defer to
Polarion instead of blocking: a *successful* empty option set (a field with no
configured options), and a 404 from ``getAvailableOptions`` (the endpoint or
field is unsupported on this instance, so there is nothing to validate
against). A 404 cannot mask a ghost -- a genuinely wrong project/field path
makes the subsequent write fail loudly. This keeps a single unsupported
endpoint from blocking every enum-bearing write on an instance that lacks it.
"""

from __future__ import annotations

import logging
from collections.abc import Iterable

from mcp_server_polarion.core.client import PolarionClient
from mcp_server_polarion.core.exceptions import PolarionError, PolarionNotFoundError
from mcp_server_polarion.models import WorkItemLinkSpec
from mcp_server_polarion.tools._cache import (
    Resource,
    get_cached_enum_options,
    get_cached_project_enum,
    get_document_custom_keys,
    get_work_item_custom_keys,
    record_document_custom_field_keys,
    record_work_item_custom_field_keys,
    store_cached_enum_options,
    store_cached_project_enum,
)
from mcp_server_polarion.tools._helpers import (
    DOCUMENT_DETAIL_FIELDS,
    STANDARD_DOCUMENT_ATTRIBUTES,
    STANDARD_WORK_ITEM_ATTRIBUTES,
    WORK_ITEM_DETAIL_FIELDS,
    encode_path_segment,
    extract_short_id,
    safe_str,
)

logger = logging.getLogger("mcp_server_polarion._guard")

_GUARD_PAGE_SIZE: int = 100


def _unreachable_write_block(
    what: str, project_id: str, exc: PolarionError
) -> RuntimeError:
    """Build the fail-closed error raised when validation cannot reach Polarion."""
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


async def fetch_enum_option_ids(
    client: PolarionClient,
    project_id: str,
    resource: Resource,
    field_id: str,
    type_id: str,
) -> frozenset[str]:
    """Return the valid option ids for ``(project, resource, field, type)``.

    Cached for the guard TTL. Fail-closed on a *reachable-but-erroring*
    backend: after the client's own 429/5xx backoff, a 5xx or auth error
    raises ``RuntimeError`` so the caller's write is blocked rather than
    risking a ghost. A 404 is the exception -- it means ``getAvailableOptions``
    is unsupported on this instance (or the field/project path does not
    exist), so there is nothing to validate against; the guard defers (empty
    set) rather than block every enum-bearing write. A genuinely wrong target
    makes the subsequent write fail loudly, so deferring cannot mask a ghost.
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
        # Endpoint/field unsupported on this instance: cache an empty set so
        # _check_enum defers and we do not re-probe a missing endpoint on
        # every write within the TTL.
        logger.warning(
            "getAvailableOptions returned 404 for field=%s (resource=%s, "
            "project=%s); skipping enum validation for this field -- the "
            "endpoint or field is unsupported here, so there is nothing to "
            "validate against.",
            field_id,
            resource,
            project_id,
        )
        store_cached_enum_options(project_id, resource, field_id, type_id, frozenset())
        return frozenset()
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
    # An empty set is a successful "no options configured" response (a
    # deprecated field, or a type axis with none), not the unreachable failure
    # that already raised above. Defer to Polarion rather than risk a false
    # positive on a legitimately optionless field.
    if not option_ids or value in option_ids:
        return
    raise ValueError(
        f"{field_id}='{value}' is not a valid {field_id} option in "
        f"project '{project_id}' for {resource} type '{type_id}'. "
        f"Valid options: {sorted(option_ids)}. "
        f"Polarion accepts unknown ids as silent ghosts that never match "
        f"Lucene queries -- call list_{resource[:-1]}_enum_options first."
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

    ``work_item_type`` is the type axis Polarion scopes
    status/severity/resolution/priority by. Pass ``'~'`` for type-agnostic
    lookups (used when validating ``type`` itself or when the caller does not
    have a type in hand). ``type`` is always checked first so an invalid
    ``type`` raises before it could be used as the scoping axis. On cache hit
    the guard adds zero round trips; on miss, one GET per (field, type) pair.

    Raises ``ValueError`` listing the valid options on an unknown id, or
    ``RuntimeError`` if Polarion is unreachable (fail-closed).
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


def _custom_keys_from_attributes(
    response: dict[str, object], allowlist: frozenset[str]
) -> frozenset[str]:
    data = response.get("data", {})
    attrs: dict[str, object] = {}
    if isinstance(data, dict):
        raw_attrs = data.get("attributes")
        if isinstance(raw_attrs, dict):
            attrs = raw_attrs
    return frozenset(k for k in attrs if isinstance(k, str) and k not in allowlist)


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


async def guard_work_item_custom_field_keys(
    client: PolarionClient,
    project_id: str,
    work_item_id: str,
    work_item_type: str,
    custom_fields: dict[str, object],
) -> None:
    """Reject ``update_work_item.custom_fields`` keys never seen on a get.

    Looks up the per-type observed-keys cache; on miss, does one inline
    ``get_work_item``-shaped GET to populate, then validates. Unknown keys
    raise ``ValueError``; an unreachable priming GET raises ``RuntimeError``
    (fail-closed).
    """
    if not custom_fields:
        return

    known = get_work_item_custom_keys(project_id, work_item_type)
    if known is None:
        path = (
            f"/projects/{encode_path_segment(project_id)}"
            f"/workitems/{encode_path_segment(work_item_id)}"
        )
        try:
            response = await client.get(
                path, params={"fields[workitems]": WORK_ITEM_DETAIL_FIELDS}
            )
        except PolarionError as exc:
            raise _unreachable_write_block(
                "custom_fields keys", project_id, exc
            ) from exc
        known = _custom_keys_from_attributes(response, STANDARD_WORK_ITEM_ATTRIBUTES)
        record_work_item_custom_field_keys(project_id, work_item_type, known)

    _reject_unknown_custom_keys(
        custom_fields,
        known,
        scope=f"work_item_type '{work_item_type}'",
        discovery_tool="get_work_item",
    )


async def guard_document_custom_field_keys(
    client: PolarionClient,
    project_id: str,
    space_id: str,
    document_name: str,
    custom_fields: dict[str, object],
) -> None:
    """Reject ``update_document.custom_fields`` keys never seen on a get.

    Mirrors :func:`guard_work_item_custom_field_keys` but keys the cache by
    ``(project, space, document)``; on miss, does one inline
    ``get_document``-shaped GET to populate, then validates.
    """
    if not custom_fields:
        return

    known = get_document_custom_keys(project_id, space_id, document_name)
    if known is None:
        path = (
            f"/projects/{encode_path_segment(project_id)}"
            f"/spaces/{encode_path_segment(space_id)}"
            f"/documents/{encode_path_segment(document_name)}"
        )
        try:
            response = await client.get(
                path, params={"fields[documents]": DOCUMENT_DETAIL_FIELDS}
            )
        except PolarionError as exc:
            raise _unreachable_write_block(
                "custom_fields keys", project_id, exc
            ) from exc
        known = _custom_keys_from_attributes(response, STANDARD_DOCUMENT_ATTRIBUTES)
        record_document_custom_field_keys(project_id, space_id, document_name, known)

    _reject_unknown_custom_keys(
        custom_fields,
        known,
        scope=f"document '{space_id}/{document_name}'",
        discovery_tool="get_document",
    )


async def _existing_target_ids(
    client: PolarionClient,
    project_id: str,
    target_ids: frozenset[str],
) -> frozenset[str]:
    """Return which *target_ids* exist in *project_id*, via ``id:(...)`` queries.

    Chunked at ``_GUARD_PAGE_SIZE``: a single ``id:(...)`` query is bounded by
    ``page[size]``, so more targets than that need successive queries. A 404
    means the project does not exist; the caller treats every target there as
    missing.
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

    Polarion creates a dangling link (HTTP 201) to a nonexistent target -- the
    target shows empty title/type/status in ``list_work_item_links`` and there
    is no error to detect it. Groups requested targets by project, runs one
    ``id:(...)`` query per project (chunked at 100), and raises ``ValueError``
    listing any target that was not returned.

    Fail-closed: an unreachable backend raises ``RuntimeError``; a 404 (the
    target project does not exist) raises ``ValueError`` since every target
    there would be dangling.
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

    Used for enums Polarion does not expose through ``getAvailableOptions``
    (link role, hyperlink role). Reads the single-enumeration resource
    ``/projects/{p}/enumerations/~/{enum}/~`` -- far lighter than the bulk
    ``getProjectEnumerations`` listing, which returns every enum including
    large dynamic ones. Unlike ``getAvailableOptions`` (a list ``data``), this
    single-resource response returns ``data`` as a dict whose options live at
    ``data.attributes.options[].id``.

    Cached for the guard TTL. Same fail-closed contract as
    :func:`fetch_enum_option_ids`: a 404 means the enumeration is unsupported
    here, so the guard defers (empty set); any other error after the client's
    backoff raises ``RuntimeError`` to block the write.
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
    # An empty set is the lenient "no options / enum unsupported here" case
    # that already deferred above, not the unreachable failure that raised.
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

    Polarion stores an unknown link role verbatim (HTTP 201) and it never
    matches Lucene -- a ghost link with no error to detect it. Validates every
    requested role against the project's ``workitem-link-role`` enumeration and
    raises ``ValueError`` listing the valid ids on a miss; fail-closed
    (``RuntimeError``) if the enumeration cannot be reached.
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

    ``Hyperlink.role`` on ``create_work_items`` / ``update_work_item`` accepts
    only configured ids (typically ``ref_int`` / ``ref_ext``); an unknown role
    persists as a silent ghost on the work item. Validates against the
    ``hyperlink-role`` enumeration and raises ``ValueError`` on a miss;
    fail-closed if it cannot be reached.
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


__all__ = [
    "fetch_enum_option_ids",
    "fetch_project_enum_option_ids",
    "guard_document_custom_field_keys",
    "guard_document_enums",
    "guard_hyperlink_roles",
    "guard_work_item_custom_field_keys",
    "guard_work_item_enums",
    "guard_work_item_link_roles",
    "guard_work_item_link_targets",
]
