"""Entry point — ``python -m mcp_server_polarion``."""

from __future__ import annotations

from mcp_server_polarion.server import mcp


def main() -> None:
    """Run Polarion MCP server over stdio."""
    mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
