"""Tests for the __main__ entry point module."""

from __future__ import annotations

from unittest.mock import patch

from mcp_server_polarion.__main__ import main


class TestMain:
    """Verify the CLI entry point."""

    def test_main_calls_mcp_run_with_stdio(self) -> None:
        with patch("mcp_server_polarion.__main__.mcp") as mock_mcp:
            main()
            mock_mcp.run.assert_called_once_with(transport="stdio")

    def test_main_returns_none(self) -> None:
        with patch("mcp_server_polarion.__main__.mcp"):
            result = main()
            assert result is None
