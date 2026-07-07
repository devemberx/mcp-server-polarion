"""MCP tool definitions — domain-grouped tools for Polarion ALM.

Import of each module register its ``@mcp.tool``s as side effect.
"""

from __future__ import annotations

import mcp_server_polarion.tools.comments
import mcp_server_polarion.tools.documents
import mcp_server_polarion.tools.enum
import mcp_server_polarion.tools.links
import mcp_server_polarion.tools.moves
import mcp_server_polarion.tools.projects
import mcp_server_polarion.tools.recipes
import mcp_server_polarion.tools.test_runs
import mcp_server_polarion.tools.work_items  # noqa: F401

# Empty: tools register via import side effect, not name export.
__all__: list[str] = []
