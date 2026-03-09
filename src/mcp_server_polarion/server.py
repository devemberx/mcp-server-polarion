"""FastMCP server instance and lifespan management."""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastmcp import FastMCP

from mcp_server_polarion.core.client import PolarionClient
from mcp_server_polarion.core.config import PolarionConfig
from mcp_server_polarion.core.logging import setup_logging

logger = logging.getLogger("mcp_server_polarion.server")

type LifespanContext = dict[str, PolarionClient]


@asynccontextmanager
async def _lifespan(
    _server: FastMCP[LifespanContext],
) -> AsyncIterator[LifespanContext]:
    """Initialize and clean up shared resources for the MCP server.

    Creates a ``PolarionClient`` from environment configuration and
    makes it available to all tools via the lifespan context.

    Args:
        _server: The FastMCP server instance (provided automatically).

    Yields:
        A dict containing the ``polarion_client`` key mapped to an
        active ``PolarionClient`` instance.
    """
    setup_logging()
    config = PolarionConfig()  # type: ignore[call-arg]
    logger.info("Connecting to Polarion at %s", config.polarion_url)

    async with PolarionClient(config) as client:
        logger.info("Polarion client ready")
        yield {"polarion_client": client}

    logger.info("Polarion client closed")


mcp = FastMCP(
    name="mcp-server-polarion",
    instructions=(
        "MCP server for Polarion ALM. "
        "Read and write documents and work items in Polarion."
    ),
    lifespan=_lifespan,
)

# Register tool modules — must be at bottom to avoid circular imports.
import mcp_server_polarion.tools  # noqa: E402, F401
