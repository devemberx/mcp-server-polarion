#!/usr/bin/env python3
"""SessionStart hook: auto-link untracked `.claude/sensitive-patterns.local`
into git worktree checkouts.

Pattern file untracked (private names) — `git worktree add` never carry it,
and a worktree session without it silently run sensitive-text guard off.
Link main checkout's file at session start; contributor without one =
no-op. Existing file/link never touched — local copy = deliberate,
dangling link = guard fail closed, no silent repair.

Exit always 0 — setup helper, never block session.
"""

from __future__ import annotations

import os
import re
import sys
from pathlib import Path

PATTERN_RELPATH = Path(".claude/sensitive-patterns.local")
GITDIR_RE = re.compile(r"^gitdir:\s*(.+?)\s*$")


def worktree_main_root(root: Path) -> Path | None:
    """Main checkout root when ``root`` is a linked worktree, else ``None``.

    Worktree `.git` = file `gitdir: <main>/.git/worktrees/<name>`; main
    checkout `.git` = directory; submodule gitdir (`.git/modules/...`)
    skipped.
    """
    git_marker = root / ".git"
    if not git_marker.is_file():
        return None
    try:
        match = GITDIR_RE.match(git_marker.read_text())
    except (OSError, UnicodeDecodeError):
        return None
    if match is None:
        return None
    gitdir = Path(match.group(1))
    if gitdir.parent.name != "worktrees" or gitdir.parent.parent.name != ".git":
        return None
    return gitdir.parent.parent.parent


def ensure_link(root: Path, main_root: Path) -> str | None:
    """Relative symlink to main checkout's pattern file; message on action."""
    source = main_root / PATTERN_RELPATH
    target = root / PATTERN_RELPATH
    if not source.is_file():
        return None
    if target.is_symlink() or target.exists():
        return None
    relative = os.path.relpath(source, target.parent)
    try:
        target.symlink_to(relative)
    except OSError as error:
        return f"sensitive-patterns auto-link failed: {error}"
    return f"Linked {PATTERN_RELPATH} from main checkout ({relative})."


def main() -> int:
    root = Path(os.environ.get("CLAUDE_PROJECT_DIR") or Path.cwd())
    main_root = worktree_main_root(root)
    if main_root is None:
        return 0
    message = ensure_link(root, main_root)
    if message is not None:
        sys.stdout.write(f"{message}\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
