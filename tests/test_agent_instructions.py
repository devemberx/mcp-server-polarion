"""Guard portable coding-agent instruction entry points."""

from __future__ import annotations

from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]


def test_claude_md_is_portable_agents_import() -> None:
    claude_md = REPO_ROOT / "CLAUDE.md"

    assert not claude_md.is_symlink()
    assert claude_md.read_text(encoding="utf-8") == "@AGENTS.md\n"
    assert (REPO_ROOT / "AGENTS.md").is_file()
