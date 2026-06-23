"""MCP tool definitions — domain-grouped tools for Polarion ALM.

Importing each module registers its ``@mcp.tool`` functions as a side effect.
"""

from __future__ import annotations

import mcp_server_polarion.tools.comments
import mcp_server_polarion.tools.documents
import mcp_server_polarion.tools.enum
import mcp_server_polarion.tools.links
import mcp_server_polarion.tools.moves
import mcp_server_polarion.tools.projects
import mcp_server_polarion.tools.work_items  # noqa: F401

# Intentionally empty: tools register via import side effect, not by name export.
__all__: list[str] = []
