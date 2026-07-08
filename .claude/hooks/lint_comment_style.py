#!/usr/bin/env python3
"""PostToolUse tripwire: flag caveman-rule violations in ``#`` comments the
model just wrote. Scope = ``#`` comments in ``.py`` files only. Docstrings
skipped on purpose — ``@mcp.tool`` docstrings + ``Field(description=...)`` are
LLM-facing prose (eval-gated), false-positive risk too high.

Exit 2 with stderr feeds the finding back to the model so it self-corrects;
never blocks the edit (tool already ran).
"""

from __future__ import annotations

import json
import re
import sys

# Functional pseudo-comments drive tooling — never lint prose after them.
_FUNCTIONAL = re.compile(
    r"#\s*(noqa|type:\s*ignore|pragma|fmt:|ruff:|isort:|coding:|!)"
)

# whole-word, case-insensitive where prose varies.
_ARTICLE = re.compile(r"\b(the|an)\b", re.IGNORECASE)
_FILLER = re.compile(
    r"\b(just|really|basically|actually|simply|of course|please)\b",
    re.IGNORECASE,
)
# caveman bans leading NOTE / WARNING prefixes + banner dividers.
_PREFIX = re.compile(r"#\s*(WARNING|NOTE)\b", re.IGNORECASE)
_BANNER = re.compile(r"#\s*[=\-*_]{4,}")
# task-marker without ``(owner/#issue)`` = stray.
_STRAY_TODO = re.compile(r"#.*\bTODO\b(?!\s*\()")


def _comment_text(line: str) -> str | None:
    """Return the ``#`` comment portion of a line, or None. Naive split: skip
    lines where ``#`` sits inside a string literal (best-effort, tripwire only).
    """
    idx = line.find("#")
    if idx == -1:
        return None
    before = line[:idx]
    # crude string guard: unbalanced quote before # = likely inside.
    if before.count('"') % 2 or before.count("'") % 2:
        return None
    return line[idx:]


def _scan(text: str) -> list[str]:
    findings: list[str] = []
    for raw in text.splitlines():
        comment = _comment_text(raw)
        if comment is None or _FUNCTIONAL.search(comment):
            continue
        body = comment.lstrip("#").strip()
        if not body:
            continue
        if _ARTICLE.search(body):
            findings.append(f'article: "{body}"')
        if _FILLER.search(body):
            findings.append(f'filler: "{body}"')
        if _PREFIX.search(comment):
            findings.append(f'NOTE/WARNING prefix: "{body}"')
        if _BANNER.search(comment):
            findings.append(f'banner divider: "{body}"')
        if _STRAY_TODO.search(comment):
            findings.append(f'stray TODO (need owner/#issue): "{body}"')
    return findings


def _edited_text(tool_input: dict[str, object]) -> str:
    """Concatenate new comment-bearing text from Edit/Write/MultiEdit input."""
    parts: list[str] = []
    for key in ("new_string", "content"):
        val = tool_input.get(key)
        if isinstance(val, str):
            parts.append(val)
    edits = tool_input.get("edits")
    if isinstance(edits, list):
        for e in edits:
            if isinstance(e, dict) and isinstance(e.get("new_string"), str):
                parts.append(e["new_string"])
    return "\n".join(parts)


def main() -> int:
    try:
        payload = json.load(sys.stdin)
    except (json.JSONDecodeError, ValueError):
        return 0
    tool_input = payload.get("tool_input", {})
    if not isinstance(tool_input, dict):
        return 0
    path = tool_input.get("file_path", "")
    if not (isinstance(path, str) and path.endswith(".py")):
        return 0

    findings = _scan(_edited_text(tool_input))
    if not findings:
        return 0

    # dedup, keep order.
    seen: dict[str, None] = {}
    for f in findings:
        seen.setdefault(f, None)
    msg = "\n".join(f"  - {f}" for f in seen)
    print(
        "Comment-style tripwire (caveman rules) in "
        f"{path}:\n{msg}\n"
        "Drop articles/filler, no NOTE:/WARNING:/banner, TODO needs "
        "(owner/#issue). Fix these comments. (Docstrings/Field prose exempt.)",
        file=sys.stderr,
    )
    return 2


if __name__ == "__main__":
    sys.exit(main())
