"""FastMCP server instance and lifespan tests."""

from __future__ import annotations

from unittest.mock import patch
from urllib.parse import urlparse

import pytest

from mcp_server_polarion.core.client import PolarionClient
from mcp_server_polarion.server import LifespanContext, _lifespan, mcp

_FAKE_ENV = {
    "POLARION_URL": "https://polarion.example.com",
    "POLARION_TOKEN": "test-token-secret",
}


class TestMcpInstance:
    """FastMCP instance configured correctly."""

    def test_server_name(self) -> None:
        assert mcp.name == "mcp-server-polarion"

    def test_server_has_instructions(self) -> None:
        assert mcp.instructions is not None

    def test_instructions_mention_polarion(self) -> None:
        assert mcp.instructions is not None
        assert "Polarion" in mcp.instructions


class TestLifespan:
    """Lifespan context manager."""

    async def test_lifespan_yields_polarion_client(self) -> None:
        with patch.dict("os.environ", _FAKE_ENV, clear=False):
            async with _lifespan(mcp) as ctx:
                assert "polarion_client" in ctx
                assert isinstance(ctx["polarion_client"], PolarionClient)

    async def test_lifespan_client_is_open_during_context(self) -> None:
        with patch.dict("os.environ", _FAKE_ENV, clear=False):
            async with _lifespan(mcp) as ctx:
                client = ctx["polarion_client"]
                assert not client.is_closed

    async def test_lifespan_client_is_closed_after_context(self) -> None:
        with patch.dict("os.environ", _FAKE_ENV, clear=False):
            async with _lifespan(mcp) as ctx:
                client = ctx["polarion_client"]

            assert client.is_closed

    async def test_lifespan_context_type(self) -> None:
        with patch.dict("os.environ", _FAKE_ENV, clear=False):
            async with _lifespan(mcp) as ctx:
                result: LifespanContext = ctx
                assert isinstance(result, dict)

    async def test_lifespan_calls_setup_logging(self) -> None:
        with (
            patch.dict("os.environ", _FAKE_ENV, clear=False),
            patch(
                "mcp_server_polarion.server.setup_logging",
            ) as mock_setup,
        ):
            async with _lifespan(mcp) as _ctx:
                mock_setup.assert_called_once()

    async def test_lifespan_logs_connecting_message(self) -> None:
        with patch.dict("os.environ", _FAKE_ENV, clear=False):
            with patch("mcp_server_polarion.server.logger") as mock_logger:
                async with _lifespan(mcp) as _ctx:
                    pass

            mock_logger.info.assert_any_call(
                "Connecting to Polarion at %s",
                _FAKE_ENV["POLARION_URL"],
            )

    async def test_lifespan_client_has_correct_base_url(self) -> None:
        with patch.dict("os.environ", _FAKE_ENV, clear=False):
            async with _lifespan(mcp) as ctx:
                client = ctx["polarion_client"]
                parsed = urlparse(str(client.base_url))
                assert parsed.netloc == "polarion.example.com"
                assert parsed.path == "/polarion/rest/v1"


class TestLifespanRateCap:
    """Env rate cap reach the lifespan-built client and get logged."""

    async def test_env_rate_cap_reaches_client_pacing(self) -> None:
        """End-to-end: env var → ``PolarionConfig`` → client interval."""
        env = {**_FAKE_ENV, "POLARION_MAX_REQUESTS_PER_SECOND": "2"}
        with patch.dict("os.environ", env, clear=False):
            async with _lifespan(mcp) as ctx:
                client = ctx["polarion_client"]
                assert client._min_interval == pytest.approx(0.5)

    async def test_lifespan_logs_effective_rate(self) -> None:
        env = {**_FAKE_ENV, "POLARION_MAX_REQUESTS_PER_SECOND": "2"}
        with patch.dict("os.environ", env, clear=False):
            with patch("mcp_server_polarion.server.logger") as mock_logger:
                async with _lifespan(mcp) as _ctx:
                    pass

            mock_logger.info.assert_any_call("Request rate cap: %g req/s", 2.0)

    async def test_lifespan_warns_when_pacing_disabled(self) -> None:
        env = {**_FAKE_ENV, "POLARION_MAX_REQUESTS_PER_SECOND": "0"}
        with patch.dict("os.environ", env, clear=False):
            async with _lifespan(mcp) as ctx:
                assert ctx["polarion_client"]._min_interval == 0.0

            with patch("mcp_server_polarion.server.logger") as mock_logger:
                async with _lifespan(mcp) as _ctx:
                    pass

            warning = mock_logger.warning.call_args[0][0]
            assert "pacing is DISABLED" in warning
