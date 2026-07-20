r"""Guard pyproject coverage ``exclude_lines`` vs false-positive block drops.

Bare ``\.\.\.`` pattern once matched literal ``...`` inside Field description
strings in tools/documents.py signatures — coverage block-exclusion dropped
whole update_document/create_document bodies from measurement (#212).
"""

from __future__ import annotations

import re
import tomllib
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
# Mirror [tool.coverage.run] source.
COVERAGE_SOURCE_DIRS = ("src/mcp_server_polarion", "evals")


def _exclude_patterns() -> list[re.Pattern[str]]:
    pyproject_text = (REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8")
    raw = tomllib.loads(pyproject_text)["tool"]["coverage"]["report"]["exclude_lines"]
    assert isinstance(raw, list)
    return [re.compile(pattern) for pattern in raw]


def _is_intended_exclusion(line: str) -> bool:
    # Whole-line construct per configured pattern; anything else = pattern
    # bleed into strings/comments/signatures.
    stripped = line.strip()
    return (
        stripped == "..."
        or "pragma: no cover" in stripped
        or stripped.startswith("if TYPE_CHECKING:")
        or stripped.startswith("raise NotImplementedError")
        or stripped.startswith("@overload")
    )


def test_exclude_patterns_match_only_intended_lines() -> None:
    patterns = _exclude_patterns()
    offenders: list[str] = []
    for source_dir in COVERAGE_SOURCE_DIRS:
        for path in sorted((REPO_ROOT / source_dir).rglob("*.py")):
            lines = path.read_text(encoding="utf-8").splitlines()
            for lineno, line in enumerate(lines, start=1):
                matched = any(p.search(line) for p in patterns)
                if matched and not _is_intended_exclusion(line):
                    rel = path.relative_to(REPO_ROOT)
                    offenders.append(f"{rel}:{lineno}: {line.strip()}")
    assert not offenders, (
        "exclude_lines pattern hits non-stub source lines — coverage silently "
        "drops their whole block from measurement:\n" + "\n".join(offenders)
    )


def test_ellipsis_pattern_statement_only() -> None:
    patterns = _exclude_patterns()
    stub_body = "        ..."
    # Verbatim offender line from update_document signature (#212).
    prose = '        description="Enable auto outline numbers (1, 1.1, ...).",'
    assert any(p.search(stub_body) for p in patterns)
    assert not any(p.search(prose) for p in patterns)
