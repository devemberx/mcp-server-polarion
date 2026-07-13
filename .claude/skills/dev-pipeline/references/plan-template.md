# Plan — <feature>

Copy this file to `.pipeline/plan.md` and fill every slot, then delete this
instruction paragraph — downstream agents read the filled file verbatim.
Each Changes task is exactly one `pipeline-implementer` spawn — keep tasks
bounded, ordered by dependency, and self-contained (the implementer sees only
the task excerpt plus the spec/plan files, never this conversation).

## Context

Two to four sentences: what the spec asks for, and the key reuse findings
from pattern-scout with file:line references.

## Branch

`<type>/<kebab-summary>` — type ∈ feat|fix|refactor|test|docs|chore|ci.

## Changes

### Task 1 — <short name> (deps: none)

- `src/...` — change, citing scout file:line refs to mirror
- `tests/...` — RED case(s) to write first

### Task 2 — <short name> (deps: Task 1)

- ...

Cross-task integration (imports, `EXPECTED_TOOL_NAMES`, eval coverage entry)
stays in the main thread — list it here as its own unnumbered item.

## Verification

- [ ] Gates: pytest+cov, ruff check, ruff format --check, mypy, diff-cover ≥90%
- [ ] Live test (one per spec UNVERIFIED item): <what to check against real server>
- [ ] Registration verified: tool listed, eval coverage green

## Commit-PR

- Commit: `type(scope): summary` ≤50 chars + 2-bullet body (motivation, change)
- PR notes: live-test evidence, unfixed LOW/NIT findings
