---
name: dev-pipeline
description: Full feature-development pipeline for this repo — isolated worktree, spec, implementation plan, TDD build, quality gates, then a review→fix loop that only stops when review passes. Each stage delegates to a named project subagent (spec-researcher, pattern-scout, pipeline-implementer, pipeline-reviewer) with a stage-appropriate model. Use when starting any new MCP tool, feature, or multi-file change from scratch; when the user says "new tool/feature" (any language), "run the pipeline", or asks to develop something "the same way as list_test_records". Triggers on `/dev-pipeline`. NOT for one-file tweaks, doc edits, or work already mid-flight — jump to the matching stage instead.
---

# Dev Pipeline

Encode the sequence that shipped `list_test_records` (PR #170): worktree → spec →
plan → TDD implement → gates → review loop → ship. Main session is the
**orchestrator**: it sequences stages, holds user approvals, spawns the stage
agents, and merges their reports. Subagents never spawn other subagents
(hard rule here — every paraphrase hop loses information).

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
   | `.pipeline/spec.md` | orchestrator (Stage 1, draft before approval; approval freezes it) | pattern-scout, pipeline-implementer, pipeline-reviewer |
   | `.pipeline/plan.md` | orchestrator (Stage 2, full text shown before approval — via ExitPlanMode or quoted draft; approval freezes it) | pipeline-implementer, pipeline-reviewer |
   | `.pipeline/review-round-N.md` | orchestrator (Stage 5, from reviewer report) | pipeline-implementer (Stage 6), user on escalation |

Follow-up questions to an agent you already spawned: `SendMessage` to its id —
a fresh Agent call starts cold and re-derives everything.

## Stage → agent → model

| Stage | Invoke | Model | Why |
|---|---|---|---|
| 0 Worktree setup | main thread | — | harness tools (EnterWorktree), no judgment needed |
| 1 Spec research | `spec-researcher` | opus | wrong contract fact poisons every later stage; judgment on source trust worth the cost |
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

1. `git fetch origin` first — EnterWorktree branches from `origin/main`
   (default `worktree.baseRef: fresh`), a remote-tracking ref that moves only
   on fetch; skip this and parallel sessions branch from a stale base, hitting
   conflicts and strict-mode update-branch churn at merge time.
2. `EnterWorktree` (never bare `git worktree add` — native tool tracks cleanup).
3. Rename the generated branch to `<type>/<kebab-summary>` — pre-push hook
   accepts only `feat|fix|refactor|test|docs|chore|ci` prefixes (`feat/`, not
   `feature/`).
4. Symlink untracked configs from the main checkout: `.env`, `.mcp.json`,
   `.claude/settings.local.json`, `.vscode/settings.json`.
5. `uv sync --dev --group evals`, then baseline `uv run pytest` — must be green
   before any change, else you can't tell new breakage from old.
6. `mkdir -p .pipeline` for the handoff files.

## Stage 1 — Spec

1. **Invoke `spec-researcher`** with: the feature ask, the vendor doc URL(s),
   and which sibling tools to read for conventions. Expect its
   CONTRACT FACTS / QUIRKS / UNVERIFIED report back.
2. Copy [spec-template.md](references/spec-template.md) to `.pipeline/spec.md`
   and fill its slots from the report. The template fixes the required
   sections — don't invent a new shape; they are the standard
   pipeline-reviewer judges the diff against.
3. **If live credentials work, burn down UNVERIFIED before approval.** Probe a
   project-scoped endpoint first (global ones can 403 on a scoped token), then
   settle the researcher's UNVERIFIED items with cheap reads and scratch
   writes (create a disposable resource, probe, delete it). A fact settled at
   spec time is a design decision made once; the same fact discovered after
   implementation is a review-fix round. (Run 2026-07-13: a pre-approval probe
   found the real enum path and two silent-ghost writes — flipped a guard from
   "deferred" to "shipped" before any code existed.)
4. **Show the user the full spec, then ask approval.** The approval request
   quotes the spec content (or the revised sections on a redo) in the reply —
   the user approves text they have read, never a bare "spec ready, approve?"
   prompt. Fold feedback back into `.pipeline/spec.md`; approval freezes it.
   Spec changes are cheap here, expensive later.

## Stage 2 — Plan

1. **Invoke `pattern-scout`** with: `.pipeline/spec.md` path and the areas to
   sweep (helpers, sibling tool, test mechanics, eval coverage). Expect its
   REUSE / MIRROR / GAPS report. One scout unless scope spans unrelated areas.
2. Draft the plan in the main thread (or a Plan agent for large scope) in the
   shape of [plan-template.md](references/plan-template.md) — one implementer task per
   Changes entry, live-test items from the spec's UNVERIFIED list under
   Verification. Use plan mode when available — ExitPlanMode already puts the
   full plan in front of the user for approval; write it to
   `.pipeline/plan.md` right after. Without plan mode, copy the template to
   `.pipeline/plan.md`, fill it, and quote it in the approval request — same
   rule as the spec: the user approves text they have read.

## Stage 3 — Implement (TDD)

Follow superpowers:test-driven-development — it is the law here, not a style.

For each plan task, in dependency order: **invoke `pipeline-implementer`**
with the `.pipeline/spec.md` + `.pipeline/plan.md` paths, the single task
excerpt, and its file:line references. Expect CHANGED / EVIDENCE (RED then
GREEN output) / DEVIATIONS back — a report without RED evidence means the task
was not done TDD; reject it and rerun. RED form depends on the task: a
brand-new symbol or module may show a collection-time ImportError as its RED;
changed behavior on existing code needs an assertion-level failure — an
ImportError there means the test never exercised the behavior.

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

1. **Commit the work first**, in Stage 7's message format — the reviewer
   diffs `origin/main...HEAD`, and uncommitted changes are invisible to that
   range. Fix rounds add commits; squash merge collapses them at the end.
2. **Invoke `pipeline-reviewer`** with: `.pipeline/spec.md` and
   `.pipeline/plan.md` paths and the diff scope (`git diff origin/main...HEAD`).
   Give it the spec and plan — **never** the implementer reports or your
   implementation narrative (context bleed defeats the fresh eyes).
3. Save its report to `.pipeline/review-round-N.md`; relay findings to the user.
4. **Stop criterion — the only exit:** verdict PASS (zero CRITICAL/MEDIUM
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
- CI: after opening the PR, `gh pr checks <PR#> --watch --fail-fast` until
  every check is green — merge is blocked on red anyway, but an unwatched red
  PR rots until a human notices; catch it while the session context is hot.
  Right after `gh pr create` it can exit "no checks reported" before check
  runs register — wait a few seconds and rerun, don't skip the watch.
  A red check is a Stage 4 gate failure, not a review finding: fix, re-run
  gates, and if code changed re-enter Stage 5. Protection is strict-mode, so
  if main moved, update the branch and let checks re-run.

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
- Approval requested without the full spec/plan text in front of the user.
- Reviewer given implementer reports or conversation summaries (context bleed).
- Review round 4+ still producing MEDIUM findings — escalate, don't grind.
- `.pipeline/` files committed, or a subagent prompted without the file paths
  it needs (it cannot see the conversation).
- `git worktree add` or bare `git stash` in a session that has native tools.
- Branch named `feature/…` (hook rejects) or commit bullets over 120 chars.
- Findings "fixed" without gates re-run before re-review.

## Verification (pipeline exit checklist)

- [ ] Worktree isolated, branch `<type>/<kebab>`, baseline was green at start
- [ ] Spec and plan each shown in full to the user, approved, frozen in `.pipeline/`
- [ ] Every implementer report carried RED-then-GREEN evidence
- [ ] All five gate commands pass (pytest, ruff check, ruff format --check,
      mypy, diff-cover); diff-cover ≥90% on changed lines
- [ ] Live test done for contract-boundary changes, findings folded into mocks
- [ ] Final `pipeline-reviewer` verdict is PASS (zero CRITICAL/MEDIUM)
- [ ] PR open with template filled, LOW/NIT + live evidence recorded
- [ ] PR CI checks all green (`gh pr checks <PR#> --watch --fail-fast`),
      branch up to date with main
