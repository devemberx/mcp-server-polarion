"""Entry point for the MCP server — ``python -m mcp_server_polarion``."""

from __future__ import annotations

from mcp_server_polarion.server import mcp


def main() -> None:
    """Run the Polarion MCP server over stdio transport."""
    mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
