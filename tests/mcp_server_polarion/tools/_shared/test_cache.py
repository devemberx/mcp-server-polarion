"""``TTLCache`` + typed wrapper tests; expiry driven by patching the
module-level ``_now`` clock seam."""

from __future__ import annotations

import pytest

from mcp_server_polarion.tools._shared import cache as cache_mod
from mcp_server_polarion.tools._shared.cache import (
    DiscoveredDocument,
    TTLCache,
    get_cached_documents,
    get_cached_enum_options,
    get_cached_project_enum,
    get_document_type_custom_keys,
    get_work_item_custom_keys,
    invalidate_document_type_custom_keys,
    invalidate_documents_cache,
    invalidate_work_item_custom_keys,
    store_cached_documents,
    store_cached_enum_options,
    store_cached_project_enum,
    store_document_type_custom_keys,
    store_work_item_custom_keys,
)


@pytest.fixture(autouse=True)
def _reset_caches() -> None:
    """Start each test with every module-level cache cold."""
    cache_mod._document_list_cache.clear()
    cache_mod._enum_option_cache.clear()
    cache_mod._project_enum_cache.clear()
    cache_mod._work_item_custom_key_cache.clear()
    cache_mod._document_type_custom_key_cache.clear()


@pytest.fixture
def clock(monkeypatch: pytest.MonkeyPatch) -> list[float]:
    """Patch the cache clock to a controllable list-wrapped value."""
    now = [1000.0]
    monkeypatch.setattr(cache_mod, "_now", lambda: now[0])
    return now


class TestTTLCachePrimitive:
    """Behaviour of the generic ``TTLCache`` independent of any wrapper."""

    def test_miss_returns_none(self) -> None:
        cache: TTLCache[str, int] = TTLCache(60.0)

        assert cache.get("absent") is None

    def test_set_then_get_hits(self) -> None:
        cache: TTLCache[str, int] = TTLCache(60.0)
        cache.set("k", 7)

        assert cache.get("k") == 7

    def test_set_overwrites_value_and_resets_deadline(self, clock: list[float]) -> None:
        cache: TTLCache[str, int] = TTLCache(60.0)
        cache.set("k", 1)
        clock[0] += 30.0
        cache.set("k", 2)
        clock[0] += 40.0  # 70s since first set, only 40s since second

        assert cache.get("k") == 2

    def test_entry_expires_after_ttl(self, clock: list[float]) -> None:
        cache: TTLCache[str, int] = TTLCache(60.0)
        cache.set("k", 1)

        clock[0] += 61.0
        assert cache.get("k") is None

    def test_per_entry_ttl_overrides_cache_default(self, clock: list[float]) -> None:
        cache: TTLCache[str, int] = TTLCache(60.0)
        cache.set("long", 1, ttl_seconds=600.0)
        cache.set("short", 2)

        clock[0] += 61.0  # past default TTL, within the override
        assert cache.get("long") == 1
        assert cache.get("short") is None

        clock[0] += 600.0  # past the override too
        assert cache.get("long") is None

    def test_entry_live_at_exactly_below_ttl(self, clock: list[float]) -> None:
        cache: TTLCache[str, int] = TTLCache(60.0)
        cache.set("k", 1)

        clock[0] += 59.9
        assert cache.get("k") == 1

    def test_expiry_is_inclusive_at_deadline(self, clock: list[float]) -> None:
        cache: TTLCache[str, int] = TTLCache(60.0)
        cache.set("k", 1)

        clock[0] += 60.0  # _now >= expires_at drops the entry
        assert cache.get("k") is None

    def test_invalidate_drops_one_key(self) -> None:
        cache: TTLCache[str, int] = TTLCache(60.0)
        cache.set("a", 1)
        cache.set("b", 2)

        cache.invalidate("a")

        assert cache.get("a") is None
        assert cache.get("b") == 2

    def test_invalidate_absent_key_is_a_no_op(self) -> None:
        cache: TTLCache[str, int] = TTLCache(60.0)

        cache.invalidate("absent")  # must not raise

    def test_clear_drops_everything(self) -> None:
        cache: TTLCache[str, int] = TTLCache(60.0)
        cache.set("a", 1)
        cache.set("b", 2)

        cache.clear()

        assert cache.get("a") is None
        assert cache.get("b") is None


class TestDocumentListCache:
    """The mutable document-discovery listing wrappers."""

    def test_store_then_get_returns_a_list_copy(self) -> None:
        doc = DiscoveredDocument("_default", "Doc")
        store_cached_documents("P", [doc])

        cached = get_cached_documents("P")
        assert cached == [doc]
        # A mutation of the returned list must not corrupt the cache.
        assert cached is not None
        cached.append(DiscoveredDocument("_default", "Other"))
        assert get_cached_documents("P") == [doc]

    def test_miss_returns_none(self) -> None:
        assert get_cached_documents("absent") is None

    def test_invalidate_drops_the_project(self) -> None:
        store_cached_documents("P", [DiscoveredDocument("_default", "Doc")])

        invalidate_documents_cache("P")

        assert get_cached_documents("P") is None

    def test_expiry_uses_document_list_ttl(self, clock: list[float]) -> None:
        store_cached_documents("P", [DiscoveredDocument("_default", "Doc")])

        clock[0] += cache_mod._DOCUMENT_LIST_TTL_SECONDS + 1.0
        assert get_cached_documents("P") is None


class TestEnumOptionCache:
    """Enum-option id wrappers keyed by (project, resource, field, type)."""

    def test_store_then_get_hits(self) -> None:
        store_cached_enum_options(
            "P", "workitems", "severity", "task", frozenset({"high", "low"})
        )

        assert get_cached_enum_options("P", "workitems", "severity", "task") == (
            frozenset({"high", "low"})
        )

    def test_keys_are_distinct_per_axis(self) -> None:
        store_cached_enum_options(
            "P", "workitems", "severity", "task", frozenset({"high"})
        )

        assert get_cached_enum_options("P", "workitems", "severity", "bug") is None
        assert get_cached_enum_options("P", "documents", "severity", "task") is None
        assert get_cached_enum_options("P", "workitems", "status", "task") is None

    def test_expiry_uses_guard_ttl(self, clock: list[float]) -> None:
        store_cached_enum_options(
            "P", "workitems", "severity", "task", frozenset({"high"})
        )

        clock[0] += cache_mod._GUARD_TTL_SECONDS + 1.0
        assert get_cached_enum_options("P", "workitems", "severity", "task") is None

    def test_not_found_entries_use_long_ttl(self, clock: list[float]) -> None:
        store_cached_enum_options(
            "P", "workitems", "freeText", "task", frozenset(), not_found=True
        )

        clock[0] += cache_mod._GUARD_TTL_SECONDS + 1.0
        assert get_cached_enum_options("P", "workitems", "freeText", "task") == (
            frozenset()
        )

        clock[0] += cache_mod._ENUM_NOT_FOUND_TTL_SECONDS
        assert get_cached_enum_options("P", "workitems", "freeText", "task") is None


class TestProjectEnumCache:
    """Project-level enum wrappers keyed by (project, enum_name)."""

    def test_store_then_get_hits(self) -> None:
        store_cached_project_enum(
            "P", "workitem-link-role", frozenset({"parent", "relates_to"})
        )

        assert get_cached_project_enum("P", "workitem-link-role") == frozenset(
            {"parent", "relates_to"}
        )

    def test_keys_are_distinct_per_enum_and_project(self) -> None:
        store_cached_project_enum("P", "workitem-link-role", frozenset({"parent"}))

        assert get_cached_project_enum("P", "hyperlink-role") is None
        assert get_cached_project_enum("Q", "workitem-link-role") is None

    def test_expiry_uses_guard_ttl(self, clock: list[float]) -> None:
        store_cached_project_enum("P", "hyperlink-role", frozenset({"ref_ext"}))

        clock[0] += cache_mod._GUARD_TTL_SECONDS + 1.0
        assert get_cached_project_enum("P", "hyperlink-role") is None


class TestWorkItemCustomKeys:
    """``store/get/invalidate_work_item_custom_keys`` — the type's key schema."""

    def test_store_then_get(self) -> None:
        store_work_item_custom_keys("P", "task", frozenset({"a", "b"}))

        assert get_work_item_custom_keys("P", "task") == frozenset({"a", "b"})

    def test_store_replaces_verbatim_not_union(self) -> None:
        store_work_item_custom_keys("P", "task", frozenset({"a", "b"}))
        store_work_item_custom_keys("P", "task", frozenset({"c"}))

        assert get_work_item_custom_keys("P", "task") == frozenset({"c"})

    def test_keyed_by_type(self) -> None:
        store_work_item_custom_keys("P", "task", frozenset({"a"}))

        assert get_work_item_custom_keys("P", "requirement") is None
        assert get_work_item_custom_keys("Q", "task") is None

    def test_miss_returns_none(self) -> None:
        assert get_work_item_custom_keys("P", "never_sampled") is None

    def test_invalidate(self) -> None:
        store_work_item_custom_keys("P", "task", frozenset({"a"}))
        invalidate_work_item_custom_keys("P", "task")

        assert get_work_item_custom_keys("P", "task") is None

    def test_expiry(self, clock: list[float]) -> None:
        store_work_item_custom_keys("P", "task", frozenset({"a"}))

        clock[0] += cache_mod._GUARD_TTL_SECONDS + 1.0
        assert get_work_item_custom_keys("P", "task") is None


class TestDocumentTypeCustomKeys:
    """``store/get/invalidate_document_type_custom_keys`` keyed by (project, type)."""

    def test_store_then_get(self) -> None:
        store_document_type_custom_keys(
            "P", "softwareReqSpecification", frozenset({"v"})
        )

        assert get_document_type_custom_keys("P", "softwareReqSpecification") == (
            frozenset({"v"})
        )

    def test_store_replaces_verbatim_not_union(self) -> None:
        store_document_type_custom_keys("P", "generic", frozenset({"a", "b"}))
        store_document_type_custom_keys("P", "generic", frozenset({"c"}))

        assert get_document_type_custom_keys("P", "generic") == frozenset({"c"})

    def test_keyed_by_type(self) -> None:
        store_document_type_custom_keys("P", "generic", frozenset({"a"}))

        assert get_document_type_custom_keys("P", "systemReqSpecification") is None
        assert get_document_type_custom_keys("Q", "generic") is None

    def test_invalidate(self) -> None:
        store_document_type_custom_keys("P", "generic", frozenset({"a"}))
        invalidate_document_type_custom_keys("P", "generic")

        assert get_document_type_custom_keys("P", "generic") is None

    def test_expiry(self, clock: list[float]) -> None:
        store_document_type_custom_keys("P", "generic", frozenset({"a"}))

        clock[0] += cache_mod._GUARD_TTL_SECONDS + 1.0
        assert get_document_type_custom_keys("P", "generic") is None
