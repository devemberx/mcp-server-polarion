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


class PolarionResponseTooLargeError(PolarionError):
    """Streamed GET aborted client-side — body crossed ``limit`` bytes
    before completing. Not a Polarion status code — server never asked.
    """

    def __init__(self, message: str, *, limit: int) -> None:
        super().__init__(message, status_code=0)
        self.limit = limit
