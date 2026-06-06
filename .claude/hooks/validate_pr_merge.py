#!/usr/bin/env python3
"""PreToolUse hook: enforce squash-merge conventions on `gh pr merge`.

Triggered on: gh pr merge

Rules:
  1. Squash-only — --merge / --rebase are rejected.
  2. No --subject / -t — the PR title becomes the squash subject verbatim.
  3. Explicit body required — exactly two non-empty bullets, each <= 120 chars.
  4. Claude co-author trailer — body must include
     Co-Authored-By: Claude ... <noreply@anthropic.com>.

Exit 0 = allow, exit 2 = block.
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
            "Drop --subject / -t; the PR title becomes the squash subject verbatim."
        )
    if not has_squash(argv):
        errors.append("Re-run with --squash; --merge / --rebase are not allowed.")

    found_body, body = extract_body(argv)
    if not found_body:
        errors.append("Pass an explicit body via --body / -b or --body-file / -F.")
    elif body is None:
        errors.append("--body-file points at a file that could not be read.")
    else:
        errors.extend(body_format_errors(body))

    if errors:
        sys.stderr.write("BLOCKED by .claude/hooks/validate-pr-merge.py:\n\n")
        for e in errors:
            sys.stderr.write(f"* {e}\n\n")
        return 2

    return 0


def is_pr_merge(argv: list[str]) -> bool:
    positionals = [a for a in argv if not a.startswith("-")]
    return any(
        positionals[i] == "gh" and tuple(positionals[i + 1 : i + 3]) == PR_MERGE
        for i in range(len(positionals))
    )


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
    lines = [ln.rstrip() for ln in body.splitlines() if not ln.lstrip().startswith("#")]
    bullets = [ln for ln in lines if ln.startswith("- ")]

    errors: list[str] = []
    if len(bullets) != REQUIRED_BULLETS:
        errors.append(
            f"Body must contain exactly {REQUIRED_BULLETS} bullets, "
            f"found {len(bullets)}."
        )
    for ln in bullets:
        if len(ln) > BULLET_LIMIT:
            errors.append(
                f"Bullet is {len(ln)} chars (limit: {BULLET_LIMIT}):\n    {ln}"
            )

    if not CO_AUTHOR_RE.search(body):
        errors.append(
            "Body must include: Co-Authored-By: Claude ... <noreply@anthropic.com>"
        )
    return errors


if __name__ == "__main__":
    sys.exit(main())
