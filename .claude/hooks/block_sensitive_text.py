#!/usr/bin/env python3
"""PreToolUse hook: block outward Bash commands carrying private deployment
names (Polarion project/space/document ids) into PR/issue/commit/release text.

Pattern file `.claude/sensitive-patterns.local` — untracked, one regex per
line, `#` comments. Absent or empty file = allow all, so contributors
without a private deployment never hit this hook.

Scanned: whole command string + contents of message/body file flags
(--body-file/--notes-file/--file/-F, gh api field=@file). Outward commands
only — local grep/cat of sensitive names stay allowed.

Exit 0 = allow, exit 2 = block.
"""

from __future__ import annotations

import contextlib
import json
import re
import shlex
import sys
from pathlib import Path

PATTERN_PATH = Path(".claude/sensitive-patterns.local")

# gh gist/release included: both publish text. git push absent — push ship
# commits already guarded at git commit.
OUTWARD_RE = re.compile(
    r"\bgh\s+(?:pr|issue|api|release|gist)\b|\bgit\s+(?:commit|tag)\b"
)
# Next argv after these = file shipped as outward text. -F double duty:
# gh api field flag (field=@file) vs gh pr / git commit file shorthand.
FILE_FLAGS = frozenset({"--body-file", "--notes-file", "--file", "-F"})
FIELD_AT_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_-]*=@(.+)$")


def main() -> int:
    try:
        data = json.load(sys.stdin)
    except json.JSONDecodeError:
        return 0
    if data.get("tool_name") != "Bash":
        return 0
    cmd = (data.get("tool_input") or {}).get("command", "")
    if not isinstance(cmd, str) or not cmd:
        return 0
    if not outward(cmd):
        return 0

    patterns = load_patterns()
    if patterns is None:
        sys.stderr.write(
            "BLOCKED by .claude/hooks/block_sensitive_text.py:\n\n"
            f"* Pattern file {PATTERN_PATH} exists but is unreadable — "
            "fail closed. Fix permissions or remove the file.\n"
        )
        return 2

    hits = scan(cmd, patterns)
    if hits:
        sys.stderr.write("BLOCKED by .claude/hooks/block_sensitive_text.py:\n\n")
        for pattern in hits:
            sys.stderr.write(
                f"* Outward command text matches private pattern: {pattern}\n"
            )
        sys.stderr.write(
            "\nPrivate deployment names (project/space/document ids) must not"
            " appear in PR/issue/commit/release text. Reword generically,"
            " e.g. 'live testdrive project'.\n"
        )
        return 2
    return 0


def outward(cmd: str) -> bool:
    """Whether command publish text beyond the local checkout."""
    return OUTWARD_RE.search(cmd) is not None


def load_patterns() -> list[re.Pattern[str]] | None:
    """Compiled patterns; ``None`` = file unreadable (caller fail closed)."""
    if not PATTERN_PATH.exists():
        return []
    try:
        lines = PATTERN_PATH.read_text().splitlines()
    except OSError:
        return None
    patterns: list[re.Pattern[str]] = []
    for line in lines:
        text = line.strip()
        if not text or text.startswith("#"):
            continue
        try:
            patterns.append(re.compile(text))
        except re.error:
            # Broken regex still guard as literal — never silently drop.
            patterns.append(re.compile(re.escape(text)))
    return patterns


def scan(cmd: str, patterns: list[re.Pattern[str]]) -> list[str]:
    """Matched pattern strings across command + referenced text files."""
    texts = [cmd, *referenced_file_texts(cmd)]
    return [p.pattern for p in patterns if any(p.search(t) for t in texts)]


def referenced_file_texts(cmd: str) -> list[str]:
    """Contents of files the command ship as text; unreadable skipped."""
    try:
        argv = shlex.split(cmd)
    except ValueError:
        return []
    texts: list[str] = []
    for i, arg in enumerate(argv):
        nxt = argv[i + 1] if i + 1 < len(argv) else None
        path: str | None = None
        if arg in FILE_FLAGS and nxt is not None:
            field_at = FIELD_AT_RE.match(nxt)
            if field_at:
                path = field_at.group(1)
            elif "=" not in nxt:
                # gh api -F field=inline = no file; bare next arg = file path.
                path = nxt
        elif arg.split("=", 1)[0] in FILE_FLAGS and "=" in arg:
            path = arg.split("=", 1)[1]
        if path is not None:
            with contextlib.suppress(OSError):
                texts.append(Path(path).read_text())
    return texts


if __name__ == "__main__":
    sys.exit(main())
