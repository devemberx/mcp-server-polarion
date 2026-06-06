"""Shared pytest fixtures for the MCP-server-polarion test suite."""

from __future__ import annotations

import importlib.util
from collections.abc import AsyncIterator
from pathlib import Path
from types import ModuleType

import pytest

from mcp_server_polarion.core.client import PolarionClient
from mcp_server_polarion.core.config import PolarionConfig


def load_module_from_path(path: Path, module_name: str) -> ModuleType:
    """Import a standalone script by file path.

    The tracked hooks (`.claude/hooks/`) and CI scripts (`.github/scripts/`) live
    outside the package and some use hyphenated names, so they can't be imported
    normally; their tests load them through here.
    """
    spec = importlib.util.spec_from_file_location(module_name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


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
