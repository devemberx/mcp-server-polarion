"""Server-side write guards that prevent silent corruption on Polarion writes.

Polarion accepts unknown enum ids (``type`` / ``status`` / ``severity`` /
``priority`` on work items; ``type`` / ``status`` on documents) and unknown
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
unverifiable write is refused rather than risked. The one lenient case is a
*successful* empty option set (a field with no configured options), where the
guard defers to Polarion rather than risk a false positive.
"""

from __future__ import annotations

import logging

from mcp_server_polarion.core.client import PolarionClient
from mcp_server_polarion.core.exceptions import PolarionError
from mcp_server_polarion.tools._cache import (
    Resource,
    get_cached_enum_options,
    get_document_custom_keys,
    get_work_item_custom_keys,
    record_document_custom_field_keys,
    record_work_item_custom_field_keys,
    store_cached_enum_options,
)
from mcp_server_polarion.tools._helpers import (
    DOCUMENT_DETAIL_FIELDS,
    STANDARD_DOCUMENT_ATTRIBUTES,
    STANDARD_WORK_ITEM_ATTRIBUTES,
    WORK_ITEM_DETAIL_FIELDS,
    encode_path_segment,
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

    Cached for the guard TTL. Fail-closed: on Polarion error (after the
    client's own 429/5xx backoff) this raises ``RuntimeError`` rather than
    returning a sentinel, so the caller's write is blocked.
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
) -> None:
    """Validate every supplied work-item enum arg against ``getAvailableOptions``.

    ``work_item_type`` is the type axis Polarion scopes status/severity by.
    Pass ``'~'`` for type-agnostic lookups (used when validating ``type``
    itself or when the caller does not have a type in hand). On cache hit the
    guard adds zero round trips; on miss, one GET per (field, type) pair.

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


__all__ = [
    "fetch_enum_option_ids",
    "guard_document_custom_field_keys",
    "guard_document_enums",
    "guard_work_item_custom_field_keys",
    "guard_work_item_enums",
]
