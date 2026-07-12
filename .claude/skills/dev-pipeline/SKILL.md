---
name: dev-pipeline
description: Full feature-development pipeline for this repo — isolated worktree, spec, implementation plan, TDD build, quality gates, then a review→fix loop that only stops when review passes. Each stage delegates to a subagent with a stage-appropriate model. Use when starting any new MCP tool, feature, or multi-file change from scratch; when the user says "새 도구", "new tool/feature", "run the pipeline", or asks to develop something "the same way as list_test_records". Triggers on `/dev-pipeline`. NOT for one-file tweaks, doc edits, or work already mid-flight — jump to the matching stage instead.
---

# Dev Pipeline

Encode the sequence that shipped `list_test_records` (PR #170): worktree → spec →
plan → TDD implement → gates → review loop → ship. Main session is the
**orchestrator**: it sequences stages, holds approvals, and merges subagent
reports. Subagents do the stage work in fresh context windows — they never spawn
other subagents (platform constraint, and paraphrase hops lose information).

```
 0.Worktree → 1.Spec →(approve)→ 2.Plan →(approve)→ 3.Implement(TDD) → 4.Gates
                                                          ▲                │
                                                          │                ▼
                                              6.Fix ◄─(findings)─ 5.Review ─(clean)→ 7.Ship
```

## Stage → agent → model

| Stage | Who runs it | Model | Why this model |
|---|---|---|---|
| 0 Worktree setup | main thread | — | harness tools (EnterWorktree), no judgment needed |
| 1 Spec research | Explore / general-purpose subagent | sonnet | broad doc/API sweep; conclusions matter, not transcripts |
| 1 Spec writing | main thread | — | needs full user context + approval dialogue |
| 2 Plan exploration | Explore subagent | sonnet (haiku if pure lookup) | fan-out file:line pattern hunting |
| 2 Plan design | Plan subagent | opus | architecture trade-offs, reasoning-heavy |
| 3 Implement | general-purpose subagent per plan task | sonnet | code volume; escalate to opus/fable when a task touches tricky contracts |
| 4 Gates | main thread | — | deterministic commands; a subagent adds nothing |
| 5 Review | fresh general-purpose subagent (or cavecrew-reviewer) | opus | judgment-heavy; fresh context avoids implementer blindness |
| 6 Fix | general-purpose subagent (or cavecrew-builder for 1-2 files) | sonnet | bounded edits from a findings list |

Escalation rule: default is the table; move one model tier up when the stage
keeps failing or the domain is unfamiliar, one tier down for mechanical work.
Parallel subagents only for **independent** research (no shared state, no
ordering) — implementation tasks with dependencies run sequentially.

## Stage 0 — Worktree

1. `EnterWorktree` (never bare `git worktree add` — native tool tracks cleanup).
2. Rename the generated branch to `<type>/<kebab-summary>` — pre-push hook
   accepts only `feat|fix|refactor|test|docs|chore|ci` prefixes (`feat/`, not
   `feature/`).
3. Symlink untracked configs from the main checkout: `.env`, `.mcp.json`,
   `.claude/settings.local.json`, `.vscode/settings.json`.
4. `uv sync --dev --group evals`, then baseline `uv run pytest` — must be green
   before any change, else you can't tell new breakage from old.

## Stage 1 — Spec

Goal: a minimal spec the user approves before any plan exists.

1. Spawn research subagent(s) for the external contract: real API docs (fetch
   the vendor SDK/OpenAPI dump, not memory), plus existing sibling tools for
   conventions. Ask for exact endpoints, params, resource attributes,
   relationships, id formats.
2. Write the spec in the main thread: tool signature, response model fields
   (minimal — defer detail to a future `get_*`), request params, error mapping,
   files to touch, open live-verification questions.
3. **Stop and get user approval.** Spec changes are cheap here, expensive later.

## Stage 2 — Plan

1. Explore subagent: reusable pieces with `file:line` (parsers, pagination,
   fixtures, test patterns, eval coverage mechanics). One agent unless scope
   spans unrelated areas.
2. Plan output sections: Context / Branch / Changes (per file, referencing the
   found patterns) / Verification (incl. live-test items for contract
   boundaries) / Commit-PR. Use plan mode when available; get approval.

## Stage 3 — Implement (TDD)

Follow superpowers:test-driven-development — it is the law here, not a style:

- Tests first, watch them fail (RED) for the *right* reason before any
  production code. Collection errors on missing symbols count as valid RED for
  new modules.
- Implement minimal code to GREEN, one plan task at a time; new `@mcp.tool`
  needs `EXPECTED_TOOL_NAMES` + eval coverage (case or `DEFERRED` entry).
- Dispatch bounded tasks to implementer subagents with the plan excerpt +
  file:line references; keep cross-task integration in the main thread.

## Stage 4 — Gates (all must pass before review)

```bash
uv run pytest --cov=src/mcp_server_polarion --cov=evals --cov-report=xml
uv run ruff check . && uv run ruff format --check . && uv run mypy src/
uv run diff-cover coverage.xml --compare-branch=origin/main --fail-under=90
```

Contract-boundary changes (include/fields/parse/auth or any new HTTP shape)
additionally need a **live test** against the real server — mocks encode your
assumptions, they cannot falsify them. Feed live findings back into fakes and
mocks so tests mirror reality (e.g. an endpoint that omits `meta.totalCount`).

## Stage 5 — Review (loop entry)

1. Spawn a fresh-context reviewer subagent on the full branch diff. It gets the
   spec + plan as context, not the implementation transcript.
2. Findings come back severity-tagged: CRITICAL / MEDIUM / LOW / NIT, each with
   file:line, failure scenario, and a concrete fix.
3. **Stop criterion — the only exit:** zero CRITICAL/MEDIUM actionable
   findings. LOW/NIT get recorded in PR notes, not necessarily fixed. When the
   criterion holds → Stage 7.
4. Criterion not met → Stage 6.

## Stage 6 — Fix, then back to review

- Apply CRITICAL/MEDIUM items; behavior changes go through RED→GREEN again,
  never patch-and-hope.
- Re-run Stage 4 gates, then **return to Stage 5** with a fresh reviewer pass
  over the new diff.
- Loop cap: after 3 review rounds with open findings, stop and escalate to the
  user with the remaining list — grinding further usually means the spec or
  plan is wrong, not the code.

## Stage 7 — Ship

- Commit: `type(scope): summary` ≤50 chars + 2-bullet body (motivation,
  change), bullets ≤120 chars — hook enforces.
- PR: flip template checkboxes `[ ]`→`[x]`, keep unchecked options, record
  live-test evidence and unfixed LOW/NIT findings in Notes. Squash merge only.

## Common Rationalizations

| Excuse | Reality |
|---|---|
| "Small feature, skip the spec" | Spec took 10 min for list_test_records and caught a missing-Lucene-support surprise before any code existed. |
| "I'll run tests after implementing" | A test that never failed proves nothing. RED first is the whole point. |
| "Mocks cover it, skip the live test" | Live run found bare-GUID user ids and a missing `meta.totalCount` that no mock predicted. |
| "Review myself in-context, faster" | The implementer's context contains its own justifications. Fresh context finds what you rationalized. |
| "One more fix round, no re-review" | Fixes are new code; new code gets reviewed. That's why the loop exists. |
| "Subagent for the gate commands too" | Gates are deterministic bash. A subagent burns tokens to relay an exit code. |
| "Opus everywhere to be safe" | Model choice is a cost/judgment trade-off; the table exists because sonnet ships code volume fine and opus earns its cost only on judgment stages. |

## Red Flags

- Production code written before its failing test exists.
- Reviewer subagent given the implementation transcript (context bleed).
- Review round 4+ still producing MEDIUM findings — escalate, don't grind.
- `git worktree add` or bare `git stash` in a session that has native tools.
- Branch named `feature/…` (hook rejects) or commit bullets over 120 chars.
- Findings "fixed" without gates re-run before re-review.

## Verification (pipeline exit checklist)

- [ ] Worktree isolated, branch `<type>/<kebab>`, baseline was green at start
- [ ] Spec and plan each got explicit user approval before the next stage
- [ ] Every new behavior has a test that was seen failing first
- [ ] All four gate commands pass; diff-cover ≥90% on changed lines
- [ ] Live test done for contract-boundary changes, findings folded into mocks
- [ ] Final review round returned zero CRITICAL/MEDIUM findings
- [ ] PR open with template filled, LOW/NIT + live evidence recorded
