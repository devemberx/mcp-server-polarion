"""Logging setup — all output goes to stderr (stdout is reserved for MCP)."""

from __future__ import annotations

import logging
import sys


def setup_logging(*, level: int = logging.INFO) -> logging.Logger:
    """Configure and return the package-level logger — single
    ``StreamHandler(sys.stderr)`` so log messages never pollute the MCP
    JSON-RPC channel on stdout."""
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
