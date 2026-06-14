"""Shared fixtures: tools called directly with a mock ``PolarionClient``
injected via a mock ``Context``.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from mcp_server_polarion.core.client import PolarionClient
from mcp_server_polarion.tools._shared import cache as _cache_mod


def _clear_guard_caches() -> None:
    """Drop the enum / custom-field guard caches owned by ``_shared/cache.py``."""
    _cache_mod._enum_option_cache.clear()
    _cache_mod._project_enum_cache.clear()
    _cache_mod._work_item_custom_key_cache.clear()
    _cache_mod._document_type_custom_key_cache.clear()


@pytest.fixture(autouse=True)
def _reset_guard_caches() -> None:
    """Cold guard caches per test — a key primed by one test would leak into the
    next and mask a missing priming GET.
    """
    _clear_guard_caches()


@pytest.fixture
def mock_client() -> AsyncMock:
    """Return a mock PolarionClient with async methods."""
    client = AsyncMock(spec=PolarionClient)
    # Default to an empty dict: an unstubbed GET (e.g. an enum-options probe a
    # test doesn't care about) then defers cleanly instead of returning a nested
    # AsyncMock whose ``.get`` leaks an unawaited coroutine.
    client.get = AsyncMock(return_value={})
    client.post = AsyncMock()
    client.patch = AsyncMock()
    client.delete = AsyncMock()
    return client


@pytest.fixture
def mock_ctx(mock_client: AsyncMock) -> MagicMock:
    """Return a mock FastMCP Context with the mock client."""
    ctx = MagicMock()
    ctx.lifespan_context = {
        "polarion_client": mock_client,
    }
    return ctx
