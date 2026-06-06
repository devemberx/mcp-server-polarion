"""Tests for ``setup_logging`` — stderr handler, propagation, idempotency."""

from __future__ import annotations

import logging
import sys

from mcp_server_polarion.core.logging import setup_logging


class TestSetupLogging:
    """Verify logging configuration behaviour."""

    def test_returns_named_logger(self) -> None:
        logger = setup_logging()
        assert logger.name == "mcp_server_polarion"

    def test_handler_writes_to_stderr(self) -> None:
        logger = setup_logging()
        handler = logger.handlers[0]
        assert isinstance(handler, logging.StreamHandler)
        assert handler.stream is sys.stderr

    def test_propagation_disabled(self) -> None:
        logger = setup_logging()
        assert logger.propagate is False

    def test_default_level_is_info(self) -> None:
        logger = setup_logging()
        assert logger.level == logging.INFO

    def test_custom_level(self) -> None:
        logger = setup_logging(level=logging.DEBUG)
        assert logger.level == logging.DEBUG
        # Reset to default for other tests.
        setup_logging(level=logging.INFO)

    def test_idempotent_no_duplicate_handlers(self) -> None:
        """Calling ``setup_logging`` twice must not add a second handler."""
        logger = setup_logging()
        handler_count = len(logger.handlers)
        setup_logging()
        assert len(logger.handlers) == handler_count
