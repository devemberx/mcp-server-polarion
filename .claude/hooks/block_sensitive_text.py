#!/usr/bin/env python3
"""PreToolUse hook: block outward Bash commands carrying private deployment
names (Polarion project/space/document ids) into PR/issue/commit/release/
gist/repo text.

Pattern file `.claude/sensitive-patterns.local`: untracked, one regex per
line, `#` comments, resolved vs `CLAUDE_PROJECT_DIR`. Absent/empty = allow
all; unreadable or dangling symlink = fail closed.

Outward commands only — local grep/cat of sensitive names stay allowed.
Scanned: command string + files it ship as text (file flags, gh api/
workflow `field=@file`, gist/release positionals, `< file` stdin
redirects — flag sets below define surface). Not expanded: command
substitution, pipe sources, wrapper-quoted scripts (`bash -c 'gh ...'`;
unquoted `xargs gh` stay detected). Bare `git push` ship no branch name.

Exit 0 = allow, exit 2 = block.
"""

from __future__ import annotations

import contextlib
import json
import os
import re
import shlex
import sys
from pathlib import Path

PATTERN_FILENAME = ".claude/sensitive-patterns.local"
# Referenced-file read cap — huge release asset must not stall hook.
MAX_SCAN_BYTES = 5_000_000

# All publish text (repo via --description, workflow via dispatch inputs);
# git push = ref names in command string. Detection = token walk —
# option-tolerant regex = exponential backtracking (CodeQL py/redos).
GH_OUTWARD_SUBCOMMANDS = frozenset(
    {"pr", "issue", "api", "release", "gist", "repo", "label", "workflow"}
)
GIT_OUTWARD_SUBCOMMANDS = frozenset({"commit", "tag", "push"})
# git global options taking separate value — skip option + value pair.
GIT_VALUE_OPTIONS = frozenset(
    {"-C", "-c", "--git-dir", "--work-tree", "--namespace", "--exec-path"}
)
# Unparseable command fallback — literal alternation, linear-safe.
OUTWARD_FALLBACK_RE = re.compile(
    r"\bgh\s+(?:pr|issue|api|release|gist|repo|label|workflow)\b"
    r"|\bgit\s+(?:commit|tag|push)\b"
)
# Next argv after these = file shipped as outward text. -F/--field double
# duty: gh api field flag (field=@file) vs gh pr / git commit file shorthand.
FILE_FLAGS = frozenset(
    {"--body-file", "--notes-file", "--file", "-F", "--field", "--input"}
)
API_FIELD_FLAGS = frozenset({"-F", "--field"})
# Key allow []/. — gh api nested field syntax (files[a.md][content]=@path).
FIELD_AT_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_.\[\]-]*=@(.+)$")
# gist edit file flags scoped to `gist edit` — global -a collide git tag -a.
GIST_ADD_FLAGS = frozenset({"-a", "--add"})
# Value-flags — next argv = metadata, not file to publish.
GIST_VALUE_FLAGS = frozenset({"-d", "--desc", "-f", "--filename"})
RELEASE_VALUE_FLAGS = frozenset(
    {
        "-t",
        "--title",
        "-n",
        "--notes",
        "-F",
        "--notes-file",
        "--notes-start-tag",
        "--target",
        "--discussion-category",
    }
)
SHARED_VALUE_FLAGS = frozenset({"-R", "--repo"})
# Operators own tokens even glued (`x;gh`, `<file`); newline = separator.
PUNCTUATION_CHARS = "();<>|&\n"
SEPARATOR_CHARS = frozenset(";&|()\n")
REDIRECT_OPS = frozenset({">", ">>", "<", "<<"})
# Sub-command pair → (leading positionals to skip, value-flags). Release
# first positional = tag name, not a file.
POSITIONAL_FILE_CMDS: dict[tuple[str, str], tuple[int, frozenset[str]]] = {
    ("gist", "create"): (0, GIST_VALUE_FLAGS),
    ("release", "create"): (1, RELEASE_VALUE_FLAGS),
    ("release", "upload"): (1, RELEASE_VALUE_FLAGS),
}


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
    cwd = data.get("cwd")
    if not isinstance(cwd, str):
        cwd = None

    patterns = load_patterns()
    if patterns is None:
        sys.stderr.write(
            "BLOCKED by .claude/hooks/block_sensitive_text.py:\n\n"
            + fail_closed_reason()
        )
        return 2

    hits = scan(cmd, patterns, cwd)
    if hits:
        sys.stderr.write("BLOCKED by .claude/hooks/block_sensitive_text.py:\n\n")
        for pattern in hits:
            # Masked — echoing full pattern hand private name back to model.
            sys.stderr.write(
                "* Outward command text matches private pattern "
                f"{mask(pattern)} in {PATTERN_FILENAME}\n"
            )
        sys.stderr.write(
            "\nPrivate deployment names (project/space/document ids) must not"
            " appear in PR/issue/commit/release/gist/repo text. Reword"
            " generically, e.g. 'live testdrive project'.\n"
        )
        return 2
    return 0


def tokenize(cmd: str) -> list[str]:
    """Shell-operator-aware shlex split; ValueError on unbalanced quote."""
    lex = shlex.shlex(cmd, posix=True, punctuation_chars=PUNCTUATION_CHARS)
    lex.whitespace_split = True
    lex.whitespace = " \t\r"
    return list(lex)


def outward(cmd: str) -> bool:
    """Whether command publish text beyond the local checkout."""
    try:
        argv = tokenize(cmd)
    except ValueError:
        # Unbalanced quote — conservative regex approximation.
        return OUTWARD_FALLBACK_RE.search(cmd) is not None
    for seg in split_segments(argv):
        for i, arg in enumerate(seg):
            if (
                arg == "gh"
                and i + 1 < len(seg)
                and seg[i + 1] in GH_OUTWARD_SUBCOMMANDS
            ):
                return True
            if arg == "git" and git_subcommand(seg[i + 1 :]) in GIT_OUTWARD_SUBCOMMANDS:
                return True
    return False


def git_subcommand(rest: list[str]) -> str | None:
    """First non-option token after `git` — global options skipped."""
    skip_value = False
    for arg in rest:
        if skip_value:
            skip_value = False
            continue
        if arg in GIT_VALUE_OPTIONS:
            skip_value = True
            continue
        if arg.startswith("-"):
            continue
        return arg
    return None


def pattern_path() -> Path:
    """Pattern file under CLAUDE_PROJECT_DIR; cwd fallback outside hook env."""
    return Path(os.environ.get("CLAUDE_PROJECT_DIR", ".")) / PATTERN_FILENAME


def mask(pattern: str) -> str:
    """Identifiable preview without echoing private name; inline-flag prefix
    carry no name chars — strip; short patterns hidden whole."""
    body = re.sub(r"^\(\?[aiLmsux]+\)", "", pattern)
    prefix = body[:2] if len(body) > 4 else ""
    return f"{prefix}…({len(pattern)} chars)"


def dangling_symlink(path: Path) -> bool:
    """Guard installed then link target moved — not "never set up"."""
    return path.is_symlink() and not path.exists()


def fail_closed_reason() -> str:
    """Block reason for ``load_patterns() is None`` — remedy differ by cause."""
    path = pattern_path()
    if dangling_symlink(path):
        return (
            f"* Pattern file {path} is a dangling symlink — fail closed. "
            "Re-link it to the main checkout's file or remove it.\n"
        )
    return (
        f"* Pattern file {path} exists but is unreadable or not UTF-8 — "
        "fail closed. Fix permissions/encoding or remove the file.\n"
    )


def load_patterns() -> list[re.Pattern[str]] | None:
    """Compiled patterns; ``None`` = file unreadable (caller fail closed)."""
    path = pattern_path()
    if dangling_symlink(path):
        return None
    if not path.exists():
        return []
    try:
        lines = path.read_text().splitlines()
    except (OSError, UnicodeDecodeError):
        # Undecodable = broken install, same class as unreadable.
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


def scan(
    cmd: str, patterns: list[re.Pattern[str]], cwd: str | None = None
) -> list[str]:
    """Matched pattern strings across command + referenced text files."""
    if not patterns:
        # Absent/empty pattern file = allow all — skip referenced-file I/O.
        return []
    texts = [cmd, *referenced_file_texts(cmd, cwd)]
    return [p.pattern for p in patterns if any(p.search(t) for t in texts)]


def referenced_file_texts(cmd: str, cwd: str | None = None) -> list[str]:
    """Contents of files the command ship as text; unreadable skipped.

    Relative paths anchor to ``cwd`` (Bash session dir — hook cwd differ);
    binary decoded lossy — crash on read = exit 1 = fail open.
    """
    try:
        argv = tokenize(cmd)
    except ValueError:
        return []
    paths: list[str] = []
    for seg in split_segments(argv):
        paths.extend(segment_paths(seg))
    texts: list[str] = []
    for path in paths:
        resolved = Path(path).expanduser()
        if not resolved.is_absolute() and cwd:
            resolved = Path(cwd) / resolved
        with contextlib.suppress(OSError):
            with resolved.open("rb") as handle:
                raw = handle.read(MAX_SCAN_BYTES)
            texts.append(raw.decode(errors="replace"))
    return texts


def split_segments(argv: list[str]) -> list[list[str]]:
    """argv split at shell separators — flag context never cross commands."""
    segments: list[list[str]] = []
    seg: list[str] = []
    for arg in argv:
        # Separator runs clump (`&&`, `;\n`) — any all-separator token split.
        if all(c in SEPARATOR_CHARS for c in arg):
            if seg:
                segments.append(seg)
                seg = []
        else:
            seg.append(arg)
    if seg:
        segments.append(seg)
    return segments


def has_pair(seg: list[str], first: str, second: str) -> bool:
    """Adjacent token pair present (sub-command detection)."""
    return any(seg[i] == first and seg[i + 1] == second for i in range(len(seg) - 1))


def segment_paths(seg: list[str]) -> list[str]:
    """File paths one command segment ship as outward text."""
    gist_edit = has_pair(seg, "gist", "edit")
    # workflow run share gh api @file/@- syntax; --json read stdin.
    workflow_run = has_pair(seg, "workflow", "run")
    at_field_syntax = has_pair(seg, "gh", "api") or workflow_run
    paths: list[str] = []
    stdin_body = workflow_run and "--json" in seg

    def add_flag_value(flag: str, value: str) -> None:
        nonlocal stdin_body
        if flag in API_FIELD_FLAGS and at_field_syntax:
            field_at = FIELD_AT_RE.match(value)
            if field_at and field_at.group(1) == "-":
                stdin_body = True
            elif field_at:
                paths.append(field_at.group(1))
        elif value == "-":
            stdin_body = True
        else:
            paths.append(value)

    for i, arg in enumerate(seg):
        nxt = seg[i + 1] if i + 1 < len(seg) else None
        if arg in FILE_FLAGS and nxt is not None:
            add_flag_value(arg, nxt)
        elif "=" in arg and arg.partition("=")[0] in FILE_FLAGS:
            flag, _, value = arg.partition("=")
            add_flag_value(flag, value)
        elif arg.startswith("-F") and len(arg) > 2 and arg[2] != "=":
            # Attached short form: gh api -Fkey=@path / git commit -Fmsg.txt.
            add_flag_value("-F", arg[2:])
        elif arg in GIST_ADD_FLAGS and gist_edit and nxt is not None:
            paths.append(nxt)

    positional, positional_stdin = positional_paths(seg)
    paths.extend(positional)
    if stdin_body or positional_stdin:
        # Stdin body — scan `< file` source; pipes stay unexpanded.
        paths.extend(
            seg[i + 1] for i, arg in enumerate(seg) if arg == "<" and i + 1 < len(seg)
        )
    return paths


def positional_paths(seg: list[str]) -> tuple[list[str], bool]:
    """Positional file args for publish-file sub-commands + stdin-marker flag."""
    for i in range(len(seg) - 1):
        pair = (seg[i], seg[i + 1])
        spec = POSITIONAL_FILE_CMDS.get(pair)
        if spec is not None:
            skip_positionals, value_flags = spec
            start = i + 2
            break
    else:
        return [], False
    paths: list[str] = []
    saw_stdin = False
    skip_value = False
    for arg in seg[start:]:
        if skip_value:
            skip_value = False
            continue
        if arg in value_flags or arg in SHARED_VALUE_FLAGS or arg in REDIRECT_OPS:
            skip_value = True
            continue
        if arg == "-":
            saw_stdin = True
            continue
        if arg.startswith("-"):
            # Boolean flag — no file behind it.
            continue
        if skip_positionals > 0:
            skip_positionals -= 1
            continue
        paths.append(arg)
    if pair == ("gist", "create") and not paths:
        # gh read stdin when zero file args — `-` token not required.
        saw_stdin = True
    return paths, saw_stdin


if __name__ == "__main__":
    sys.exit(main())
