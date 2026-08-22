"""Fail-closed write-block builders: returned exception type, guidance
text, and the warning log emitted before blocking.
"""

from __future__ import annotations

import logging

import pytest

from mcp_server_polarion.core.exceptions import PolarionAuthError, PolarionError
from mcp_server_polarion.tools._shared.guard._errors import (
    unauthorized_write_block,
    unreachable_write_block,
)


@pytest.fixture(autouse=True)
def _propagate_package_logs(monkeypatch: pytest.MonkeyPatch) -> None:
    # setup_logging set propagate=False — caplog miss package logs;
    # re-enable propagation locally for order independence.
    monkeypatch.setattr(logging.getLogger("mcp_server_polarion"), "propagate", True)


class TestUnreachableWriteBlock:
    def test_returns_runtime_error_with_guidance(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        caplog.set_level("WARNING", logger="mcp_server_polarion.tools._shared.guard")

        exc = unreachable_write_block(
            "enum 'severity'", "P", PolarionError("backend down")
        )

        assert isinstance(exc, RuntimeError)
        message = str(exc)
        assert "enum 'severity'" in message
        assert "project 'P'" in message
        assert "backend down" in message
        assert "Refusing the write" in message
        assert any("blocking write" in r.message for r in caplog.records)


class TestUnauthorizedWriteBlock:
    def test_returns_permission_error_with_guidance(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        caplog.set_level("WARNING", logger="mcp_server_polarion.tools._shared.guard")

        exc = unauthorized_write_block(
            "link roles", "P", PolarionAuthError("denied by policy", status_code=403)
        )

        assert isinstance(exc, PermissionError)
        message = str(exc)
        assert "link roles" in message
        assert "project 'P'" in message
        assert "POLARION_TOKEN" in message
        assert "denied by policy" in message
        assert any("blocking write" in r.message for r in caplog.records)
