#!/usr/bin/env python3
"""PreToolUse hook: block issue Bash invocations that violate repo conventions.

Triggered on: gh issue (create|edit|comment), gh api paths hitting /issues
(item paths and the creation POST).

Rules:
  1. English-only (title + body) — no non-ASCII letters. Common typographic
     punctuation (dashes, arrows, curly quotes, ellipsis, math signs) + emoji
     allowed.
  2. Template match (gh issue create with a body flag) — exactly one label
     mapping to a .github/ISSUE_TEMPLATE form, and body must carry a
     '### <field label>' heading for every required field of that form
     (GitHub renders form submissions in that shape). Interactive/--web
     creation has no body to inspect — the web chooser enforces the form.
  3. Title shape — 'scope: imperative summary', mirroring the commit
     convention minus the type (issue type = label). Scope prefix make the
     queue scannable by area; cap keep titles readable in list views.

Regex + body/title parsers duplicated in validate_pr.py — standalone
scripts, no shared import; keep in sync.

Exit 0 = allow, exit 2 = block.
"""

from __future__ import annotations

import json
import re
import shlex
import sys
from pathlib import Path

NON_ASCII_RE = re.compile(r"[^\x00-\x7F]")
# Typographic punctuation allowed: formatting not language.
TYPOGRAPHIC_RE = re.compile(
    "[\u2013\u2014\u2018\u2019\u201c\u201d\u2022\u2026"  # dashes quotes bullet ellipsis
    "\u2190\u2192\u2194\u21d2\u2260\u2264\u2265"  # arrows ne le ge
    "\u00b1\u00b7\u00d7\u00f7]"  # plus-minus middot times divide
)
EMOJI_RE = re.compile(
    "[\U0001f000-\U0001faff"  # pictographs, emoticons, transport, flags
    "\U00002600-\U000027bf"  # misc symbols + dingbats
    "\U00002b00-\U00002bff"  # misc symbols and arrows
    "\U0000fe00-\U0000fe0f"  # variation selectors
    "\U0000200d]"  # zero-width joiner (emoji sequences)
)

# Title = "scope: summary" / "scope(subscope): summary"; lowercase scope keep
# it parallel to commit scopes.
TITLE_SHAPE_RE = re.compile(r"^[a-z0-9_]+(\([a-z0-9_./-]+\))?: \S")
MAX_TITLE_LEN = 72

ISSUE_CREATE_RE = re.compile(r"\bgh\s+issue\s+create\b")
ISSUE_OTHER_RE = re.compile(r"\bgh\s+issue\s+(edit|comment)\b")
GH_API_RE = re.compile(r"\bgh\s+api\b")
# \b cover item paths (/issues/5) and creation POST (/issues, no trailing slash).
ISSUES_PATH_RE = re.compile(r"/issues\b")

TEMPLATE_DIR = Path(".github/ISSUE_TEMPLATE")
# Form yml simple enough for line parsing — hook run on system python3, no PyYAML.
TEMPLATE_LABELS_RE = re.compile(r"^labels:\s*\[(.*)\]", re.MULTILINE)
FIELD_SPLIT_RE = re.compile(r"^  - type:", re.MULTILINE)
FIELD_LABEL_RE = re.compile(r"^\s+label:\s*(.+?)\s*$", re.MULTILINE)
FIELD_REQUIRED_RE = re.compile(r"^\s+required:\s*true\b", re.MULTILINE)


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
    title = extract_title(cmd)

    errors: list[str] = []
    if title is not None and has_disallowed_non_ascii(title):
        errors.append(
            "Title contains non-ASCII characters (other than emoji and "
            "typographic punctuation). Per repo convention PR/issue/commit "
            "artifacts must be in English."
        )
    if title is not None:
        errors.extend(title_errors(title))
    if body is not None and has_disallowed_non_ascii(body):
        errors.append(
            "Body contains non-ASCII characters (other than emoji and "
            "typographic punctuation). Per repo convention PR/issue/commit "
            "artifacts must be in English."
        )

    if kind == "create" and body is not None:
        errors.extend(template_errors(extract_labels(cmd), body, load_template_map()))

    if errors:
        sys.stderr.write("BLOCKED by .claude/hooks/validate_issue.py:\n\n")
        for e in errors:
            sys.stderr.write(f"* {e}\n\n")
        return 2

    return 0


def title_errors(title: str) -> list[str]:
    """Title must read 'scope: imperative summary' and stay scannable."""
    errors: list[str] = []
    if not TITLE_SHAPE_RE.match(title):
        errors.append(
            "Title must start with a lowercase scope then ': ' — "
            "'scope: imperative summary' or 'scope(subscope): summary', "
            "mirroring the commit convention minus the type (the issue type "
            f"is its label). Got: {title!r}"
        )
    if len(title) > MAX_TITLE_LEN:
        errors.append(
            f"Title is {len(title)} chars (limit: {MAX_TITLE_LEN}). Put the "
            "detail in the body — the title only has to say which area and "
            "what work."
        )
    if title.endswith("."):
        errors.append("Title must not end with a period.")
    return errors


def has_disallowed_non_ascii(body: str) -> bool:
    stripped = TYPOGRAPHIC_RE.sub("", EMOJI_RE.sub("", body))
    return bool(NON_ASCII_RE.search(stripped))


def classify(cmd: str) -> str | None:
    if ISSUE_CREATE_RE.search(cmd):
        return "create"
    if ISSUE_OTHER_RE.search(cmd):
        return "other"
    if GH_API_RE.search(cmd) and ISSUES_PATH_RE.search(cmd):
        return "other"
    return None


def extract_body(cmd: str) -> str | None:
    try:
        argv = shlex.split(cmd)
    except ValueError:
        return None

    # gh api: -F/-f = field flags; gh subcommands: -F = --body-file shorthand.
    api = GH_API_RE.search(cmd) is not None
    i = 0
    while i < len(argv):
        a = argv[i]
        nxt = argv[i + 1] if i + 1 < len(argv) else None

        if a in {"--body", "-b"} and nxt is not None:
            return nxt
        if a.startswith("--body="):
            return a[len("--body=") :]
        if a == "--body-file" and nxt is not None:
            return _read_file(nxt)
        if a.startswith("--body-file="):
            return _read_file(a[len("--body-file=") :])
        if api:
            if (
                a in {"-F", "-f", "--field", "--raw-field"}
                and nxt is not None
                and nxt.startswith("body=")
            ):
                val = nxt[len("body=") :]
                if val.startswith("@"):
                    return _read_file(val[1:])
                return val
        elif a == "-F" and nxt is not None:
            return _read_file(nxt)
        i += 1
    return None


def _read_file(path: str) -> str | None:
    try:
        return Path(path).read_text()
    except OSError:
        return None


def extract_title(cmd: str) -> str | None:
    try:
        argv = shlex.split(cmd)
    except ValueError:
        return None

    # gh api: -t = --template (output format) — title travel as title= field.
    api = GH_API_RE.search(cmd) is not None
    i = 0
    while i < len(argv):
        a = argv[i]
        nxt = argv[i + 1] if i + 1 < len(argv) else None
        if api:
            if (
                a in {"-F", "-f", "--field", "--raw-field"}
                and nxt is not None
                and nxt.startswith("title=")
            ):
                val = nxt[len("title=") :]
                if val.startswith("@"):
                    return _read_file(val[1:])
                return val
        else:
            if a in {"--title", "-t"} and nxt is not None:
                return nxt
            if a.startswith("--title="):
                return a[len("--title=") :]
        i += 1
    return None


def extract_labels(cmd: str) -> list[str]:
    try:
        argv = shlex.split(cmd)
    except ValueError:
        return []

    raw: list[str] = []
    i = 0
    while i < len(argv):
        a = argv[i]
        nxt = argv[i + 1] if i + 1 < len(argv) else None
        if a in {"--label", "-l"} and nxt is not None:
            raw.append(nxt)
            i += 1
        elif a.startswith("--label="):
            raw.append(a[len("--label=") :])
        i += 1
    return [part.strip() for chunk in raw for part in chunk.split(",") if part.strip()]


def load_template_map() -> dict[str, list[str]]:
    """Map template label -> required form-field labels, from ISSUE_TEMPLATE ymls."""
    tdir = Path.cwd() / TEMPLATE_DIR
    if not tdir.is_dir():
        return {}
    tmap: dict[str, list[str]] = {}
    for path in sorted(tdir.glob("*.yml")):
        try:
            text = path.read_text()
        except OSError:
            continue
        labels_line = TEMPLATE_LABELS_RE.search(text)
        if labels_line is None:  # config.yml and label-less forms carry no mapping
            continue
        labels = [
            item.strip().strip("\"'")
            for item in labels_line.group(1).split(",")
            if item.strip().strip("\"'")
        ]
        required: list[str] = []
        # Chunk 0 = preamble before first field.
        for chunk in FIELD_SPLIT_RE.split(text)[1:]:
            field_label = FIELD_LABEL_RE.search(chunk)
            if field_label is None:  # markdown items carry no label
                continue
            if FIELD_REQUIRED_RE.search(chunk):
                required.append(field_label.group(1).strip("\"'"))
        for label in labels:
            tmap[label] = required
    return tmap


def template_errors(
    labels: list[str], body: str, tmap: dict[str, list[str]]
) -> list[str]:
    if not tmap:
        return []
    mapped = [label for label in labels if label in tmap]
    if not mapped:
        return [
            "Issue must carry exactly one template label so the body shape is "
            f"checkable. Valid labels: {', '.join(sorted(tmap))}. Extra "
            "non-template labels are fine."
        ]
    if len(set(mapped)) > 1:
        return [
            "Issue carries more than one template label "
            f"({', '.join(sorted(set(mapped)))}) — ambiguous body shape; keep one."
        ]
    missing = [
        field
        for field in tmap[mapped[0]]
        if not re.search(rf"^###\s+{re.escape(field)}\s*$", body, re.MULTILINE)
    ]
    if missing:
        return [
            f"Body is missing required '{mapped[0]}' template headings "
            "(GitHub form fields render as '### <label>'):"
            + "".join(f"\n    ### {m}" for m in missing)
        ]
    return []


if __name__ == "__main__":
    sys.exit(main())
