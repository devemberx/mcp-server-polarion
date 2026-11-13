"""Shared fixtures: tools called direct with mock ``PolarionClient``
injected via mock ``Context``.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from mcp_server_polarion.core.client import PolarionClient
from mcp_server_polarion.tools._shared import cache as _cache_mod


def _clear_guard_caches() -> None:
    """Drop enum / custom-field guard caches owned by ``_shared/cache.py``."""
    _cache_mod._field_option_cache.clear()
    _cache_mod._enum_option_id_cache.clear()
    _cache_mod._work_item_custom_key_cache.clear()
    _cache_mod._document_type_custom_key_cache.clear()
    _cache_mod._test_run_custom_key_cache.clear()
    _cache_mod._confirmed_work_item_cache.clear()


@pytest.fixture(autouse=True)
def _reset_guard_caches() -> None:
    """Cold guard caches per test — key primed by one test would leak into
    next and mask a missing priming GET.
    """
    _clear_guard_caches()


@pytest.fixture
def mock_client() -> AsyncMock:
    """Mock PolarionClient with async methods."""
    client = AsyncMock(spec=PolarionClient)
    # Default empty dict: unstubbed GET (e.g. enum-options probe a test
    # doesn't care about) then defer cleanly instead of returning nested
    # AsyncMock whose ``.get`` leak an unawaited coroutine.
    client.get = AsyncMock(return_value={})
    client.post = AsyncMock()
    client.patch = AsyncMock()
    client.delete = AsyncMock()
    return client


@pytest.fixture
def mock_ctx(mock_client: AsyncMock) -> MagicMock:
    """Mock FastMCP Context with the mock client."""
    ctx = MagicMock()
    ctx.lifespan_context = {
        "polarion_client": mock_client,
    }
    return ctx
