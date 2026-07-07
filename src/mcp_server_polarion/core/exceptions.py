"""Polarion API exceptions; each carry HTTP ``status_code`` for tool handlers."""

from __future__ import annotations


class PolarionError(Exception):
    """Base for Polarion REST API errors."""

    def __init__(self, message: str, *, status_code: int = 0) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.message = message


class PolarionAuthError(PolarionError):
    """HTTP 401/403 — expired or insufficient-scope bearer token."""


class PolarionNotFoundError(PolarionError):
    """HTTP 404 — invalid project ID, work-item ID, or document path."""
