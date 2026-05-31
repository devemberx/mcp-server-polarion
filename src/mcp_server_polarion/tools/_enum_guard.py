"""Server-side guards that prevent silent corruption on Polarion writes.

Polarion accepts unknown enum ids (``type`` / ``status`` / ``severity`` /
``priority`` on work items; ``type`` / ``status`` on documents) and unknown
custom-field keys verbatim — HTTP 200, the value persists, but the field
never appears in Lucene queries or reports. Docstring-level rules ("MUST
first call ``list_*_enum_options``") are unreliable in practice — multi-model
eval runs show LLMs ignore them. This module turns those rules into a
deterministic precondition: before every write that carries an enum-valued
argument, fetch the project's actual ``getAvailableOptions`` set and raise
``ValueError`` if the supplied id is not in it.

Two TTL caches keep the overhead bounded:

* ``_enum_cache`` — per ``(project, resource, field, type)`` option-id set;
  one ``getAvailableOptions`` GET per cache miss, then 60 s of free hits.
* ``_observed_custom_keys`` — per ``(project, work_item_type)`` set of
  custom-field keys ever seen on ``get_work_item`` responses in this session.
  ``update_work_item`` uses it to reject ghost custom-field keys; on a
  miss, the guard does a single inline ``get_work_item``-shaped GET to
  populate before validating.

Both caches mirror the ``_documents_cache`` pattern in :mod:`_helpers`
(frozen dataclass + ``time.monotonic()`` expiry + module-level dict). Soft
fail: if the underlying ``getAvailableOptions`` GET errors after the
client's built-in 429/5xx backoff, the guard logs a warning and lets the
write proceed — Polarion-outage availability trumps strict validation
since the ghost-write window is narrow and the warning provides forensic
trail.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Final, Literal

from mcp_server_polarion.core.client import PolarionClient
from mcp_server_polarion.core.exceptions import PolarionError
from mcp_server_polarion.tools._helpers import (
    STANDARD_WORK_ITEM_ATTRIBUTES,
    encode_path_segment,
)

logger = logging.getLogger("mcp_server_polarion._enum_guard")

_CACHE_TTL_SECONDS: Final[float] = 60.0
_GUARD_PAGE_SIZE: Final[int] = 100

Resource = Literal["workitems", "documents"]


@dataclass(frozen=True, slots=True)
class _EnumCacheEntry:
    expires_at: float
    option_ids: frozenset[str]


@dataclass(frozen=True, slots=True)
class _CustomKeyCacheEntry:
    expires_at: float
    keys: frozenset[str]


_enum_cache: dict[tuple[str, Resource, str, str], _EnumCacheEntry] = {}
_observed_custom_keys: dict[tuple[str, str], _CustomKeyCacheEntry] = {}


def _now() -> float:
    return time.monotonic()


def _get_cached_enum_ids(
    project_id: str,
    resource: Resource,
    field_id: str,
    type_id: str,
) -> frozenset[str] | None:
    key = (project_id, resource, field_id, type_id)
    entry = _enum_cache.get(key)
    if entry is None:
        return None
    if _now() >= entry.expires_at:
        _enum_cache.pop(key, None)
        return None
    return entry.option_ids


def _store_cached_enum_ids(
    project_id: str,
    resource: Resource,
    field_id: str,
    type_id: str,
    option_ids: frozenset[str],
) -> None:
    _enum_cache[(project_id, resource, field_id, type_id)] = _EnumCacheEntry(
        expires_at=_now() + _CACHE_TTL_SECONDS,
        option_ids=option_ids,
    )


async def fetch_enum_option_ids(
    client: PolarionClient,
    project_id: str,
    resource: Resource,
    field_id: str,
    type_id: str,
) -> frozenset[str] | None:
    """Return the valid option ids for ``(project, resource, field, type)``.

    Cached for ``_CACHE_TTL_SECONDS``. On Polarion error (after the client's
    own 429/5xx backoff), logs a stderr warning and returns ``None`` —
    callers MUST treat ``None`` as "guard skipped, proceed without check".
    """
    cached = _get_cached_enum_ids(project_id, resource, field_id, type_id)
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
        logger.warning(
            "enum guard skipped: getAvailableOptions failed for "
            "project=%s resource=%s field=%s type=%s (%s)",
            project_id,
            resource,
            field_id,
            type_id,
            exc.message,
        )
        return None

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
    _store_cached_enum_ids(project_id, resource, field_id, type_id, option_ids)
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
    # ``None`` is the soft-fail sentinel; an empty ``frozenset`` means the
    # field has no configured options in this project (legitimate state for
    # deprecated or unused fields), so the guard has no basis to reject —
    # skip and let Polarion be the arbiter.
    if option_ids is None or not option_ids or value in option_ids:
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
    itself or when the caller does not have a type in hand). On cache hit
    the guard adds zero round trips; on miss, one GET per (field, type)
    pair, cached for 60 s.

    Raises ``ValueError`` listing the valid options on the first miss.
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


def _get_cached_custom_keys(
    project_id: str, work_item_type: str
) -> frozenset[str] | None:
    key = (project_id, work_item_type)
    entry = _observed_custom_keys.get(key)
    if entry is None:
        return None
    if _now() >= entry.expires_at:
        _observed_custom_keys.pop(key, None)
        return None
    return entry.keys


def record_custom_keys_from_get(
    project_id: str,
    work_item_type: str,
    keys: Iterable[str],
) -> None:
    """Merge ``keys`` into the observed-keys cache for ``(project, type)``.

    Called from ``get_work_item`` so subsequent ``update_work_item`` calls
    can validate ``custom_fields`` keys against ids the LLM has actually
    seen. Empty ``keys`` still bumps the TTL so the type counts as
    "observed" (no inline fetch needed by the guard later).
    """
    new_keys = frozenset(k for k in keys if isinstance(k, str) and k)
    cached = _get_cached_custom_keys(project_id, work_item_type) or frozenset()
    merged = cached | new_keys
    _observed_custom_keys[(project_id, work_item_type)] = _CustomKeyCacheEntry(
        expires_at=_now() + _CACHE_TTL_SECONDS,
        keys=merged,
    )


async def guard_update_custom_field_keys(
    client: PolarionClient,
    project_id: str,
    work_item_id: str,
    work_item_type: str,
    custom_fields: dict[str, object],
) -> None:
    """Reject ``update_work_item.custom_fields`` keys never seen on a get.

    Looks up the per-type observed-keys cache; on miss, does one inline
    ``get_work_item``-shaped GET to populate, then validates. Unknown keys
    raise ``ValueError`` with the known set. Soft-fail on the inline GET
    mirrors :func:`fetch_enum_option_ids`.
    """
    if not custom_fields:
        return

    known = _get_cached_custom_keys(project_id, work_item_type)
    if known is None:
        path = (
            f"/projects/{encode_path_segment(project_id)}"
            f"/workitems/{encode_path_segment(work_item_id)}"
        )
        try:
            response = await client.get(path, params={"fields[workitems]": "@all"})
        except PolarionError as exc:
            logger.warning(
                "custom-field guard skipped: priming GET failed for %s/%s (%s)",
                project_id,
                work_item_id,
                exc.message,
            )
            return
        data = response.get("data", {})
        attrs: dict[str, object] = {}
        if isinstance(data, dict):
            raw_attrs = data.get("attributes")
            if isinstance(raw_attrs, dict):
                attrs = raw_attrs
        observed = frozenset(
            k
            for k in attrs
            if isinstance(k, str) and k not in STANDARD_WORK_ITEM_ATTRIBUTES
        )
        record_custom_keys_from_get(project_id, work_item_type, observed)
        known = observed

    unknown = sorted(k for k in custom_fields if k not in known)
    if unknown:
        raise ValueError(
            f"custom_fields key(s) {unknown} were not present in any prior "
            f"get_work_item for type '{work_item_type}'. Known keys: "
            f"{sorted(known)}. Polarion accepts unknown keys as silent "
            f"ghost attributes -- fetch a sample work item first to "
            f"discover the project's actual custom-field ids."
        )


def _reset_caches_for_tests() -> None:
    """Drop all cached entries. Test-only helper; do not call from production."""
    _enum_cache.clear()
    _observed_custom_keys.clear()


__all__ = [
    "fetch_enum_option_ids",
    "guard_document_enums",
    "guard_update_custom_field_keys",
    "guard_work_item_enums",
    "record_custom_keys_from_get",
]
