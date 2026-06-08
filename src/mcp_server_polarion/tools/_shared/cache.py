"""In-process TTL caches shared across tool implementations.

Near-static project facts (documents, enum option ids, observed custom-field
keys) are memoised to spare the server's tight budget (<=3 req/s, no
concurrency). This module owns all cache state; tool logic reaches it only
through the typed get / store / record wrappers below.
"""

from __future__ import annotations

import time
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Final, Literal


def _now() -> float:
    """Monotonic clock seam; patched wholesale in tests to drive TTL expiry."""
    return time.monotonic()


@dataclass(frozen=True, slots=True)
class _Entry[V]:
    expires_at: float
    value: V


class TTLCache[K, V]:
    """Hashable-keyed cache whose entries expire ``ttl_seconds`` after a set.

    Single-threaded; lazy expiry (a key is dropped only when next accessed past
    its deadline), so a bounded key space never grows past that bound.
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

    def set(self, key: K, value: V) -> None:
        self._entries[key] = _Entry(expires_at=_now() + self._ttl, value=value)

    def invalidate(self, key: K) -> None:
        self._entries.pop(key, None)

    def clear(self) -> None:
        self._entries.clear()


Resource = Literal["workitems", "documents"]

# Bounded by ghost-safety: a stale entry keeps accepting an admin-removed
# option until expiry, so 60s caps that window.
_GUARD_TTL_SECONDS: Final[float] = 60.0

# Bounded by freshness: a new document must surface within ~1 min
# (``create_document`` also invalidates on write).
_DOCUMENT_LIST_TTL_SECONDS: Final[float] = 60.0

# project_id -> tuple of (space_id, document_name) pairs.
_document_list_cache: TTLCache[str, tuple[tuple[str, str], ...]] = TTLCache(
    _DOCUMENT_LIST_TTL_SECONDS
)
# (project, resource, field, type) -> valid option ids.
_enum_option_cache: TTLCache[tuple[str, Resource, str, str], frozenset[str]] = TTLCache(
    _GUARD_TTL_SECONDS
)
# (project, enum_name) -> option ids for a project-level enum (link/hyperlink
# role). No type axis: these are project config, not type-scoped.
_project_enum_cache: TTLCache[tuple[str, str], frozenset[str]] = TTLCache(
    _GUARD_TTL_SECONDS
)
# (project, work_item_type) -> custom-field keys seen on get_work_item.
_work_item_custom_key_cache: TTLCache[tuple[str, str], frozenset[str]] = TTLCache(
    _GUARD_TTL_SECONDS
)
# (project, space, document) -> custom-field keys seen on get_document.
_document_custom_key_cache: TTLCache[tuple[str, str, str], frozenset[str]] = TTLCache(
    _GUARD_TTL_SECONDS
)


def get_cached_documents(project_id: str) -> list[tuple[str, str]] | None:
    """Return the cached document list for *project_id* or ``None``."""
    cached = _document_list_cache.get(project_id)
    return list(cached) if cached is not None else None


def store_cached_documents(
    project_id: str,
    documents: list[tuple[str, str]],
) -> None:
    """Cache *documents* for *project_id* for ``_DOCUMENT_LIST_TTL_SECONDS``."""
    _document_list_cache.set(project_id, tuple(documents))


def invalidate_documents_cache(project_id: str) -> None:
    """Drop the cached document list for *project_id*, if any."""
    _document_list_cache.invalidate(project_id)


def get_cached_enum_options(
    project_id: str,
    resource: Resource,
    field_id: str,
    type_id: str,
) -> frozenset[str] | None:
    """Return cached valid option ids for the field/type, or ``None`` on miss."""
    return _enum_option_cache.get((project_id, resource, field_id, type_id))


def store_cached_enum_options(
    project_id: str,
    resource: Resource,
    field_id: str,
    type_id: str,
    option_ids: frozenset[str],
) -> None:
    """Cache the valid option ids for the field/type for ``_GUARD_TTL_SECONDS``."""
    _enum_option_cache.set((project_id, resource, field_id, type_id), option_ids)


def get_cached_project_enum(
    project_id: str,
    enum_name: str,
) -> frozenset[str] | None:
    """Return cached valid option ids for the project enum, or ``None`` on miss."""
    return _project_enum_cache.get((project_id, enum_name))


def store_cached_project_enum(
    project_id: str,
    enum_name: str,
    option_ids: frozenset[str],
) -> None:
    """Cache the valid option ids for the project enum for ``_GUARD_TTL_SECONDS``."""
    _project_enum_cache.set((project_id, enum_name), option_ids)


def _record_custom_keys[KT: tuple[str, ...]](
    cache: TTLCache[KT, frozenset[str]],
    key: KT,
    keys: Iterable[str],
) -> None:
    """Union *keys* (filtered to non-empty strings) into the cached set at *key*."""
    new_keys = frozenset(k for k in keys if isinstance(k, str) and k)
    cache.set(key, (cache.get(key) or frozenset()) | new_keys)


def get_work_item_custom_keys(
    project_id: str,
    work_item_type: str,
) -> frozenset[str] | None:
    """Return keys seen on this ``(project, work_item_type)``, or ``None``."""
    return _work_item_custom_key_cache.get((project_id, work_item_type))


def record_work_item_custom_field_keys(
    project_id: str,
    work_item_type: str,
    keys: Iterable[str],
) -> None:
    """Merge *keys* into the per-``(project, work_item_type)`` observed set.

    Called from ``get_work_item`` so a later ``update_work_item`` can validate
    ``custom_fields`` against ids the caller has actually seen. Empty *keys*
    still records the type as observed, so no inline fetch is needed later.
    """
    _record_custom_keys(_work_item_custom_key_cache, (project_id, work_item_type), keys)


def get_document_custom_keys(
    project_id: str,
    space_id: str,
    document_name: str,
) -> frozenset[str] | None:
    """Return keys seen on this ``(project, space, document)``, or ``None``."""
    return _document_custom_key_cache.get((project_id, space_id, document_name))


def record_document_custom_field_keys(
    project_id: str,
    space_id: str,
    document_name: str,
    keys: Iterable[str],
) -> None:
    """Merge *keys* into the per-``(project, space, document)`` observed set.

    Called from ``get_document`` so a later ``update_document`` can validate
    ``custom_fields`` against ids the caller has actually seen.
    """
    _record_custom_keys(
        _document_custom_key_cache, (project_id, space_id, document_name), keys
    )


__all__ = [
    "Resource",
    "TTLCache",
    "get_cached_documents",
    "get_cached_enum_options",
    "get_cached_project_enum",
    "get_document_custom_keys",
    "get_work_item_custom_keys",
    "invalidate_documents_cache",
    "record_document_custom_field_keys",
    "record_work_item_custom_field_keys",
    "store_cached_documents",
    "store_cached_enum_options",
    "store_cached_project_enum",
]
