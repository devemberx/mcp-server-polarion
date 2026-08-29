"""In-process TTL caches for near-static project facts — spare server's
tight budget (throttle deployment-configured, no concurrency). Own ALL
cache state; tool logic reach it only via typed get / store wrappers.

Naming mirror :mod:`...tools._shared.guard.enums`: ``field_*`` = per-field
``getAvailableOptions``, ``enum_*`` = per-project ``/enumerations/``.
"""

from __future__ import annotations

import time
from collections.abc import Mapping
from dataclasses import dataclass
from types import MappingProxyType
from typing import Final, Literal, NamedTuple


def _now() -> float:
    """Monotonic clock seam; patched wholesale in tests to drive TTL expiry."""
    return time.monotonic()


@dataclass(frozen=True, slots=True)
class _Entry[V]:
    expires_at: float
    value: V


class TTLCache[K, V]:
    """Single-threaded TTL cache; lazy expiry — bounded key space stay bounded.

    ``get`` hand back stored object itself — wrapper must close that path,
    by storing immutable value or copying on get. Mutable value reaching
    caller = one edit poison every later read until TTL expire.
    """

    def __init__(self, ttl_seconds: float) -> None:
        self._ttl = ttl_seconds
        self._entries: dict[K, _Entry[V]] = {}

    def get(self, key: K) -> V | None:
        entry = self._entries.get(key)
        if entry is None:
            return None
        if _now() >= entry.expires_at:
            self._entries.pop(key, None)
            return None
        return entry.value

    def set(self, key: K, value: V, ttl_seconds: float | None = None) -> None:
        """Store *value*; *ttl_seconds* override cache default for this entry."""
        ttl = self._ttl if ttl_seconds is None else ttl_seconds
        self._entries[key] = _Entry(expires_at=_now() + ttl, value=value)

    def invalidate(self, key: K) -> None:
        self._entries.pop(key, None)

    def clear(self) -> None:
        self._entries.clear()


Resource = Literal["workitems", "documents"]


class DiscoveredDocument(NamedTuple):
    """One document from ``list_documents`` discovery scan."""

    space_id: str
    document_name: str
    type: str = ""
    status: str = ""
    updated: str = ""
    author_name: str = ""
    last_updated_by_name: str = ""


# Custom-field key schema: sampling scan = priciest guard fetch, and only
# admin field edit change it. Stale window trade against that scan.
# Accepted risk both here and below: refetch-once heal missing entry only, so
# admin-REMOVED key/option stay accepted till expiry -- ghost write unrecoverable.
_SCHEMA_TTL_SECONDS: Final[float] = 900.0

# Enum options: admin-configured too, but guard reject against them, so
# refetch-once (guard/_revalidate.py) carry the freshness, not TTL.
_ENUM_TTL_SECONDS: Final[float] = 600.0

# Work item existence: saved fetch = one batched query, and portal user
# delete work items any time -- keep dangling window narrow.
_TARGET_TTL_SECONDS: Final[float] = 60.0

# 404 "not an Enumeration field" = stable schema fact; stale worst case
# just defer to Polarion — safe to outlive positive option sets.
_FIELD_OPTIONS_NOT_FOUND_TTL_SECONDS: Final[float] = 3600.0

# New documents surface within ~1 min (create also invalidate on write).
_DOCUMENT_LIST_TTL_SECONDS: Final[float] = 60.0


# project_id -> discovered documents in display order.
_document_list_cache: TTLCache[str, tuple[DiscoveredDocument, ...]] = TTLCache(
    _DOCUMENT_LIST_TTL_SECONDS
)


def get_cached_documents(project_id: str) -> list[DiscoveredDocument] | None:
    """Cached document list for *project_id*, or ``None``."""
    cached = _document_list_cache.get(project_id)
    return list(cached) if cached is not None else None


def store_cached_documents(
    project_id: str,
    documents: list[DiscoveredDocument],
) -> None:
    """Cache *documents* for ``_DOCUMENT_LIST_TTL_SECONDS``."""
    _document_list_cache.set(project_id, tuple(documents))


def invalidate_documents_cache(project_id: str) -> None:
    """Drop cached document list for *project_id*, if any."""
    _document_list_cache.invalidate(project_id)


# (project, resource, field, type) -> option id -> display name. Name kept
# beside id so display-name reader (rendering layout `label`) reuse guard's
# fetch instead of spending second request; unnamed option = "".
_field_option_cache: TTLCache[tuple[str, Resource, str, str], Mapping[str, str]] = (
    TTLCache(_ENUM_TTL_SECONDS)
)


def get_cached_field_options(
    project_id: str,
    resource: Resource,
    field_id: str,
    type_id: str,
) -> Mapping[str, str] | None:
    """Cached option id → display name for field/type, or ``None`` on miss."""
    return _field_option_cache.get((project_id, resource, field_id, type_id))


def store_cached_field_options(  # noqa: PLR0913
    project_id: str,
    resource: Resource,
    field_id: str,
    type_id: str,
    options: Mapping[str, str],
    *,
    not_found: bool = False,
) -> None:
    """Cache option id → name for field/type; ``not_found=True`` (404 result)
    use longer ``_FIELD_OPTIONS_NOT_FOUND_TTL_SECONDS``.
    """
    # Store read-only view: ``TTLCache.get`` hand back stored object itself,
    # so plain dict would let one caller's mutation poison every later guard.
    _field_option_cache.set(
        (project_id, resource, field_id, type_id),
        MappingProxyType(dict(options)),
        ttl_seconds=_FIELD_OPTIONS_NOT_FOUND_TTL_SECONDS if not_found else None,
    )


# (project, enum_name) -> project-level enum option ids (no type axis).
_enum_option_id_cache: TTLCache[tuple[str, str], frozenset[str]] = TTLCache(
    _ENUM_TTL_SECONDS
)


def get_cached_enum_option_ids(
    project_id: str,
    enum_name: str,
) -> frozenset[str] | None:
    """Cached valid option ids for project enum, or ``None`` on miss."""
    return _enum_option_id_cache.get((project_id, enum_name))


def store_cached_enum_option_ids(
    project_id: str,
    enum_name: str,
    option_ids: frozenset[str],
) -> None:
    """Cache valid option ids for project enum for ``_ENUM_TTL_SECONDS``."""
    _enum_option_id_cache.set((project_id, enum_name), option_ids)


# (project, work_item_id) -> True once confirmed existing. Positives only --
# a missing WI may be created later, so absence never cache as a negative.
_confirmed_work_item_cache: TTLCache[tuple[str, str], bool] = TTLCache(
    _TARGET_TTL_SECONDS
)


def get_cached_confirmed_work_item(project_id: str, work_item_id: str) -> bool | None:
    """``True`` if ``(project, work_item)`` confirmed existing, else
    ``None`` -- ``None`` also cover expired/never-checked, both treated
    as a cache miss by callers.
    """
    return _confirmed_work_item_cache.get((project_id, work_item_id))


def store_cached_confirmed_work_item(project_id: str, work_item_id: str) -> None:
    """Mark ``(project, work_item)`` confirmed existing for
    ``_TARGET_TTL_SECONDS`` -- partial batches merge naturally since each
    id is its own entry.
    """
    _confirmed_work_item_cache.set((project_id, work_item_id), True)


# (project, work_item_type) -> full custom-field key schema (MIN-per-key sample).
_work_item_custom_key_cache: TTLCache[tuple[str, str], frozenset[str]] = TTLCache(
    _SCHEMA_TTL_SECONDS
)


def get_work_item_custom_keys(
    project_id: str,
    work_item_type: str,
) -> frozenset[str] | None:
    """Cached complete key schema for ``(project, type)``, or ``None``."""
    return _work_item_custom_key_cache.get((project_id, work_item_type))


def store_work_item_custom_keys(
    project_id: str,
    work_item_type: str,
    keys: frozenset[str],
) -> None:
    """Replace prior set — each sample = full key set, admin removal
    shrink schema on expiry.
    """
    _work_item_custom_key_cache.set((project_id, work_item_type), keys)


# (project, document_type) -> custom-field key schema; document-side mirror.
_document_type_custom_key_cache: TTLCache[tuple[str, str], frozenset[str]] = TTLCache(
    _SCHEMA_TTL_SECONDS
)


def get_document_type_custom_keys(
    project_id: str,
    document_type: str,
) -> frozenset[str] | None:
    """Cached key schema for ``(project, document_type)``, or ``None``."""
    return _document_type_custom_key_cache.get((project_id, document_type))


def store_document_type_custom_keys(
    project_id: str,
    document_type: str,
    keys: frozenset[str],
) -> None:
    """Replace prior set — each sample = full key set, admin removal
    shrink schema on expiry.
    """
    _document_type_custom_key_cache.set((project_id, document_type), keys)


# project -> testrun custom-field key schema (project config, no type axis).
_test_run_custom_key_cache: TTLCache[str, frozenset[str]] = TTLCache(
    _SCHEMA_TTL_SECONDS
)


def get_test_run_custom_keys(project_id: str) -> frozenset[str] | None:
    """Cached testrun custom-field key schema, or ``None`` on miss."""
    return _test_run_custom_key_cache.get(project_id)


def store_test_run_custom_keys(project_id: str, keys: frozenset[str]) -> None:
    """Replace prior set — each sample = full key set, admin removal
    shrink schema on expiry.
    """
    _test_run_custom_key_cache.set(project_id, keys)


__all__ = [
    "DiscoveredDocument",
    "Resource",
    "TTLCache",
    "get_cached_confirmed_work_item",
    "get_cached_documents",
    "get_cached_enum_option_ids",
    "get_cached_field_options",
    "get_document_type_custom_keys",
    "get_test_run_custom_keys",
    "get_work_item_custom_keys",
    "invalidate_documents_cache",
    "store_cached_confirmed_work_item",
    "store_cached_documents",
    "store_cached_enum_option_ids",
    "store_cached_field_options",
    "store_document_type_custom_keys",
    "store_test_run_custom_keys",
    "store_work_item_custom_keys",
]
