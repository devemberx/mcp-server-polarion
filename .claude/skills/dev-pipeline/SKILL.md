---
name: dev-pipeline
description: Full feature-development pipeline for this repo — isolated worktree, spec, implementation plan, TDD build, quality gates, then a review→fix loop that only stops when review passes. Each stage delegates to a named project subagent (spec-researcher, pattern-scout, pipeline-implementer, pipeline-reviewer) with a stage-appropriate model. Use when starting any new MCP tool, feature, or multi-file change from scratch; when the user says "새 도구", "new tool/feature", "run the pipeline", or asks to develop something "the same way as list_test_records". Triggers on `/dev-pipeline`. NOT for one-file tweaks, doc edits, or work already mid-flight — jump to the matching stage instead.
---

# Dev Pipeline

Encode the sequence that shipped `list_test_records` (PR #170): worktree → spec →
plan → TDD implement → gates → review loop → ship. Main session is the
**orchestrator**: it sequences stages, holds user approvals, spawns the stage
agents, and merges their reports. Subagents never spawn other subagents
(platform constraint, and paraphrase hops lose information).

```
 0.Worktree → 1.Spec →(approve)→ 2.Plan →(approve)→ 3.Implement(TDD) → 4.Gates
                                                          ▲                │
                                                          │                ▼
                                              6.Fix ◄─(findings)─ 5.Review ─(clean)→ 7.Ship
```

## Data handoff — how stages talk

Subagents share **no memory and no conversation history**. Three channels only:

1. **Prompt in** — a subagent sees exactly what you put in its `prompt` plus its
   own agent definition. Always pass the handoff-file paths and the task
   excerpt; never assume it "knows" what was discussed.
2. **Report out** — its final message returns as the tool result. Each agent
   definition fixes a report format; the report is the interface. Relay what
   matters to the user — they don't see tool results.
3. **Files** — the durable channel. This pipeline writes its artifacts to
   `.pipeline/` in the worktree (gitignored, never committed):

   | File | Written by | Read by |
   |---|---|---|
   | `.pipeline/spec.md` | orchestrator (Stage 1, after approval) | pattern-scout, pipeline-implementer, pipeline-reviewer |
   | `.pipeline/plan.md` | orchestrator (Stage 2, after approval) | pipeline-implementer, pipeline-reviewer |
   | `.pipeline/review-round-N.md` | orchestrator (Stage 5, from reviewer report) | pipeline-implementer (Stage 6), user on escalation |

Follow-up questions to an agent you already spawned: `SendMessage` to its id —
a fresh Agent call starts cold and re-derives everything.

## Stage → agent → model

| Stage | Invoke | Model | Why |
|---|---|---|---|
| 0 Worktree setup | main thread | — | harness tools (EnterWorktree), no judgment needed |
| 1 Spec research | `spec-researcher` | sonnet | verified contract facts; conclusions matter, not transcripts |
| 1 Spec writing | main thread | — | needs full user context + approval dialogue |
| 2 Plan exploration | `pattern-scout` | sonnet | file:line reuse hunting, read-only |
| 2 Plan design | main thread (Plan agent for big scope) | opus for Plan agent | architecture trade-offs |
| 3 Implement | `pipeline-implementer` × 1 per plan task | sonnet | code volume; escalate model per rule below |
| 4 Gates | main thread | — | deterministic bash; a subagent relays an exit code for tokens |
| 5 Review | `pipeline-reviewer` | opus | judgment-heavy; fresh context avoids implementer blindness |
| 6 Fix | `pipeline-implementer` (fix mode) | sonnet | bounded edits from findings list |

Escalation rule: move one model tier up (`model` param on the Agent call
overrides the definition) when a stage keeps failing or the domain is
unfamiliar; one tier down for mechanical work. Parallel spawns only for
**independent** research — implementation tasks with dependencies run
sequentially, in one Agent call each.

## Stage 0 — Worktree (main thread)

1. `EnterWorktree` (never bare `git worktree add` — native tool tracks cleanup).
2. Rename the generated branch to `<type>/<kebab-summary>` — pre-push hook
   accepts only `feat|fix|refactor|test|docs|chore|ci` prefixes (`feat/`, not
   `feature/`).
3. Symlink untracked configs from the main checkout: `.env`, `.mcp.json`,
   `.claude/settings.local.json`, `.vscode/settings.json`.
4. `uv sync --dev --group evals`, then baseline `uv run pytest` — must be green
   before any change, else you can't tell new breakage from old.
5. `mkdir -p .pipeline` for the handoff files.

## Stage 1 — Spec

1. **Invoke `spec-researcher`** with: the feature ask, the vendor doc URL(s),
   and which sibling tools to read for conventions. Expect its
   CONTRACT FACTS / QUIRKS / UNVERIFIED report back.
2. Write the spec in the main thread from that report: tool signature, response
   model fields (minimal — defer detail to a future `get_*`), request params,
   error mapping, files to touch, and the UNVERIFIED items as open
   live-verification questions.
3. **Stop and get user approval.** Then write the approved spec to
   `.pipeline/spec.md`. Spec changes are cheap here, expensive later.

## Stage 2 — Plan

1. **Invoke `pattern-scout`** with: `.pipeline/spec.md` path and the areas to
   sweep (helpers, sibling tool, test mechanics, eval coverage). Expect its
   REUSE / MIRROR / GAPS report. One scout unless scope spans unrelated areas.
2. Draft the plan in the main thread (or a Plan agent for large scope):
   Context / Branch / Changes (per file, citing the scout's file:line refs) /
   Verification (incl. live-test items from the spec's UNVERIFIED list) /
   Commit-PR. Use plan mode when available; get approval; write the approved
   plan to `.pipeline/plan.md`.

## Stage 3 — Implement (TDD)

Follow superpowers:test-driven-development — it is the law here, not a style.

For each plan task, in dependency order: **invoke `pipeline-implementer`**
with the `.pipeline/spec.md` + `.pipeline/plan.md` paths, the single task
excerpt, and its file:line references. Expect CHANGED / EVIDENCE (RED then
GREEN output) / DEVIATIONS back — a report without RED evidence means the task
was not done TDD; reject it and rerun.

Keep cross-task integration (imports, registration lists like
`EXPECTED_TOOL_NAMES`, eval coverage entry) in the main thread, where the
whole picture lives.

## Stage 4 — Gates (main thread; all must pass before review)

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

1. **Invoke `pipeline-reviewer`** with: `.pipeline/spec.md` and
   `.pipeline/plan.md` paths and the diff scope (`git diff origin/main...HEAD`).
   Give it the spec and plan — **never** the implementer reports or your
   implementation narrative (context bleed defeats the fresh eyes).
2. Save its report to `.pipeline/review-round-N.md`; relay findings to the user.
3. **Stop criterion — the only exit:** verdict PASS (zero CRITICAL/MEDIUM
   actionable findings). LOW/NIT go to PR notes, not necessarily fixed.
   PASS → Stage 7. FAIL → Stage 6.

## Stage 6 — Fix, then back to review

- **Invoke `pipeline-implementer` in fix mode** with the
  `.pipeline/review-round-N.md` path and the CRITICAL/MEDIUM items to address;
  behavior changes go through RED→GREEN again, never patch-and-hope.
- Re-run Stage 4 gates, then **return to Stage 5** — a fresh
  `pipeline-reviewer` spawn over the new diff, not a reply to the old one.
- Loop cap: after 3 review rounds with open findings, stop and escalate to the
  user with `.pipeline/review-round-*.md` — grinding further usually means the
  spec or plan is wrong, not the code.

## Stage 7 — Ship (main thread)

- Commit: `type(scope): summary` ≤50 chars + 2-bullet body (motivation,
  change), bullets ≤120 chars — hook enforces. `.pipeline/` stays uncommitted.
- PR: flip template checkboxes `[ ]`→`[x]`, keep unchecked options, record
  live-test evidence and unfixed LOW/NIT findings in Notes. Squash merge only.

## Common Rationalizations

| Excuse | Reality |
|---|---|
| "Small feature, skip the spec" | Spec took 10 min for list_test_records and caught a missing-Lucene-support surprise before any code existed. |
| "I'll run tests after implementing" | A test that never failed proves nothing. RED first is the whole point. |
| "Mocks cover it, skip the live test" | Live run found bare-GUID user ids and a missing `meta.totalCount` that no mock predicted. |
| "Review myself in-context, faster" | The implementer's context contains its own justifications. Fresh context finds what you rationalized. |
| "Give the reviewer my implementation summary for context" | That summary is the rationalization you need it to not have. Spec + plan + diff only. |
| "One more fix round, no re-review" | Fixes are new code; new code gets reviewed. That's why the loop exists. |
| "Subagent for the gate commands too" | Gates are deterministic bash. A subagent burns tokens to relay an exit code. |
| "Opus everywhere to be safe" | Model choice is a cost/judgment trade-off; sonnet ships code volume fine, opus earns its cost only on judgment stages. |

## Red Flags

- Production code written before its failing test exists.
- Implementer report accepted without RED evidence.
- Reviewer given implementer reports or conversation summaries (context bleed).
- Review round 4+ still producing MEDIUM findings — escalate, don't grind.
- `.pipeline/` files committed, or a subagent prompted without the file paths
  it needs (it cannot see the conversation).
- `git worktree add` or bare `git stash` in a session that has native tools.
- Branch named `feature/…` (hook rejects) or commit bullets over 120 chars.
- Findings "fixed" without gates re-run before re-review.

## Verification (pipeline exit checklist)

- [ ] Worktree isolated, branch `<type>/<kebab>`, baseline was green at start
- [ ] Spec and plan each got explicit user approval, then landed in `.pipeline/`
- [ ] Every implementer report carried RED-then-GREEN evidence
- [ ] All four gate commands pass; diff-cover ≥90% on changed lines
- [ ] Live test done for contract-boundary changes, findings folded into mocks
- [ ] Final `pipeline-reviewer` verdict is PASS (zero CRITICAL/MEDIUM)
- [ ] PR open with template filled, LOW/NIT + live evidence recorded
