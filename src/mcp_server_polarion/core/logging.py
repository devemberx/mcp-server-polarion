"""Logging setup — all output goes to stderr (stdout is reserved for MCP)."""

from __future__ import annotations

import logging
import sys


def setup_logging(*, level: int = logging.INFO) -> logging.Logger:
    """Configure and return the package-level logger.

    A single ``StreamHandler(sys.stderr)`` is attached so that log
    messages never pollute the MCP JSON-RPC channel on stdout.

    Args:
        level: Logging level (default ``INFO``).

    Returns:
        The configured ``mcp_server_polarion`` logger.
    """
    logger = logging.getLogger("mcp_server_polarion")

    if not logger.handlers:
        handler = logging.StreamHandler(sys.stderr)
        handler.setFormatter(
            logging.Formatter(
                "%(asctime)s [%(levelname)s] %(name)s: %(message)s",
                datefmt="%Y-%m-%d %H:%M:%S",
            ),
        )
        logger.addHandler(handler)

    logger.setLevel(level)
    logger.propagate = False
    return logger
