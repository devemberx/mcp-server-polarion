"""Tests for Polarion domain exceptions — hierarchy, attributes, messages."""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager

from mcp_server_polarion.core.exceptions import (
    PolarionAuthError,
    PolarionError,
    PolarionNotFoundError,
)


class TestExceptionHierarchy:
    """Verify that subclasses inherit from ``PolarionError``."""

    def test_auth_error_is_polarion_error(self) -> None:
        assert issubclass(PolarionAuthError, PolarionError)

    def test_not_found_error_is_polarion_error(self) -> None:
        assert issubclass(PolarionNotFoundError, PolarionError)

    def test_polarion_error_is_exception(self) -> None:
        assert issubclass(PolarionError, Exception)

    def test_catch_base_catches_auth(self) -> None:
        exc = PolarionAuthError("auth fail", status_code=401)
        assert isinstance(exc, PolarionError)

    def test_catch_base_catches_not_found(self) -> None:
        exc = PolarionNotFoundError("missing", status_code=404)
        assert isinstance(exc, PolarionError)


class TestExceptionAttributes:
    """Verify ``status_code`` and ``message`` on each exception."""

    def test_base_error_defaults(self) -> None:
        exc = PolarionError("something broke")
        assert exc.status_code == 0
        assert exc.message == "something broke"
        assert str(exc) == "something broke"

    def test_base_error_with_status_code(self) -> None:
        exc = PolarionError("server error", status_code=500)
        assert exc.status_code == 500

    def test_auth_error_preserves_status(self) -> None:
        exc = PolarionAuthError("forbidden", status_code=403)
        assert exc.status_code == 403
        assert exc.message == "forbidden"

    def test_not_found_error_preserves_status(self) -> None:
        exc = PolarionNotFoundError("no item", status_code=404)
        assert exc.status_code == 404
        assert exc.message == "no item"


class TestExceptionRaiseAndCatch:
    """Verify raise / except patterns used by tool code."""

    def test_raise_auth_catch_base(self) -> None:
        with _raises_polarion_error(PolarionAuthError):
            raise PolarionAuthError("bad token", status_code=401)

    def test_raise_not_found_catch_base(self) -> None:
        with _raises_polarion_error(PolarionNotFoundError):
            raise PolarionNotFoundError("gone", status_code=404)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


@contextmanager
def _raises_polarion_error(
    expected_type: type[PolarionError],
) -> Iterator[None]:
    """Assert that PolarionError is raised and is of *expected_type*."""
    try:
        yield
    except PolarionError as exc:
        assert type(exc) is expected_type
    else:
        msg = f"Expected {expected_type.__name__} to be raised"
        raise AssertionError(msg)
