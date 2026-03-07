"""Shared pytest fixtures for the MCP-server-polarion test suite."""

from __future__ import annotations

from collections.abc import AsyncIterator

import pytest

from mcp_server_polarion.core.client import PolarionClient
from mcp_server_polarion.core.config import PolarionConfig


@pytest.fixture
def polarion_config() -> PolarionConfig:
    """Return a ``PolarionConfig`` pointing at a fake local URL."""
    return PolarionConfig(
        polarion_url="https://polarion.example.com",
        polarion_token="test-token-secret",
    )


@pytest.fixture
async def polarion_client(
    polarion_config: PolarionConfig,
) -> AsyncIterator[PolarionClient]:
    """Yield a ``PolarionClient`` with a near-zero write delay.

    The write delay is set to 0 so tests do not sleep unnecessarily.
    """
    async with PolarionClient(polarion_config, write_delay=0.0) as client:
        yield client
