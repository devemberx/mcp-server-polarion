#!/usr/bin/env python3
"""PreToolUse hook: block PR/issue body Bash invocations that violate repo conventions.

Triggered on: gh pr (create|edit|comment), gh issue (create|edit|comment),
gh api .../pulls/... or .../issues/...

Rules:
  1. English-only — no non-ASCII characters in the body.
  2. Template checkboxes preserved — every checkbox from PULL_REQUEST_TEMPLATE.md
     must appear (PR create/edit only).
  3. ## Changes section — exactly two non-empty bullets, each <= 120 chars
     (PR create/edit only).

Exit 0 = allow, exit 2 = block.
"""

from __future__ import annotations

import json
import re
import shlex
import sys
from pathlib import Path

NON_ASCII_RE = re.compile(r"[^\x00-\x7F]")
EMOJI_RE = re.compile(
    "[\U0001f000-\U0001faff"  # pictographs, emoticons, transport, flags
    "\U00002600-\U000027bf"  # misc symbols + dingbats
    "\U00002b00-\U00002bff"  # misc symbols and arrows
    "\U0000fe00-\U0000fe0f"  # variation selectors
    "\U0000200d]"  # zero-width joiner (emoji sequences)
)
CHECKBOX_RE = re.compile(r"^- \[[ x]\] (.+)$", re.MULTILINE)
CHANGES_HEADER_RE = re.compile(r"^##\s+Changes\s*$", re.IGNORECASE | re.MULTILINE)
NEXT_SECTION_RE = re.compile(r"^##\s", re.MULTILINE)
TEMPLATE_PATH = Path(".github/PULL_REQUEST_TEMPLATE.md")
BULLET_LIMIT = 120
REQUIRED_BULLETS = 2

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
        return 0

    errors: list[str] = []
    if has_disallowed_non_ascii(body):
        errors.append(
            "Body contains non-ASCII characters (other than emoji). Per repo "
            "convention PR/issue/commit artifacts must be in English."
        )

    if kind == "pr":
        missing = missing_template_boxes(body)
        if missing:
            errors.append(
                "Body is missing template checkboxes. "
                "Do NOT delete unchecked options."
                + "\n".join(f"    - [ ] {m}" for m in missing)
            )
        errors.extend(changes_format_errors(body))

    if errors:
        sys.stderr.write("BLOCKED by .claude/hooks/validate-pr-body.py:\n\n")
        for e in errors:
            sys.stderr.write(f"* {e}\n\n")
        return 2

    return 0


def has_disallowed_non_ascii(body: str) -> bool:
    return bool(NON_ASCII_RE.search(EMOJI_RE.sub("", body)))


def classify(cmd: str) -> str | None:
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


def changes_format_errors(body: str) -> list[str]:
    header = CHANGES_HEADER_RE.search(body)
    if header is None:
        return []
    nxt = NEXT_SECTION_RE.search(body, header.end())
    section = body[header.end() : nxt.start()] if nxt else body[header.end() :]
    lines = (ln.rstrip() for ln in section.splitlines())
    bullets = [ln for ln in lines if ln.startswith("- ")]

    errors: list[str] = []
    if len(bullets) != REQUIRED_BULLETS:
        errors.append(
            f"The ## Changes section must contain exactly {REQUIRED_BULLETS}. "
            f"These become the squash commit body (motivation, then change)"
        )
    for ln in bullets:
        if len(ln) > BULLET_LIMIT:
            errors.append(
                f"A ## Changes bullet is {len(ln)} chars (limit: {BULLET_LIMIT}):"
                f"\n    {ln}"
            )
    return errors


if __name__ == "__main__":
    sys.exit(main())
