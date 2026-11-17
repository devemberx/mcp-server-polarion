"""Shared fixtures: tools called direct with mock ``PolarionClient``
injected via mock ``Context``.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from mcp_server_polarion.core.client import PolarionClient
from mcp_server_polarion.tools._shared import cache as _cache_mod


@pytest.fixture(autouse=True)
def _reset_guard_caches() -> None:
    """Cold caches per test — key primed by one test would leak into next
    and mask a missing priming GET.
    """
    _cache_mod.clear_all_caches()


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
