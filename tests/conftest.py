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
    """Import standalone script by file path — hooks and CI scripts live
    outside the package (some hyphen-named), so normal import fail.
    """
    spec = importlib.util.spec_from_file_location(module_name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def polarion_config() -> PolarionConfig:
    """``PolarionConfig`` pointing at fake local URL."""
    return PolarionConfig(
        polarion_url="https://polarion.example.com",
        polarion_token="test-token-secret",
    )


@pytest.fixture
async def polarion_client(
    polarion_config: PolarionConfig,
) -> AsyncIterator[PolarionClient]:
    """Yield ``PolarionClient`` with write delay 0 — tests must not sleep."""
    async with PolarionClient(polarion_config, write_delay=0.0) as client:
        yield client
