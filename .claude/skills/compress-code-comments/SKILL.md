name: compress-code-comments
description: Compress verbose developer code comments and dev docstrings in .py files, Google-style — keep why, drop what, one line per point; gated on comment-only AST diff. Use when asked to tidy/shrink/clean up code comments, cut comment verbosity, or trim over-explained docstrings. NOT for `@mcp.tool` docstrings or `Field(description=...)` (LLM-facing — use shrink-mcp-tool-docs). Triggers on `/compress-code-comments`.
---

# Self-Documenting Code: Comment Compression

Over-commenting costs: every "what" comment needs re-sync on each code edit (or rots into lie), narration drags reading. Goal: raise **information density** of developer-facing comments across codebase. Verbose narration → tight "why"-first lines. Redundant "what" comments → deleted. Code keeps doing same — only comments and dev docstrings change.

Work **one file at a time**, report what cut and why. Diff must be comment-only: no executable line moves, no string literal changes, no behavior change. Gate below proves it.

## Boundary vs shrink-mcp-tool-docs (read first)

This skill and `shrink-mcp-tool-docs` edit different surfaces. Don't cross line:

| Surface | Owner | This skill |
|---|---|---|
| `#` comments (inline + standalone) | **compress-code-comments** | edit |
| Module / class docstrings | **compress-code-comments** | edit |
| Private/helper function docstrings (e.g. `_build_*_payload`) | **compress-code-comments** | edit |
| `@mcp.tool` function docstrings | shrink-mcp-tool-docs | **skip — never touch** |
| `Field(description=...)` strings | shrink-mcp-tool-docs | **skip — never touch** |

Function decorated `@mcp.tool` ships whole docstring to client LLM; that text eval-gated by other skill. Leave byte-for-byte unchanged. Same for every `Field(description=...)`. Can still tidy `#` comments *inside the body* of an `@mcp.tool` function — just not its docstring.

## Scope

Every `.py` file in repo except `.venv/` and other vendored trees: `src/`, `tests/`, `evals/`, `.github/scripts/`, `.claude/hooks/`, `scripts/`. CLAUDE.md and other `*.md` out of scope (this skill code comments only).

House style already in CLAUDE.md → **Non-Negotiable Rules** (comment/docstring bullets); this skill applies it Google-style across files that drifted. When repo rule and Google guideline disagree, repo rule wins.

## Never touch — functional pseudo-comments

These look like comments but drive tooling/runtime. Deleting or rewording breaks build. Preserve verbatim, including position on line:

- `# noqa` / `# noqa: E501` (ruff suppressions — repo uses many)
- `# type: ignore[...]` (mypy)
- `# pragma: no cover` (coverage)
- `# fmt: off` / `# fmt: on` / `# ruff: noqa` / `# isort:` directives
- `#!` shebang lines, `# -*- coding: -*-` encoding lines, license/SPDX headers
- A `#`-comment that is load-bearing reason a line reads oddly (e.g. explains a `# noqa`)

Comment *combines* directive with prose (`x = f()  # noqa: E501  legacy: drop after migration`): keep directive; tighten only prose after it.

## Compression principles (Google-style)

1. **Density.** Narrative sentences → keyword/noun-phrase fragments. *"This is here in order to retry the request when the server returns a 429 so that we don't fail"* → *"Retry on 429 (rate limit)."*
2. **Why, not what.** Delete any comment competent reader gets from code itself. Keep only intent, rationale, non-obvious constraint, gotcha. `i += 1  # increment i` → delete. `i += 1  # skip the sentinel row` → keep.
3. **No step-by-step narration.** Inline "now we do X, then Y" comments tracking each statement → delete. If one fact genuinely load-bearing, lift single summary line to top of function/block (or its docstring) instead of scattering.
4. **One line per point.** No multi-sentence paragraphs restating same idea. No dev-narrative ("we tried X then switched to Y"). No `WARNING:`/`NOTE:`/banner-divider prefixes (`# ====`, `# ----`) — state fact plainly.
5. **TODOs → Google format.** `# TODO(username): concrete action`. Add owner if surrounding context names one; else keep ownerless but still concrete (`# TODO: drop after Polarion 2410`). Prefer issue number or milestone over guessed name when one exists (`# TODO(#142): ...`). Never invent owner.
6. **Field/param doc lines** in dev docstrings: one line; drop entirely when name + type already say it.

Don't over-cut. Surviving comment explains something code cannot. Unsure if comment load-bearing → keep it — density is goal, amnesia not.

A bare triple-quoted string that isn't a docstring (not first statement of module/class/function) is string literal, not comment — gate protects it exactly like `Field(...)`. Don't compress it; touching it fails gate.

## Workflow

Run on branch off `main` (e.g. `chore/compress-code-comments`), never directly on `main`.

1. **Baseline gates green.** Confirm clean tree, then `uv run ruff check . && uv run ruff format --check . && uv run mypy src/ && uv run pytest -q`. If any already red, stop and report — need green baseline to attribute later failure to your cuts.
2. **Build the file list.** User named specific files/dirs → restrict to those. No scope given → confirm before starting; don't silently sweep whole repo; offer full scope above or ask which subset. Then build list (e.g. `git ls-files '*.py' | grep -vE '^\.venv/'`), put in todo list, walk top to bottom — one file in `in_progress` at a time.
3. **Per file:**
   a. Read it. Identify `@mcp.tool` docstrings and `Field(description=...)` → mark off-limits. Identify functional pseudo-comments → mark off-limits.
   b. Apply compression principles to remaining comments/dev-docstrings via `Edit`.
   c. **Comment-only gate:** `python .claude/skills/compress-code-comments/scripts/assert_comment_only.py <file>` — compares working file against `HEAD` with comments stripped and docstrings normalized; nonzero exit ⇒ you changed code or non-docstring literal ⇒ revert that edit. Step 1's clean tree makes `HEAD` pre-tidy baseline; don't commit mid-loop, so every per-file check compares working file against original (pass `--against <ref>` only if need different baseline).
   d. Record file's cuts for summary (count + one-line rationale).
4. **After a batch (or all files):** run full gate again — `uv run ruff check . && uv run ruff format --check . && uv run mypy src/ && uv run pytest -q`. Green ⇒ keep. New ruff/mypy failure almost always means you deleted a `# noqa`/`# type: ignore` — restore it. Red pytest on comment-only diff near-impossible; if it happens, your diff wasn't comment-only — re-check with script.
5. **Stop** when list exhausted or user scopes you to subset. Final state = all four gates green.

`assert_comment_only.py` script is cheap per-file check; four-command gate is authoritative one. Use script to fail fast, gate to confirm.

## Output format

1. **Per file** (only files you changed): `path` — N comments removed, M compressed, plus one-line rationale for any non-obvious cut. Show diff for first couple files so user can calibrate, then summarize rest.
2. **Preserved-on-purpose:** note any verbose-looking comment you deliberately kept and why (load-bearing), confirm `@mcp.tool` docstrings / `Field(description=...)` / functional comments untouched.
3. **Final gates:** four green commands.
4. **Totals:** files touched, comments removed, comments compressed, net lines/chars saved.

See `references/google-comment-style.md` for worked before/after examples covering each principle.