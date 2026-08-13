"""Mechanical gate: auth failures reach the model through one builder.

Hand-rolled ``PermissionError`` drop the Polarion detail, and 403 detail is
the only thing separating a token problem from a workflow lock — drift must
fail CI, not a live smoke test.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

import mcp_server_polarion

_TOOLS_DIR = Path(mcp_server_polarion.__file__).parent / "tools"
# Builders = only place PermissionError constructed.
_BUILDERS = {
    _TOOLS_DIR / "_shared" / "errors.py",
    _TOOLS_DIR / "_shared" / "guard" / "_errors.py",
}
_RAW_PERMISSION_ERROR = re.compile(r"\bPermissionError\(")
_AUTH_HANDLER = re.compile(
    r"except PolarionAuthError as exc:\n(?P<body>(?:[^\n]*\n){1,6})"
)


def _tool_sources() -> list[Path]:
    return sorted(p for p in _TOOLS_DIR.rglob("*.py") if p not in _BUILDERS)


@pytest.mark.parametrize("path", _tool_sources(), ids=lambda p: p.name)
def test_no_hand_rolled_permission_error(path: Path) -> None:
    assert not _RAW_PERMISSION_ERROR.search(path.read_text()), (
        f"{path.name} constructs PermissionError directly -- raise "
        "`auth_error(...)` from tools/_shared/errors.py so the Polarion detail "
        "survives."
    )


@pytest.mark.parametrize("path", _tool_sources(), ids=lambda p: p.name)
def test_auth_handlers_call_auth_error(path: Path) -> None:
    for match in _AUTH_HANDLER.finditer(path.read_text()):
        assert "auth_error(" in match.group("body"), (
            f"{path.name} handles PolarionAuthError without auth_error(): "
            f"{match.group('body').strip()}"
        )
