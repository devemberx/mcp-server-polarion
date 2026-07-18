---
name: dev-pipeline
description: Full feature-development pipeline for this repo — isolated worktree, spec, implementation plan, TDD build, quality gates, then a review→fix loop that only stops when review passes. Each stage delegates to a named project subagent (spec-researcher, pattern-scout, pipeline-implementer, pipeline-simplifier, pipeline-reviewer) with a stage-appropriate model. Use when starting any new MCP tool, feature, or multi-file change from scratch; when the user says "new tool/feature" (any language), "run the pipeline", or asks to develop something "the same way as list_test_records". Triggers on `/dev-pipeline`. NOT for one-file tweaks, doc edits, or work already mid-flight — jump to the matching stage instead.
---

# Dev Pipeline

Encode the sequence that shipped `list_test_records` (PR #170): worktree → spec →
plan → TDD implement → gates → simplify → review loop → ship. Main session is the
**orchestrator**: it sequences stages, holds user approvals, spawns the stage
agents, and merges their reports. Subagents never spawn other subagents
(hard rule here — every paraphrase hop loses information).

```
 0.Worktree → 1.Spec →(approve)→ 2.Plan →(approve)→ 3.Implement(TDD) → 4.Gates
                                                          ▲                │
                                                          │                ▼ (5.Simplify, first pass only)
                                              7.Fix ◄─(findings)─ 6.Review ─(clean)→ 8.Ship
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
   | `.pipeline/spec.md` | orchestrator (Stage 1, draft before approval; approved via file link — single copy; approval freezes it) | pattern-scout, pipeline-implementer, pipeline-reviewer |
   | `.pipeline/plan.md` | orchestrator (Stage 2, approved via file link — single copy; approval freezes it) | pipeline-implementer, pipeline-reviewer |
   | `.pipeline/review-round-N.md` | orchestrator (Stage 6, reviewer report pasted verbatim) | pipeline-implementer (Stage 7), user on escalation |
   | `.pipeline/followups.md` | orchestrator (Stage 6, FOLLOW-UPS + unfixed LOW accumulated per round, deduped) | orchestrator (Stage 8 issue export) |

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
| 5 Simplify | `pipeline-simplifier` | sonnet | code-volume edits over the whole diff; report-only return keeps orchestrator context lean |
| 6 Review | `pipeline-reviewer` | opus | judgment-heavy; fresh context avoids implementer blindness |
| 7 Fix | `pipeline-implementer` (fix mode) | sonnet | bounded edits from findings list |

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

1. **Check the follow-up queue first**: `gh issue list --label follow-up
   --state open` (plus a keyword search for the feature's domain). An open
   issue touching the same domain can change spec decisions — fold it into
   the spec's Related open issues section, absorbed or explicitly deferred
   with a reason. If the pipeline resolves one, the PR body gets `Fixes #N`.
2. **Invoke `spec-researcher`** with: the feature ask, the vendor doc URL(s),
   and which sibling tools to read for conventions. Expect its
   CONTRACT FACTS / QUIRKS / UNVERIFIED report back.
3. Copy [spec-template.md](references/spec-template.md) to `.pipeline/spec.md`
   and fill its slots from the report. The template fixes the required
   sections — don't invent a new shape; they are the standard
   pipeline-reviewer judges the diff against.
4. **If an UNVERIFIED item decides spec content** — whether a field exists,
   whether a guard is needed, which enum backs a value — **and live
   credentials work, burn it down before asking approval.** Probe a
   project-scoped endpoint first (global ones can 403 on a scoped token),
   then settle the researcher's UNVERIFIED items with cheap reads and scratch
   writes (create a disposable resource, probe, delete it — where the API
   allows delete; documents are not REST-deletable). A failed probe on
   one endpoint is not "access is dead" — probe the exact resource the tool
   will touch before concluding. A fact settled at spec time is a design
   decision made once; the same fact discovered after implementation is a
   review-fix round. (update_test_records run, 2026-07-13: a pre-approval
   probe found the real enum path and two silent-ghost writes — flipped a
   guard from "deferred" to "shipped" before any code existed.)
5. **Ask approval over the file, not a re-print.** Post a clickable link to
   `.pipeline/spec.md` — as an absolute path: the worktree sits outside the
   editor's workspace root, so a workspace-relative link may not open — ask
   the user to read it in the editor, and collect the decision via
   AskUserQuestion. Never a summary or translation — approval
   binds only the frozen file's own text — and never a bare "spec ready,
   approve?" prompt without the link. The file was already written once;
   quoting it in chat pays output tokens for a second copy. On a redo, quote
   just the revised sections so the delta is visible in place. Fold feedback
   back into `.pipeline/spec.md`; approval freezes it.
   Spec changes are cheap here, expensive later.

## Stage 2 — Plan

1. **Invoke `pattern-scout`** with: `.pipeline/spec.md` path and the areas to
   sweep (helpers, sibling tool, test mechanics, eval coverage). Expect its
   REUSE / MIRROR / GAPS report. One scout unless scope spans unrelated areas.
2. Draft the plan in the main thread (or a Plan agent for large scope) in the
   shape of [plan-template.md](references/plan-template.md) — one implementer task per
   Changes entry, live-test items from the spec's UNVERIFIED list under
   Verification. Copy the template to `.pipeline/plan.md`, fill it, and ask
   approval the Stage 1 step 5 way: file link (absolute path) +
   AskUserQuestion, no re-print, redo rounds quote only the revised sections.
   Don't enter plan mode here — ExitPlanMode ships the full plan text as a
   tool parameter, a second paid copy of a file that already exists. If the
   session is already in plan mode (user-toggled), ExitPlanMode is the only
   way out: keep its plan text to a short pointer at `.pipeline/plan.md`;
   approval still binds the file.

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
whole picture lives. The TDD law reaches here too: eval-harness handlers and
any other executable glue get their failing test before the code —
diff-cover gates `evals/harness` like `src/`, and a bounced gate is the
expensive way to rediscover that.

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

## Stage 5 — Simplify (first pass only)

Once, after gates first go green and before the first review round — never
repeated after Stage 7 fix rounds (those are bounded edits, not new
complexity):

1. **Commit the work first**, in the repo commit format (CLAUDE.md Repo
   Conventions; full rules `.github/CONTRIBUTING.md`, commit-msg hook
   enforces) — the simplifier diffs `origin/main...HEAD`, and uncommitted
   work is invisible to that range; a new tool is mostly untracked files, so
   skipping this turns the pass into a silent no-op.
2. **Invoke `pipeline-simplifier`** with the worktree path and the diff scope
   (`git diff origin/main...HEAD`). It sweeps reuse/simplification/efficiency/
   altitude and applies the fixes itself — quality only, no bug hunting;
   behavior stays pinned by the Stage 3 tests. Expect its CHANGED / SKIPPED /
   TESTS report back.
3. Re-run Stage 4 gates in the main thread, then commit the simplify edits.
   Gates red on a simplify edit (diff-cover counts its changed lines too):
   `SendMessage` the simplifier to fix or revert that edit — it holds the
   context; don't spawn an implementer for it.

Why before review, not after: reviewer rounds stop burning on quality
findings (those demote to LOW → followups and rarely get fixed), and the
simplify edits themselves still pass through Stage 6 — new code gets
reviewed. Placing it after PASS would ship unreviewed edits.

## Stage 6 — Review (loop entry)

1. **Commit anything still uncommitted** — the reviewer diffs
   `origin/main...HEAD`, and uncommitted changes are invisible to that range.
   The first pass is already committed by Stage 5; fix rounds add their
   commits here. Squash merge collapses them at the end.
2. **Invoke `pipeline-reviewer`** with: `.pipeline/spec.md` and
   `.pipeline/plan.md` paths and the diff scope (`git diff origin/main...HEAD`).
   Give it the spec and plan — **never** the implementer reports or your
   implementation narrative (context bleed defeats the fresh eyes).
3. Save its report verbatim to `.pipeline/review-round-N.md` — full text, not
   a paraphrase (Stage 7's implementer and any escalation read it); relay
   findings to the user.
4. Append the round's FOLLOW-UPS and any LOW items you won't fix to
   `.pipeline/followups.md`, deduped across rounds (drop an item a later
   round fixed). NIT stays PR-notes-only. This file is the only thing that
   survives worktree cleanup — via the Stage 8 export.
5. BREAKING items ride in the round file to Stage 8 for the PR notes. They
   never block the verdict; a shipped-surface change the spec did not
   sanction is a spec-fidelity finding instead, and does block.
6. **Stop criterion — the only exit:** verdict PASS (zero CRITICAL/MEDIUM
   actionable findings). LOW/NIT don't block, but unfixed LOW must be in
   `.pipeline/followups.md` by now. PASS → Stage 8. FAIL → Stage 7.

## Stage 7 — Fix, then back to review

- **Invoke `pipeline-implementer` in fix mode** with the
  `.pipeline/review-round-N.md` path and the CRITICAL/MEDIUM items to address;
  behavior changes go through RED→GREEN again, never patch-and-hope.
- Re-run Stage 4 gates, then **return to Stage 6** — a fresh
  `pipeline-reviewer` spawn over the new diff, not a reply to the old one.
- Loop cap: after 3 review rounds with open findings, stop and escalate to the
  user with `.pipeline/review-round-*.md` — grinding further usually means the
  spec or plan is wrong, not the code.

## Stage 8 — Ship (main thread)

- Work is already committed by review time (Stage 5.1, fix rounds 6.1);
  commit here only what is still uncommitted. Commit format, PR checklist handling, squash-merge
  rule: CLAUDE.md Repo Conventions / `.github/CONTRIBUTING.md` — restating
  them here would be a third copy that drifts. `.pipeline/` stays uncommitted.
- **Export follow-ups before cleanup** — `.pipeline/` dies with the worktree,
  so each `.pipeline/followups.md` item becomes an issue:
  `gh issue create --label follow-up` with `### Origin` / `### Finding` /
  `### Suggested fix` body sections (validate_issue.py hook checks the shape
  against `.github/ISSUE_TEMPLATE/follow_up.yml`). One issue per item —
  independently closeable. An item the user explicitly drops instead is noted
  in PR notes, not filed.
- PR Notes: record live-test evidence, NIT findings, and links to the filed
  follow-up issues; add `Fixes #N` for any follow-up issue this PR resolved.
  List every reviewer BREAKING item verbatim, plus the consequence once: a
  release containing these must bump major and rerun the full eval suite —
  the deploy skill reads version policy from here. (Deferring via a
  deprecation alias is a spec decision; an aliased rename never breaks the
  surface, so it produces no BREAKING item at all.)
- CI: after opening the PR, `gh pr checks <PR#> --watch --fail-fast` until
  every check is green — merge is blocked on red anyway, but an unwatched red
  PR rots until a human notices; catch it while the session context is hot.
  Right after `gh pr create` it can exit "no checks reported" before check
  runs register — wait a few seconds and rerun, don't skip the watch.
  A red check is a Stage 4 gate failure, not a review finding: fix, re-run
  gates, and if code changed re-enter Stage 6. Protection is strict-mode, so
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
| "Main-thread glue is too small for TDD" | A fake-server handler shipped code-first once and diff-cover bounced the gate; the failing test first was the cheaper path. |
| "The user approved my summary of it" | A summary is your interpretation. Approval binds only the frozen file's text — link the file and have them read it, never summarize. |
| "Live checks belong in Stage 4" | When an UNVERIFIED item shapes the spec, a smoke probe before freeze beats re-planning after it — the create_test_records run flipped three guard decisions that way. |
| "The reviewer will catch the complexity" | Quality findings demote to LOW → followups and rarely get fixed. The Stage 5 simplify pass before review is the cheap path. |

## Red Flags

- Production code written before its failing test exists.
- Implementer report accepted without RED evidence.
- Approval requested over a summary/translation instead of the frozen file —
  link the file itself; redo rounds quote only the revised sections.
- Spec or plan re-printed in full in chat — the file is the single paid copy;
  approval goes through its link.
- Simplifier invoked over uncommitted work — `origin/main...HEAD` is empty,
  the pass silently no-ops; commit first (Stage 5.1).
- Reviewer given implementer reports or conversation summaries (context bleed).
- Review round 4+ still producing MEDIUM findings — escalate, don't grind.
- `.pipeline/` files committed, or a subagent prompted without the file paths
  it needs (it cannot see the conversation).
- `git worktree add` or bare `git stash` in a session that has native tools.
- Branch named `feature/…` — pre-push hook rejects it, but only at push time;
  rename at Stage 0.
- Findings "fixed" without gates re-run before re-review.
- Main-thread integration code (eval-harness handler, registration) written
  before its failing test.
- `.pipeline/review-round-N.md` holding a paraphrase instead of the
  reviewer's full report.
- Spec frozen with an UNVERIFIED item that decides tool shape while the
  live server was reachable.
- Worktree cleaned up while `.pipeline/followups.md` still holds unexported
  items — they die with it.

## Verification (pipeline exit checklist)

- [ ] Worktree isolated, branch `<type>/<kebab>`, baseline was green at start
- [ ] Spec and plan each approved via frozen-file link (never a summary or
      full re-print), frozen in `.pipeline/`; reviewer reports stored verbatim
      in `.pipeline/review-round-*.md`
- [ ] Every implementer report carried RED-then-GREEN evidence
- [ ] All five gate commands pass (pytest, ruff check, ruff format --check,
      mypy, diff-cover); diff-cover ≥90% on changed lines
- [ ] Stage 5 simplify pass ran once (`pipeline-simplifier`), gates re-run
      green after
- [ ] Live test done for contract-boundary changes, findings folded into mocks
- [ ] Final `pipeline-reviewer` verdict is PASS (zero CRITICAL/MEDIUM)
- [ ] Every `.pipeline/followups.md` item filed as a `follow-up` issue (or
      explicitly dropped with the user) before worktree cleanup
- [ ] Reviewer BREAKING items (if any) listed in PR notes with the deploy
      consequence stated there
- [ ] PR open with template filled, NIT + live evidence + follow-up issue
      links recorded
- [ ] PR CI checks all green (`gh pr checks <PR#> --watch --fail-fast`),
      branch up to date with main
