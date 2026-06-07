"""Tests for ``tools/_shared/cache.py``.

Covers the ``TTLCache`` primitive (hit / miss / overwrite / lazy expiry /
invalidate / clear) and the typed get / store / record wrappers the tool
layer reaches the caches through. TTL expiry is driven by patching the
module-level ``_now`` clock seam.
"""

from __future__ import annotations

import pytest

from mcp_server_polarion.tools._shared import cache as cache_mod
from mcp_server_polarion.tools._shared.cache import (
    TTLCache,
    get_cached_documents,
    get_cached_enum_options,
    get_cached_project_enum,
    get_document_custom_keys,
    get_work_item_custom_keys,
    invalidate_documents_cache,
    record_document_custom_field_keys,
    record_work_item_custom_field_keys,
    store_cached_documents,
    store_cached_enum_options,
    store_cached_project_enum,
)


@pytest.fixture(autouse=True)
def _reset_caches() -> None:
    """Start each test with every module-level cache cold."""
    cache_mod._document_list_cache.clear()
    cache_mod._enum_option_cache.clear()
    cache_mod._project_enum_cache.clear()
    cache_mod._work_item_custom_key_cache.clear()
    cache_mod._document_custom_key_cache.clear()


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
        store_cached_documents("P", [("_default", "Doc")])

        cached = get_cached_documents("P")
        assert cached == [("_default", "Doc")]
        # A mutation of the returned list must not corrupt the cache.
        assert cached is not None
        cached.append(("_default", "Other"))
        assert get_cached_documents("P") == [("_default", "Doc")]

    def test_miss_returns_none(self) -> None:
        assert get_cached_documents("absent") is None

    def test_invalidate_drops_the_project(self) -> None:
        store_cached_documents("P", [("_default", "Doc")])

        invalidate_documents_cache("P")

        assert get_cached_documents("P") is None

    def test_expiry_uses_document_list_ttl(self, clock: list[float]) -> None:
        store_cached_documents("P", [("_default", "Doc")])

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


class TestWorkItemCustomKeyRecord:
    """``record_work_item_custom_field_keys`` union semantics + reads."""

    def test_record_then_get(self) -> None:
        record_work_item_custom_field_keys("P", "task", ["risk"])

        assert get_work_item_custom_keys("P", "task") == frozenset({"risk"})

    def test_record_merges_across_calls(self) -> None:
        record_work_item_custom_field_keys("P", "task", ["k1"])
        record_work_item_custom_field_keys("P", "task", ["k2", "k1"])

        assert get_work_item_custom_keys("P", "task") == frozenset({"k1", "k2"})

    def test_record_filters_non_string_and_empty(self) -> None:
        record_work_item_custom_field_keys("P", "task", ["k1", "", "k2"])

        assert get_work_item_custom_keys("P", "task") == frozenset({"k1", "k2"})

    def test_empty_keys_still_records_the_type_as_observed(self) -> None:
        record_work_item_custom_field_keys("P", "task", [])

        assert get_work_item_custom_keys("P", "task") == frozenset()

    def test_miss_returns_none(self) -> None:
        assert get_work_item_custom_keys("P", "never_seen") is None

    def test_expiry(self, clock: list[float]) -> None:
        record_work_item_custom_field_keys("P", "task", ["k1"])

        clock[0] += cache_mod._GUARD_TTL_SECONDS + 1.0
        assert get_work_item_custom_keys("P", "task") is None


class TestDocumentCustomKeyRecord:
    """``record_document_custom_field_keys`` keyed by (project, space, doc)."""

    def test_record_then_get(self) -> None:
        record_document_custom_field_keys("P", "_default", "Doc", ["doc_risk"])

        assert get_document_custom_keys("P", "_default", "Doc") == (
            frozenset({"doc_risk"})
        )

    def test_keyed_by_space_and_document(self) -> None:
        record_document_custom_field_keys("P", "_default", "Doc", ["doc_risk"])

        assert get_document_custom_keys("P", "other_space", "Doc") is None
        assert get_document_custom_keys("P", "_default", "Other") is None

    def test_record_merges_across_calls(self) -> None:
        record_document_custom_field_keys("P", "_default", "Doc", ["a"])
        record_document_custom_field_keys("P", "_default", "Doc", ["b"])

        assert get_document_custom_keys("P", "_default", "Doc") == frozenset({"a", "b"})
