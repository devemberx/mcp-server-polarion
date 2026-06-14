---
name: compress-code-comments
description: Compress and prune verbose developer-facing code comments and dev docstrings across .py files, Google-style — keep the "why", drop the "what", one line per point — gated on a comment-only diff that leaves lint/types/tests green. Use when asked to tidy/compress/prune/shrink/clean up code comments, reduce comment verbosity, apply Google comment style, remove redundant or step-by-step inline comments, or trim over-explained docstrings. This is the comments skill, NOT the tool-description skill — it never touches `@mcp.tool` docstrings or `Field(description=...)` (those are LLM-facing; use shrink-mcp-tool-docs for them). Triggers on `/compress-code-comments` or any request about thinning code comments rather than tool/MCP descriptions.
---

# Tidy Comments

Goal: raise the **information density** of developer-facing comments across the codebase. Verbose narration → tight, "why"-first lines. Redundant "what" comments → deleted. The code keeps doing exactly what it did — only comments and dev docstrings change.

Work **one file at a time** and report what you cut and why. The diff must be comment-only: no executable line moves, no string literal changes, no behavior change. The gate below proves it.

## Boundary vs shrink-mcp-tool-docs (read first)

This skill and `shrink-mcp-tool-docs` edit different surfaces. Do not cross the line:

| Surface | Owner | This skill |
|---|---|---|
| `#` comments (inline + standalone) | **compress-code-comments** | edit |
| Module / class docstrings | **compress-code-comments** | edit |
| Private/helper function docstrings (e.g. `_build_*_payload`) | **compress-code-comments** | edit |
| `@mcp.tool` function docstrings | shrink-mcp-tool-docs | **skip — never touch** |
| `Field(description=...)` strings | shrink-mcp-tool-docs | **skip — never touch** |

A function decorated with `@mcp.tool` ships its whole docstring to the client LLM; that text is eval-gated by the other skill. Leave it byte-for-byte unchanged. Same for every `Field(description=...)`. You may still tidy `#` comments *inside the body* of an `@mcp.tool` function — just not its docstring.

## Scope

Every `.py` file in the repo except `.venv/` and other vendored trees: `src/`, `tests/`, `evals/`, `.github/scripts/`, `.claude/hooks/`, `scripts/`. CLAUDE.md and other `*.md` are out of scope (this skill is code comments only).

House style is already written down in CLAUDE.md → **Comment & Docstring Style**; this skill applies it Google-style across files that drifted from it. When the repo rule and a Google guideline disagree, the repo rule wins.

## Never touch — functional pseudo-comments

These look like comments but drive tooling/runtime. Deleting or rewording them breaks the build. Preserve verbatim, including position on the line:

- `# noqa` / `# noqa: E501` (ruff suppressions — this repo uses many)
- `# type: ignore[...]` (mypy)
- `# pragma: no cover` (coverage)
- `# fmt: off` / `# fmt: on` / `# ruff: noqa` / `# isort:` directives
- `#!` shebang lines, `# -*- coding: -*-` encoding lines, license/SPDX headers
- A `#`-comment that is the load-bearing reason a line reads oddly (e.g. explains a `# noqa`)

If a comment *combines* a directive with prose (`x = f()  # noqa: E501  legacy: drop after migration`), keep the directive; you may tighten only the prose after it.

## Compression principles (Google-style)

1. **Density.** Narrative sentences → keyword/noun-phrase fragments. *"This is here in order to retry the request when the server returns a 429 so that we don't fail"* → *"Retry on 429 (rate limit)."*
2. **Why, not what.** Delete any comment a competent reader gets from the code itself. Keep only intent, rationale, non-obvious constraint, gotcha. `i += 1  # increment i` → delete. `i += 1  # skip the sentinel row` → keep.
3. **No step-by-step narration.** Inline "now we do X, then Y" comments tracking each statement → delete. If one fact is genuinely load-bearing, lift a single summary line to the top of the function/block (or its docstring) instead of scattering it.
4. **One line per point.** No multi-sentence paragraphs restating the same idea. No dev-narrative ("we tried X then switched to Y"). No `WARNING:`/`NOTE:`/banner-divider prefixes — state the fact plainly.
5. **TODOs → Google format.** `# TODO(username): concrete action`. Add an owner if the surrounding context names one; otherwise keep it ownerless but still concrete (`# TODO: drop after Polarion 2410`). Never invent an owner.
6. **Field/param doc lines** in dev docstrings: one line; drop entirely when the name + type already say it.

Don't over-cut. A comment that survives explains something the code cannot. When unsure whether a comment is load-bearing, keep it — density is the goal, amnesia is not.

## Workflow

Run on a branch off `main` (e.g. `chore/compress-code-comments`), never directly on `main`.

1. **Baseline gates green.** Confirm a clean tree, then `uv run ruff check . && uv run ruff format --check . && uv run mypy src/ && uv run pytest -q`. If any is already red, stop and report — you need a green baseline to attribute a later failure to your cuts.
2. **Build the file list** (scope above), e.g. `git ls-files '*.py' | grep -vE '^\.venv/'`. Put it in your todo list and walk it top to bottom — one file in `in_progress` at a time.
3. **Per file:**
   a. Read it. Identify `@mcp.tool` docstrings and `Field(description=...)` → mark off-limits. Identify functional pseudo-comments → mark off-limits.
   b. Apply the compression principles to the remaining comments/dev-docstrings via `Edit`.
   c. **Comment-only gate:** `python .claude/skills/compress-code-comments/scripts/assert_comment_only.py <file>` — compares the working file against `HEAD` with comments stripped and docstrings normalized; nonzero exit ⇒ you changed code or a non-docstring literal ⇒ revert that edit. (First run of the loop commits a green baseline so `HEAD` is the pre-tidy state; or pass `--against <ref>`.)
   d. Record the file's cuts for the summary (count + one-line rationale).
4. **After a batch (or all files):** run the full gate again — `uv run ruff check . && uv run ruff format --check . && uv run mypy src/ && uv run pytest -q`. Green ⇒ keep. A new ruff/mypy failure almost always means you deleted a `# noqa`/`# type: ignore` — restore it. Red pytest on a comment-only diff is near-impossible; if it happens, your diff wasn't comment-only — re-check with the script.
5. **Stop** when the list is exhausted or the user scopes you to a subset. Final state = all four gates green.

The `assert_comment_only.py` script is the cheap per-file check; the four-command gate is the authoritative one. Use the script to fail fast, the gate to confirm.

## Output format

1. **Per file** (only files you changed): `path` — N comments removed, M compressed, plus a one-line rationale for any non-obvious cut. Show the diff for the first couple of files so the user can calibrate, then summarize the rest.
2. **Preserved-on-purpose:** note any verbose-looking comment you deliberately kept and why (load-bearing), and confirm `@mcp.tool` docstrings / `Field(description=...)` / functional comments were untouched.
3. **Final gates:** the four green commands.
4. **Totals:** files touched, comments removed, comments compressed, net lines/chars saved.

See `references/google-comment-style.md` for worked before/after examples covering each principle.
