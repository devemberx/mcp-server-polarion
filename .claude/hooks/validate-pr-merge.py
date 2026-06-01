#!/usr/bin/env python3
"""PreToolUse hook: enforce squash-merge conventions on `gh pr merge`.

Triggered before every Bash tool call. Skips silently unless the command runs
`gh pr merge`. When it does, enforces the CLAUDE.md repo conventions so the
squash commit matches the standard commit-message format:

  1. **Squash merge only.** The merge must pass ``--squash`` / ``-s``. ``--merge``
     and ``--rebase`` are rejected.

  2. **No ``--subject`` override.** ``--subject`` / ``-t`` is rejected so the PR
     title (assumed already in commit-message format) becomes the squash subject
     verbatim. Per CLAUDE.md: "NEVER pass --subject to gh pr merge."

  3. **Explicit body in commit-message format.** A body must be supplied via
     ``--body`` / ``-b`` or ``--body-file`` / ``-F`` (gh's auto-generated body is
     not guaranteed to follow the format). The body is validated against the same
     rules as ``.githooks/commit-msg`` (the source of truth): exactly two ``- ``
     bullets, each <= 120 chars after trailing-whitespace strip, ``#`` lines
     ignored. The subject is the PR title and is assumed already valid, so only
     the body region is checked here.

  4. **Claude co-author trailer.** Because this hook only runs inside a Claude
     Code session, every merge it governs is Claude-assisted; the body must
     carry a ``Co-Authored-By: Claude ... <noreply@anthropic.com>`` trailer so
     the squash commit records that. The model version is not pinned.

Exit codes:
  0 = allow tool call (no violation, or command is not `gh pr merge`)
  2 = block tool call; stderr message reaches Claude as feedback
"""

from __future__ import annotations

import json
import re
import shlex
import sys
from pathlib import Path

PR_MERGE = ("pr", "merge")
BULLET_LIMIT = 120
REQUIRED_BULLETS = 2
CO_AUTHOR_RE = re.compile(
    r"^co-authored-by:\s*claude\b.*<noreply@anthropic\.com>\s*$",
    re.IGNORECASE | re.MULTILINE,
)


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

    try:
        argv = shlex.split(cmd)
    except ValueError:
        return 0
    if not is_pr_merge(argv):
        return 0

    errors: list[str] = []
    if has_subject(argv):
        errors.append(
            "gh pr merge must not pass --subject / -t. The PR title (assumed "
            "already in commit-message format) becomes the squash subject "
            "verbatim. Drop the --subject flag."
        )
    if not has_squash(argv):
        errors.append(
            "gh pr merge must use --squash. The repo is squash-merge only; "
            "--merge / --rebase / interactive are not allowed. Re-run with "
            "--squash."
        )

    found_body, body = extract_body(argv)
    if not found_body:
        errors.append(
            "gh pr merge must pass an explicit body via --body / -b (or "
            "--body-file / -F) so the squash body follows the commit-message "
            "format. gh's auto-generated body is not guaranteed to comply."
        )
    elif body is None:
        errors.append(
            "gh pr merge --body-file points at a file that could not be read."
        )
    else:
        errors.extend(body_format_errors(body))

    if errors:
        sys.stderr.write("BLOCKED by .claude/hooks/validate-pr-merge.py:\n\n")
        for e in errors:
            sys.stderr.write(f"* {e}\n\n")
        return 2

    return 0


def is_pr_merge(argv: list[str]) -> bool:
    """True when argv invokes `gh pr merge` (allowing flags between the words)."""
    positionals = [a for a in argv if not a.startswith("-")]
    try:
        i = positionals.index("gh")
    except ValueError:
        return False
    return tuple(positionals[i + 1 : i + 3]) == PR_MERGE


def has_squash(argv: list[str]) -> bool:
    return any(a == "--squash" or a == "-s" or a.startswith("--squash=") for a in argv)


def has_subject(argv: list[str]) -> bool:
    return any(
        a == "--subject"
        or a == "-t"
        or a.startswith("--subject=")
        or (a.startswith("-t") and len(a) > 2 and not a.startswith("--"))
        for a in argv
    )


def extract_body(argv: list[str]) -> tuple[bool, str | None]:
    """Pull the squash body out of the argv.

    Returns ``(found, content)``. ``found`` is True when a body flag is present;
    ``content`` is None when a --body-file cannot be read.
    """
    i = 0
    while i < len(argv):
        a = argv[i]
        nxt = argv[i + 1] if i + 1 < len(argv) else None

        if a in {"--body", "-b"} and nxt is not None:
            return True, nxt
        if a.startswith("--body="):
            return True, a[len("--body=") :]
        if a.startswith("-b") and len(a) > 2 and not a.startswith("--"):
            return True, a[len("-b") :]
        if a in {"--body-file", "-F"} and nxt is not None:
            return True, _read_file(nxt)
        if a.startswith("--body-file="):
            return True, _read_file(a[len("--body-file=") :])
        if a.startswith("-F") and len(a) > 2 and not a.startswith("--"):
            return True, _read_file(a[len("-F") :])
        i += 1
    return False, None


def _read_file(path: str) -> str | None:
    try:
        return Path(path).read_text()
    except OSError:
        return None


def body_format_errors(body: str) -> list[str]:
    """Validate the body against .githooks/commit-msg's body rules: exactly two
    ``- `` bullets, each <= 120 chars after trailing-whitespace strip. ``#`` lines
    are ignored, matching the commit-msg hook."""
    lines = [
        ln.rstrip()
        for ln in body.splitlines()
        if not ln.lstrip().startswith("#")
    ]
    bullets = [ln for ln in lines if ln.startswith("- ")]

    errors: list[str] = []
    if len(bullets) != REQUIRED_BULLETS:
        errors.append(
            f"Squash body must contain exactly {REQUIRED_BULLETS} bullets "
            f"(- ...), found {len(bullets)}. Match the commit-message format "
            "in .githooks/commit-msg / CONTRIBUTING.md (motivation, then change)."
        )
    over = [ln for ln in bullets if len(ln) > BULLET_LIMIT]
    for ln in over:
        errors.append(f"Squash body bullet is {len(ln)} chars (limit: {BULLET_LIMIT}):\n    {ln}")

    if not CO_AUTHOR_RE.search(body):
        errors.append(
            "Squash body must include a Claude co-author trailer so the merge "
            "records that it was done with Claude. Append a line like:\n"
            "    Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
        )
    return errors


if __name__ == "__main__":
    sys.exit(main())
