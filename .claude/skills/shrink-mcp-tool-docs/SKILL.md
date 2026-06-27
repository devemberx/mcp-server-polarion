---
name: shrink-mcp-tool-docs
description: Shrink LLM-facing MCP tool descriptions — `@mcp.tool` docstrings and `Field(description=...)` — to eval-failure boundary via compress→eval→compress loop with snapshot+rollback, all gates green. Use when asked shrink/compress/optimize/token-diet tool descriptions, docstrings, or param descriptions. NOT for ordinary code comments (use compress-code-comments). Triggers on `/shrink-mcp-tool-docs`.
---

# Shrink Tool Descriptions

Goal: minimize chars (tokens) shipped to client LLM through `@mcp.tool` descriptions — tool `description` (whole docstring) and `Field(description=...)` param descriptions — push to **failure boundary** while keeping eval gate GREEN. Run as **compress → eval → compress** loop: probe boundary, adopt **last GREEN before it**. RED never committed — RED mean "one cut too far."

Be deliberate. One tool at a time. Each cut small delta from current GREEN, never jump to absolute terse form. Snapshot before every cut. On RED, **bisect** cut set — never full-revert to original (net-zero trap). Reproduce RED before trusting it (`min_pass_rate=1.0` make single RED possibly noise).

## What actually ships to the LLM (reprove every run — Step 0)

Repo does **not** use Google-style `Args:`/`Returns:` docstrings. Verified surfaces:

- **Tool `description` = ENTIRE docstring.** No section stripped — whole prose ships. **Primary target; editing it docstring-only.**
- **Param descriptions = `Field(description=...)` in signature, NOT docstring.** Ship in input schema (`properties[p].description`); docstring edit **cannot** touch them.
- **No `Returns:`/`Raises:`/`Example:` blocks exist** to strip — no free text there to delete.

Don't trust list blind — reprove with Step-0 dump each run; refactor or FastMCP change can move text between surfaces.

### Two scopes

- **Scope A — docstring-only (default).** Compress docstring prose → tool `description`. Signature untouched. Fully honors "signature/return/logic unchanged."
- **Scope B — Field-description pass (opt-in; needs explicit operator go-ahead).** Also compress `Field(description=...)` strings. Touch **only** `description=` string — never type, `default`, `min_length`/`max_length`, or validators. Params often big share of LLM-facing chars (~40% on param-heavy tools, per Step-0 dump), so Scope B roughly doubles reachable savings.

If operator not opted into Scope B, **every `Field(description=...)` no-op for your edits** — exclude from reducible baseline, report as such.

## Step 0 — dump schema + record baseline (always first)

Dump what reaches client, confirm edits change it. `mcp.list_tools()` async, returns `list[FunctionTool]` with `.name`, `.description`, `.parameters`.

```bash
REPO=$(git rev-parse --show-toplevel)   # capture repo root before leaving it
cd /tmp && uv run --project "$REPO" python \
  "$REPO"/.claude/skills/shrink-mcp-tool-docs/scripts/dump_schema.py
```

Run from `/tmp` so repo `.env` not auto-loaded (poisons `PolarionConfig` if it holds `OPENAI_API_KEY`). Record per tool: `desc` chars (Scope A target), `param` chars (Scope B target), `total`. Sort descending — long tools = biggest savings first. Dump authoritative — never assume fixed tool count or total; set drifts. If tokenizer (`tiktoken`) available, also note token counts; else chars primary metric.

Per param, confirm schema description text matches what you intend to edit. Param whose text lives in `Field(description=...)` won't move under Scope-A edit — flag `no-op (Scope B)`.

## Compression principles (LLM-facing text only)

**Prose (→ `description`, most important)**
- "When to call / trigger" over "what it does." `"Updates a work item"` → `"Call to change fields on an existing work item; fetch it first."`
- One **boundary** line vs nearest sibling tool ("this tool for X; for Y use `other_tool`").
- One **negative** line only if misuse likely ("not for moving between documents — use `move_*`").
- Keep **pointers to runtime tools** LLM must chain to (`list_work_item_enum_options`, `get_sql_query_recipes`, `move_work_item_to_document`).

**Field descriptions (→ schema params; Scope B only)**
- Keep: format/constraint (ID shape, date format, enum allowed values), and **source** of value from another tool's output.
- Drop: type/required already stated by signature; descriptions self-evident from name.

**Form**
- One line each. No dev-narrative ("we tried X"). No `WARNING:`/`NOTE:` prefixes. No banner comments. Consistent tense/terms/format.

## The optimization loop

**Global prep**
1. Work on branch off `main` (e.g. `chore/shrink-mcp-tool-docs`) — never run loop on `main`. Each adopted GREEN committed, so per-cut rollback just `git restore <file>` (or `git checkout -- <file>`) back to that commit — no stash juggling; working tree only ever holds in-flight cut. Whole session lands as one squashed commit/PR.
2. **Match publish gate's model, or don't run.** Failure boundary model-specific, so optimize against *exact* model publish gate uses — `EVAL_MODEL`'s default `openai/gpt-4o-mini` (CI doesn't override). Needs `OPENAI_API_KEY`; if unset, **STOP and report** — never substitute local/ollama model, boundary found on different model REDs real gate. Then assume branch starts GREEN (released tip), run **one Fast gate** to confirm harness runs and green — not full Confirm; re-proving already-green tip with 10 runs wasted cost. Fast RED at entry = pre-existing breakage → STOP (not yours to fix here). That first GREEN your baseline; checkpoint it.
3. Step-0 dump + baseline chars.
4. Sort tools by LLM-facing chars **descending**.

**Cut risk classes** (L1 safest → L4 riskiest; draw candidate cuts in this order, cheapest-risk first — these *risk ordering of edits*, not atomic rungs to jump to):
- **L1** — bits restated by signature / self-evident clauses / trivially redundant prose (Scope B: redundant param trims if opted in).
- **L2** — rewrite prose what→when(trigger); compress boundary + negative to one line each.
- **L3** — prose toward trigger line + boundary line; params (Scope B) toward format/constraint/source only.
- **L4 (floor-adjacent)** — trigger line + only load-bearing enum/format/source. No further.

**Per tool (incremental descent + bisection)** — each cut small delta from *current GREEN*, never jump to absolute terse form; RED recovered by **bisecting cut set**, never reverting to original (revert-to-original net-zero trap):
1. Last GREEN already committed (step 2 / 4-a), so to roll back any cut, `git restore <file>` returns file to that committed GREEN — no separate snapshot needed.
2. **First cut always gentlest** — L1 redundancy only. Gate → commit as tool's first GREEN baseline **before** any rewrite. Guarantees ≥1 committed GREEN for every tool with slack, so later overshoot can never zero tool out.
3. Build next **candidate-cut set**: independent edits from lowest unfinished risk class. Target ~15–20% of remaining reducible chars per step — delta, not leap to floor.
4. Apply whole set, **Fast gate** → GREEN: go to 4-a. RED: go to 4-b.
4-a. Fast GREEN → run **Confirm gate**:
   - Confirm GREEN → commit as **new GREEN baseline**, refresh baseline chars, **re-sort**, build next set (step 3).
   - Confirm RED (variance or overfit) → revert to last GREEN, **lock** this tool, move to next tool.
4-b. Fast RED — **bisect candidate set, do NOT full-revert**:
   - Split set **mechanically by risk class**: safer half = lowest-Lx cuts (L1 / redundant-param trims), riskier half = higher-Lx ones (trigger/boundary/enum rewrites). If every cut same class, split by position into two equal halves (first vs second) — don't agonize over which *feels* riskier. Use failing transcript only to explain **why** a cut regressed (which tool mis-selected or mis-parametrized), not to choose split.
   - Drop riskier half, re-gate safer half. GREEN → commit that subset, then re-attempt dropped cuts **one at a time** (step 3). RED → recurse: bisect still-applied set again.
   - Single cut alone RED → that phrase load-bearing → restore **only it**, keep rest, lock out of future sets.
   - `min_pass_rate=1.0`: **reproduce RED once** (re-run Fast) before calling cut load-bearing — lone RED can be noise.
5. Repeat from 3 until reducible set exhausted or every remaining single cut RED → **lock** at last GREEN.

**Invariant:** RED discards only last delta, never accumulated GREEN. Net-zero for tool happens only if its very first L1 trim RED — meaning already at floor.

**Confirm-gate cost:** smaller deltas mean more iterations, so don't run (expensive) Confirm gate per delta far from floor. For early L1 deltas, batch up to ~3–5 Fast-GREEN deltas (or one full risk-class pass, whichever first) and Confirm once before switching risk class; near boundary, Confirm per delta. Batched Confirm RED bisects batch.

**Global stop:** all tools locked; or full pass's cumulative saving < ~50 chars; or operator-set iteration cap hit. **On stop, re-run all gates at last GREEN baseline, confirm green.** Submission = last GREEN, never RED.

## Gates

**Preconditions** (once): `uv sync --group evals` — `strands_evals` not in default env. Eval agent's model from `EVAL_MODEL` (default `openai/gpt-4o-mini`), publish gate uses that default — so **leave at default, provide `OPENAI_API_KEY`**. Optimizing against different model (e.g. local ollama) unsound vs gate (see Global prep 2). Run eval/pytest from CWD outside repo when repo `.env` carries `OPENAI_API_KEY` (shadows `PolarionConfig`); `cd /tmp && uv run --project <repo> ...` avoids it.

**Fast gate** (cheap triage after each cut):
```bash
uv run python -m evals.run --runs 1
uv run pytest tests/mcp_server_polarion/test_mcp_transport.py -q
```
- Gate runs **all behaviour categories**: `triggers`/`safety` (`min_pass_rate=1.0`) **and** `efficiency`/`orchestration` (`0.8`). Docstring cuts regress **both kinds** — too-terse description can make agent take wasteful path (efficiency) even when it never does forbidden action (safety). Read gate summary, not just exit code.
- During diagnosis target one case: `uv run python -m evals.run --case <NAME> --runs 1` (browse case names + intents with `uv run python -m evals.run --list`).

**Confirm gate** (before committing/locking a baseline — kills variance + overfit):
```bash
uv run python -m evals.run --runs 5     # raise toward boundary; full default = EVAL_RUNS (10)
uv run pytest
uv run ruff check . && uv run ruff format . && uv run mypy src/
```
- **Variance:** as cuts shrink, effect size shrinks and noise vs real regression blurs → raise `--runs` near boundary. GREEN/RED that flaps across runs = unstable cut → do **not** adopt; keep last stable GREEN.
- **Overfit:** reserve held-out subset of case names (pick a few via `--case`), **never run during loop**, run only at Confirm. Held-out RED = overfit → revert that cut.

## Floor (never cut below — load-bearing)

Trigger/when line · one sibling-boundary line when near-duplicate tool exists · enum allowed-values and ID/date formats · source of values from other tools' output · pointers to runtime-callable tools (`get_sql_query_recipes`, `list_*_enum_options`, `move_*`). Transport test asserts every `description` **non-empty** — empty description guaranteed RED.

Keep constraint/format phrasings **byte-exact** — never abbreviate single character. Load-bearing because agent copies them verbatim into payloads:
- ID shapes: `MCPT-123`, 5-segment link id, 3-segment module id form.
- Date/time format: `YYYY-MM-DDThh:mm:ssZ`.
- Enum allowed-value lists: `one of: open, inProgress, done`.

Shortening any of these (e.g. `YYYY-MM-DD` → "a date") can make agent emit malformed value even when eval happens to pass on that run.

## Absolute rules

1. **Final submission = last GREEN baseline, all gates green:**
   - `uv run python -m evals.run` (triggers/safety all pass at 1.0; efficiency/orchestration at 0.8)
   - `uv run pytest` (esp. `tests/mcp_server_polarion/test_mcp_transport.py` and `tests/evals/`)
   - `uv run ruff check . && uv run ruff format . && uv run mypy src/`
2. **Scope A: docstring only.** Scope B (opt-in): only `description=` string of `Field(...)`; never type/default/constraint/logic. Signatures, return types, behavior unchanged in both.
3. Docstrings stay **self-contained** — but keep runtime-tool pointers (`get_sql_query_recipes`, etc.).
4. **Never commit/submit RED.** RED boundary signal; adopted state always prior GREEN.
5. Tool count unchanged → leave `EXPECTED_TOOL_NAMES` alone. Don't touch `get_sql_query_recipes` guide body (transport test asserts its content).

## Output format

1. **Step-0 result:** which surface each string ships on, Scope-B param list (no-op-for-Scope-A set), baseline char table.
2. **Per tool** (LLM-facing chars descending): before/after diff + one-line rationale ("what cut and why") + adopted risk class (Lx) and final cut set (post-bisection) + char (and token, if available) savings.
3. **Loop log:** one row per iteration `[tool | cut (Lx set / bisect-subset / single restore) | Fast | Confirm | decision (commit/bisect/lock)]`; each RED cut + its diagnosis (which tool mis-selected and why) + bisection outcome on one line.
4. **Final gate captures:** all gates green at last GREEN baseline.
5. **Total LLM-facing savings:** summed chars (+ token estimate) + per-tool before→after table.