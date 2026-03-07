"""Domain-specific exceptions for Polarion API errors.

Each exception carries the HTTP ``status_code`` so that tool-level error
handlers can produce actionable messages for the LLM.
"""

from __future__ import annotations


class PolarionError(Exception):
    """Base exception for all Polarion REST API errors.

    Attributes:
        status_code: The HTTP status code returned by the Polarion server.
        message: A human-readable error message.
    """

    def __init__(self, message: str, *, status_code: int = 0) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.message = message


class PolarionAuthError(PolarionError):
    """Raised on HTTP 401 (Unauthorized) or 403 (Forbidden).

    Typical cause: expired or insufficient-scope bearer token.
    """


class PolarionNotFoundError(PolarionError):
    """Raised on HTTP 404 (Not Found).

    Typical cause: invalid project ID, work-item ID, or document path.
    """
