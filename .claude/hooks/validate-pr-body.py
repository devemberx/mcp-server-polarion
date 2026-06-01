#!/usr/bin/env python3
"""PreToolUse hook: block PR/issue body Bash invocations that violate repo conventions.

Triggered before every Bash tool call. Skips silently unless the command is one of:
  - gh pr (create|edit)
  - gh issue (create|edit|comment)
  - gh pr comment
  - gh api ... /pulls/... (body= or body=@)
  - gh api ... /issues/... (body= or body=@)

When triggered, extracts the body content from the command, then enforces two rules:

  1. **English-only.** No Hangul characters (U+AC00-D7A3, U+3131-318E) anywhere in
     the body. Per ``feedback_repo_artifacts_english`` memory and CLAUDE.md.

  2. **Template checkboxes preserved.** For PR create/edit only, every
     ``- [ ] ...`` / ``- [x] ...`` line in ``.github/PULL_REQUEST_TEMPLATE.md``
     must appear in the body (state ``[ ]`` vs ``[x]`` is irrelevant). Per
     CLAUDE.md: "flip ``[ ]`` -> ``[x]`` for matching items; do not delete
     unchecked options."

Exit codes:
  0 = allow tool call (no violation, or command is unrelated)
  2 = block tool call; stderr message reaches Claude as feedback
"""

from __future__ import annotations

import json
import re
import shlex
import sys
from pathlib import Path

HANGUL_RE = re.compile(r"[가-힣ㄱ-ㆎ]")
CHECKBOX_RE = re.compile(r"^- \[[ x]\] (.+)$", re.MULTILINE)
TEMPLATE_PATH = Path(".github/PULL_REQUEST_TEMPLATE.md")

PR_CREATE_EDIT_RE = re.compile(r"\bgh\s+pr\s+(create|edit)\b")
PR_COMMENT_RE = re.compile(r"\bgh\s+pr\s+comment\b")
ISSUE_CMD_RE = re.compile(r"\bgh\s+issue\s+(create|edit|comment)\b")
GH_API_RE = re.compile(r"\bgh\s+api\b")


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

    kind = classify(cmd)
    if kind is None:
        return 0

    body = extract_body(cmd)
    if body is None:
        # No body argument found; nothing to validate. e.g. `gh pr edit --title` only.
        return 0

    errors: list[str] = []
    if HANGUL_RE.search(body):
        errors.append(
            "Body contains Korean characters. Per repo convention "
            "(memory: feedback_repo_artifacts_english, CLAUDE.md), PR/issue/commit "
            "artifacts stay in English even when the chat is in Korean. Rewrite "
            "the body in English."
        )

    if kind == "pr":
        missing = missing_template_boxes(body)
        if missing:
            errors.append(
                "Body is missing template checkboxes. Per CLAUDE.md: "
                "'flip [ ] -> [x] for matching items; do NOT delete unchecked "
                "options.' Include these lines (toggle state as needed):\n"
                + "\n".join(f"    - [ ] {m}" for m in missing)
            )

    if errors:
        sys.stderr.write("BLOCKED by .claude/hooks/validate-pr-body.py:\n\n")
        for e in errors:
            sys.stderr.write(f"* {e}\n\n")
        return 2

    return 0


def classify(cmd: str) -> str | None:
    """Return 'pr' for PR create/edit (needs template check), 'other' for everything
    else that takes a body (Hangul check only), or None to skip the hook entirely.
    """
    if PR_CREATE_EDIT_RE.search(cmd):
        return "pr"
    if (
        GH_API_RE.search(cmd)
        and "/pulls/" in cmd
        and "/comments" not in cmd
        and "/reviews" not in cmd
    ):
        return "pr"
    if PR_COMMENT_RE.search(cmd) or ISSUE_CMD_RE.search(cmd):
        return "other"
    if GH_API_RE.search(cmd) and "/issues/" in cmd:
        return "other"
    return None


def extract_body(cmd: str) -> str | None:
    """Pull the body out of a gh-CLI argv. Returns None when no body arg is present
    or when a referenced file cannot be read.
    """
    try:
        argv = shlex.split(cmd)
    except ValueError:
        return None

    i = 0
    while i < len(argv):
        a = argv[i]
        nxt = argv[i + 1] if i + 1 < len(argv) else None

        if a == "--body" and nxt is not None:
            return nxt
        if a.startswith("--body="):
            return a[len("--body=") :]
        if a == "--body-file" and nxt is not None:
            return _read_file(nxt)
        if a.startswith("--body-file="):
            return _read_file(a[len("--body-file=") :])
        if (
            a in {"-F", "-f", "--field", "--raw-field"}
            and nxt is not None
            and nxt.startswith("body=")
        ):
            val = nxt[len("body=") :]
            if val.startswith("@"):
                return _read_file(val[1:])
            return val
        i += 1
    return None


def _read_file(path: str) -> str | None:
    try:
        return Path(path).read_text()
    except OSError:
        return None


def missing_template_boxes(body: str) -> list[str]:
    """Return template checkbox labels absent from body. Empty list if none missing
    or if the template file is missing (tolerant: no template = no enforcement)."""
    template = Path.cwd() / TEMPLATE_PATH
    if not template.exists():
        return []
    try:
        template_text = template.read_text()
    except OSError:
        return []
    template_labels = set(CHECKBOX_RE.findall(template_text))
    body_labels = set(CHECKBOX_RE.findall(body))
    return sorted(template_labels - body_labels)


if __name__ == "__main__":
    sys.exit(main())
