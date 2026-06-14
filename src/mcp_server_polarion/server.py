"""FastMCP server instance and lifespan management."""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import TypedDict

from fastmcp import FastMCP

from mcp_server_polarion.core.client import PolarionClient
from mcp_server_polarion.core.config import PolarionConfig
from mcp_server_polarion.core.logging import setup_logging
from mcp_server_polarion.middleware import CompactValidationErrorMiddleware

logger = logging.getLogger("mcp_server_polarion.server")


class LifespanContext(TypedDict):
    """Typed context yielded by the server lifespan."""

    polarion_client: PolarionClient


@asynccontextmanager
async def _lifespan(
    _server: FastMCP[LifespanContext],
) -> AsyncIterator[LifespanContext]:
    """Open one shared ``PolarionClient`` for all tools; close on shutdown."""
    setup_logging()
    config = PolarionConfig()  # type: ignore[call-arg]
    logger.info("Connecting to Polarion at %s", config.polarion_url)
    if not config.polarion_verify_ssl:
        logger.warning(
            "TLS certificate verification is DISABLED (POLARION_VERIFY_SSL=false). "
            "Connections to %s are vulnerable to MITM. Use only on trusted networks.",
            config.polarion_url,
        )

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
mcp.add_middleware(CompactValidationErrorMiddleware())

# Register tool modules — must be at bottom to avoid circular imports.
import mcp_server_polarion.tools  # noqa: E402, F401
