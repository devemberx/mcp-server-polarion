"""In-process TTL caches shared across tool implementations.

Tools repeatedly resolve the same near-static project facts within a single
session -- the documents in a project, the valid option ids for an enum
field, the custom-field keys observed on a work item or document. Re-fetching
each on every call would burn the server's tight request budget (<=3 req/s,
no client-side concurrency), so each is memoised behind a short-lived cache.

This module owns all cache state. Tool logic (request shaping, JSON:API
extraction, write guards) lives elsewhere and reaches the caches only through
the typed get / store / record wrappers below, keeping cache lifetime and
key shape in one place.
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

    Single-process and single-threaded (the server issues no concurrent
    requests). Expiry is lazy -- a key is dropped only when next accessed past
    its deadline -- so a bounded key space never accumulates dead entries
    beyond that bound.
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

# Enum options and custom-field schema are project config, near-static within
# a session. The binding TTL constraint is not freshness but the ghost-safety
# window: if an admin removes an option mid-session, a stale entry keeps
# accepting the now-invalid value until expiry. 60s keeps that window short
# while staying well inside the <=3 req/s budget.
_GUARD_TTL_SECONDS: Final[float] = 60.0

# The document listing is mutable: a freshly created document must surface
# within ~1 minute, and ``create_document`` invalidates the entry on write.
# Same ceiling, different reason -- freshness rather than ghost-safety.
_DOCUMENT_LIST_TTL_SECONDS: Final[float] = 60.0

# project_id -> tuple of (space_id, document_name) pairs.
_document_list_cache: TTLCache[str, tuple[tuple[str, str], ...]] = TTLCache(
    _DOCUMENT_LIST_TTL_SECONDS
)
# (project, resource, field, type) -> valid option ids.
_enum_option_cache: TTLCache[tuple[str, Resource, str, str], frozenset[str]] = TTLCache(
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
    "get_document_custom_keys",
    "get_work_item_custom_keys",
    "invalidate_documents_cache",
    "record_document_custom_field_keys",
    "record_work_item_custom_field_keys",
    "store_cached_documents",
    "store_cached_enum_options",
]
