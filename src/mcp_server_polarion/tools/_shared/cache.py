"""In-process TTL caches shared across tool implementations.

Near-static project facts (documents, enum option ids, custom-field key
schemas) are memoised to spare the server's tight budget (<=3 req/s, no
concurrency). This module owns all cache state; tool logic reaches it only
through the typed get / store wrappers below.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Final, Literal, NamedTuple


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

    def set(self, key: K, value: V, ttl_seconds: float | None = None) -> None:
        """Store *value*; *ttl_seconds* overrides the cache default for this entry."""
        ttl = self._ttl if ttl_seconds is None else ttl_seconds
        self._entries[key] = _Entry(expires_at=_now() + ttl, value=value)

    def invalidate(self, key: K) -> None:
        self._entries.pop(key, None)

    def clear(self) -> None:
        self._entries.clear()


Resource = Literal["workitems", "documents"]


class DiscoveredDocument(NamedTuple):
    """One document found by the ``list_documents`` discovery scan."""

    space_id: str
    document_name: str
    type: str = ""
    status: str = ""
    updated: str = ""
    author_name: str = ""
    updated_by_name: str = ""


# Bounded by ghost-safety: a stale entry keeps accepting an admin-removed
# option until expiry, so 60s caps that window.
_GUARD_TTL_SECONDS: Final[float] = 60.0

# 404 "not an Enumeration field" is a stable schema fact (field-type changes
# are rare admin ops) and its stale worst case is merely deferring to Polarion,
# so it outlives positive option sets, whose longer life would widen the
# admin-removed-option ghost window.
_ENUM_NOT_FOUND_TTL_SECONDS: Final[float] = 600.0

# Bounded by freshness: a new document must surface within ~1 min
# (``create_document`` also invalidates on write).
_DOCUMENT_LIST_TTL_SECONDS: Final[float] = 60.0

# project_id -> discovered documents in display order.
_document_list_cache: TTLCache[str, tuple[DiscoveredDocument, ...]] = TTLCache(
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
# (project, work_item_type) -> the type's full custom-field key schema (MIN-per-key
# sample). Sole source of truth for work-item custom-field validation.
_work_item_custom_key_cache: TTLCache[tuple[str, str], frozenset[str]] = TTLCache(
    _GUARD_TTL_SECONDS
)
# (project, document_type) -> the type's complete custom-field key schema, from
# the project-wide /documents sample. Mirrors the work-item cache; the single
# source of truth for document custom-field validation.
_document_type_custom_key_cache: TTLCache[tuple[str, str], frozenset[str]] = TTLCache(
    _GUARD_TTL_SECONDS
)


def get_cached_documents(project_id: str) -> list[DiscoveredDocument] | None:
    """Return the cached document list for *project_id* or ``None``."""
    cached = _document_list_cache.get(project_id)
    return list(cached) if cached is not None else None


def store_cached_documents(
    project_id: str,
    documents: list[DiscoveredDocument],
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


def store_cached_enum_options(  # noqa: PLR0913
    project_id: str,
    resource: Resource,
    field_id: str,
    type_id: str,
    option_ids: frozenset[str],
    *,
    not_found: bool = False,
) -> None:
    """Cache the valid option ids for the field/type.

    Entries live ``_GUARD_TTL_SECONDS``; ``not_found=True`` marks a 404
    ("not an Enumeration field") result, cached for
    ``_ENUM_NOT_FOUND_TTL_SECONDS`` instead.
    """
    _enum_option_cache.set(
        (project_id, resource, field_id, type_id),
        option_ids,
        ttl_seconds=_ENUM_NOT_FOUND_TTL_SECONDS if not_found else None,
    )


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


def get_work_item_custom_keys(
    project_id: str,
    work_item_type: str,
) -> frozenset[str] | None:
    """Return the cached complete key schema for ``(project, type)``, or ``None``."""
    return _work_item_custom_key_cache.get((project_id, work_item_type))


def store_work_item_custom_keys(
    project_id: str,
    work_item_type: str,
    keys: frozenset[str],
) -> None:
    """Replace any prior set: each sample is the full key set, so an admin
    removal shrinks the schema once the entry expires.
    """
    _work_item_custom_key_cache.set((project_id, work_item_type), keys)


def invalidate_work_item_custom_keys(project_id: str, work_item_type: str) -> None:
    """Drop the cached schema for ``(project, type)`` (used by the bypass-retry)."""
    _work_item_custom_key_cache.invalidate((project_id, work_item_type))


def get_document_type_custom_keys(
    project_id: str,
    document_type: str,
) -> frozenset[str] | None:
    """Return the cached key schema for ``(project, document_type)``, or ``None``."""
    return _document_type_custom_key_cache.get((project_id, document_type))


def store_document_type_custom_keys(
    project_id: str,
    document_type: str,
    keys: frozenset[str],
) -> None:
    """Replace any prior set: each sample is the full key set, so an admin
    removal shrinks the schema once the entry expires.
    """
    _document_type_custom_key_cache.set((project_id, document_type), keys)


def invalidate_document_type_custom_keys(project_id: str, document_type: str) -> None:
    """Drop the cached schema for ``(project, document_type)`` (bypass-retry)."""
    _document_type_custom_key_cache.invalidate((project_id, document_type))


__all__ = [
    "DiscoveredDocument",
    "Resource",
    "TTLCache",
    "get_cached_documents",
    "get_cached_enum_options",
    "get_cached_project_enum",
    "get_document_type_custom_keys",
    "get_work_item_custom_keys",
    "invalidate_document_type_custom_keys",
    "invalidate_documents_cache",
    "invalidate_work_item_custom_keys",
    "store_cached_documents",
    "store_cached_enum_options",
    "store_cached_project_enum",
    "store_document_type_custom_keys",
    "store_work_item_custom_keys",
]
