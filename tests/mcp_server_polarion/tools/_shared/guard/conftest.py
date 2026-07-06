"""Guard test fixtures: cold caches per test and a bare mock client.

Caches themselves are tested in ``../test_cache.py``.
"""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from mcp_server_polarion.core.client import PolarionClient
from mcp_server_polarion.tools._shared import cache as cache_mod


@pytest.fixture(autouse=True)
def _reset_caches() -> None:
    """Drop any cache state leaked from prior tests in the session."""
    cache_mod._enum_option_cache.clear()
    cache_mod._project_enum_cache.clear()
    cache_mod._work_item_custom_key_cache.clear()
    cache_mod._document_type_custom_key_cache.clear()
    cache_mod._test_run_custom_key_cache.clear()


@pytest.fixture
def mock_client() -> AsyncMock:
    client = AsyncMock(spec=PolarionClient)
    client.get = AsyncMock()
    return client
