---
name: shrink-mcp-tool-docs
description: Shrink LLM-facing MCP tool descriptions — `@mcp.tool` docstrings and `Field(description=...)` — to the eval-failure boundary via a compress→eval→compress loop with snapshot+rollback, all gates green. Use when asked to shrink/compress/optimize/token-diet tool descriptions, docstrings, or parameter descriptions. NOT for ordinary code comments (use compress-code-comments). Triggers on `/shrink-mcp-tool-docs`.
---

# Shrink Tool Descriptions

Goal: minimize the characters (tokens) shipped to the client LLM through `@mcp.tool` descriptions — the tool `description` (the whole docstring) and the `Field(description=...)` param descriptions — pushing to the **failure boundary** while keeping the eval gate GREEN. Operate as a **compress → eval → compress** loop that probes the boundary and adopts the **last GREEN before it**. RED is never committed — RED only means "one cut too far."

Be deliberate. One tool at a time; each cut is a small delta from the current GREEN, never a jump to an absolute terse form. Snapshot before every cut. On RED, **bisect** the cut set — never full-revert to original (that is the net-zero trap). Reproduce a RED before trusting it (`min_pass_rate=1.0` makes a single RED possibly noise).

## What actually ships to the LLM (reprove every run — Step 0)

This repo does **not** use Google-style `Args:`/`Returns:` docstrings. Verified surfaces:

- **Tool `description` = the ENTIRE docstring.** No section is stripped — the whole prose ships. **Primary target; editing it is docstring-only.**
- **Param descriptions = `Field(description=...)` in the signature, NOT the docstring.** They ship in the input schema (`properties[p].description`), but a docstring edit **cannot** touch them.
- **No `Returns:`/`Raises:`/`Example:` blocks exist** to strip — there is no free text to delete there.

Don't trust this list blind — reprove it with the Step-0 dump each run; a refactor or FastMCP change can move text between surfaces.

### Two scopes

- **Scope A — docstring-only (default).** Compress the docstring prose → tool `description`. Signature untouched. Fully honors "signature/return/logic unchanged."
- **Scope B — Field-description pass (opt-in; requires explicit operator go-ahead).** Also compress `Field(description=...)` strings. Touch **only** the `description=` string — never the type, `default`, `min_length`/`max_length`, or validators. Params are ~40% of LLM-facing chars (e.g. `update_work_item`: 705 of 1745 chars), so Scope B roughly doubles reachable savings.

If the operator has not opted into Scope B, **every `Field(description=...)` is a no-op for your edits** — exclude it from the reducible baseline and report it as such.

## Step 0 — dump schema + record baseline (always first)

Dump what reaches the client and confirm your edits change it. `mcp.list_tools()` is async and returns `list[FunctionTool]` with `.name`, `.description`, `.parameters`.

```bash
REPO=$(git rev-parse --show-toplevel)   # capture repo root before leaving it
cd /tmp && uv run --project "$REPO" python - <<'PY'
import asyncio
from mcp_server_polarion.server import mcp
async def go():
    tools = sorted(await mcp.list_tools(), key=lambda t: t.name)
    rows = []
    for t in tools:
        props = (t.parameters or {}).get("properties", {})
        desc_n = len(t.description or "")
        param_n = sum(len(s.get("description", "")) for s in props.values())
        rows.append((t.name, desc_n, param_n, desc_n + param_n))
    rows.sort(key=lambda r: -r[3])
    total = sum(r[3] for r in rows)
    print(f'{"tool":34}{"desc":>6}{"param":>7}{"total":>7}')
    for name, d, p, tot in rows:
        print(f"{name:34}{d:6}{p:7}{tot:7}")
    print(f'{"TOTAL LLM-facing chars":34}{total:20}')
asyncio.run(go())
PY
```

Run from `/tmp` so the repo `.env` is not auto-loaded (it poisons `PolarionConfig` if it ever holds an `OPENAI_API_KEY`). Record per tool: `desc` chars (Scope A target), `param` chars (Scope B target), `total`. Sort descending — long tools = biggest savings first. (Orientation: ~14k total across 24 tools at time of writing.) If a tokenizer (`tiktoken`) is available, also note token counts; else chars are the primary metric.

For each param, confirm the schema description text matches what you intend to edit. A param whose text lives in `Field(description=...)` will not move under a Scope-A edit — flag it `no-op (Scope B)`.

## Compression principles (LLM-facing text only)

**Prose (→ `description`, most important)**
- "When to call / trigger" over "what it does." `"Updates a work item"` → `"Call to change fields on an existing work item; fetch it first."`
- One **boundary** line vs the nearest sibling tool ("this tool for X; for Y use `other_tool`").
- One **negative** line only if misuse is likely ("not for moving between documents — use `move_*`").
- Keep **pointers to runtime tools** the LLM must chain to (`list_work_item_enum_options`, `get_sql_query_recipes`, `move_work_item_to_document`).

**Field descriptions (→ schema params; Scope B only)**
- Keep: format/constraint (ID shape, date format, enum allowed values), and the **source** of a value that comes from another tool's output.
- Drop: type/required already stated by the signature; descriptions self-evident from the name.

**Form**
- One line each. No dev-narrative ("we tried X"). No `WARNING:`/`NOTE:` prefixes. No banner comments. Consistent tense/terms/format.

## The optimization loop

**Global prep**
1. Work on a branch off `main` (e.g. `chore/shrink-mcp-tool-docs`) — never run the loop on `main`. Per-cut rollback uses a working-copy snapshot (`git stash` / file copy), not a commit; reserve commits for adopted GREEN baselines. The whole session lands as one squashed commit/PR.
2. **Verify the eval gate actually runs**, then confirm all 3 gates are GREEN → that is your **GREEN baseline**; checkpoint it. The gate is the only signal that a cut is safe — if no eval model is reachable (`OPENAI_API_KEY` unset *and* ollama down), **STOP and report**. Do not cut without the gate; that ships unvalidated descriptions to clients.
3. Step-0 dump + baseline chars.
4. Sort tools by LLM-facing chars **descending**.

**Cut risk classes** (L1 safest → L4 riskiest; draw candidate cuts in this order, cheapest-risk first — these are a *risk ordering of edits*, not atomic rungs to jump to):
- **L1** — bits restated by the signature / self-evident clauses / trivially redundant prose (Scope B: redundant param trims if opted in).
- **L2** — rewrite prose what→when(trigger); compress boundary + negative to one line each.
- **L3** — prose toward trigger line + boundary line; params (Scope B) toward format/constraint/source only.
- **L4 (floor-adjacent)** — trigger line + only load-bearing enum/format/source. No further.

**Per tool (incremental descent + bisection)** — each cut is a small delta from the *current GREEN*, never a jump to an absolute terse form; a RED is recovered by **bisecting the cut set**, never by reverting to the original (revert-to-original is the net-zero trap):
1. Snapshot current GREEN (working-copy snapshot, cheap to revert).
2. **First cut is always the gentlest** — L1 redundancy only. Gate → commit as the tool's first GREEN baseline **before** any rewrite. Guarantees ≥1 committed GREEN for every tool with slack, so a later overshoot can never zero the tool out.
3. Build the next **candidate-cut set**: independent edits from the lowest unfinished risk class. Target ~15–20% of remaining reducible chars per step — a delta, not a leap to the floor.
4. Apply the whole set, **Fast gate** → GREEN: go to 4-a. RED: go to 4-b.
4-a. Fast GREEN → run **Confirm gate**:
   - Confirm GREEN → commit as **new GREEN baseline**, refresh baseline chars, **re-sort**, build the next set (step 3).
   - Confirm RED (variance or overfit) → revert to last GREEN, **lock** this tool, move to next tool.
4-b. Fast RED — **bisect the candidate set, do NOT full-revert**:
   - Split the set: safer half (L1 / redundant-param trims) vs riskier half (trigger/boundary/enum rewrites). Read the failing transcript to decide which cuts are riskier — **which tool was mis-selected or mis-parametrized, and why** (extended thinking on if available).
   - Drop the riskier half, re-gate the safer half. GREEN → commit that subset, then re-attempt the dropped cuts **one at a time** (step 3). RED → recurse: bisect the still-applied set again.
   - A single cut alone RED → that phrase is load-bearing → restore **only it**, keep the rest, lock it out of future sets.
   - `min_pass_rate=1.0`: **reproduce a RED once** (re-run Fast) before calling a cut load-bearing — a lone RED can be noise.
5. Repeat from 3 until the reducible set is exhausted or every remaining single cut is RED → **lock** at last GREEN.

**Invariant:** a RED discards only the last delta, never accumulated GREEN. Net-zero for a tool happens only if its very first L1 trim is RED — meaning it was already at floor.

**Confirm-gate cost:** smaller deltas mean more iterations, so don't run the (expensive) Confirm gate per delta far from the floor. For early L1 deltas, batch several Fast-GREEN deltas and Confirm once before switching risk class; near the boundary, Confirm per delta. A batched Confirm RED bisects the batch.

**Global stop:** all tools locked; or a full pass's cumulative saving < ~50 chars; or an operator-set iteration cap is hit. **On stop, re-run all 3 gates at the last GREEN baseline and confirm green.** Submission = last GREEN, never RED.

## Gates

**Preconditions** (once): `uv sync --group evals` — `strands_evals` is not in the default env. The eval agent needs a model: `OPENAI_API_KEY` (cloud, CI default) or `EVAL_MODEL=ollama_chat/<model>` (local). Cloud is faster and more reliable at `min_pass_rate=1.0`; local ollama is slow, so lean on the Fast gate (`--runs 1`) for triage and raise runs only near the boundary. Run eval/pytest from a CWD outside the repo when the repo `.env` carries an `OPENAI_API_KEY` (it shadows `PolarionConfig`); `cd /tmp && uv run --project <repo> ...` avoids it.

**Fast gate** (cheap triage after each cut):
```bash
uv run python -m evals.run --runs 1
uv run pytest tests/mcp_server_polarion/test_mcp_transport.py -q
```
- The gate runs **both tiers**: Tier-1 prohibitions (`min_pass_rate=1.0`) **and** Tier-2 efficiency (`0.8`). Docstring cuts regress **both** — a too-terse trigger can make the agent take a wasteful path (T2) even when it never does the forbidden action (T1). Read the gate summary, not just the exit code.
- During diagnosis target one case: `uv run python -m evals.run --case <NAME> --runs 1` (case names in `evals/cases/tier1_prohibitions.py` and `tier2_efficiency.py`).

**Confirm gate** (before committing/locking a baseline — kills variance + overfit):
```bash
uv run python -m evals.run --runs 5     # raise toward boundary; full default = EVAL_RUNS (10)
uv run pytest
uv run ruff check . && uv run ruff format . && uv run mypy src/
```
- **Variance:** as cuts shrink, the effect size shrinks and noise vs real regression blurs → raise `--runs` near the boundary. GREEN/RED that flaps across runs = unstable cut → do **not** adopt it; keep the last stable GREEN.
- **Overfit:** reserve a held-out subset of case names (pick a few via `--case`), **never run them during the loop**, run them only at Confirm. Held-out RED = overfit → revert that cut.

## Floor (never cut below — load-bearing)

Trigger/when line · one sibling-boundary line when a near-duplicate tool exists · enum allowed-values and ID/date formats · the source of values that come from other tools' output · pointers to runtime-callable tools (`get_sql_query_recipes`, `list_*_enum_options`, `move_*`). The transport test asserts every `description` is **non-empty** — an empty description is a guaranteed RED.

## Absolute rules

1. **Final submission = the last GREEN baseline, with all 3 green:**
   - `uv run python -m evals.run` (Tier-1 all pass at 1.0; Tier-2 at 0.8)
   - `uv run pytest` (esp. `tests/mcp_server_polarion/test_mcp_transport.py` and `tests/evals/`)
   - `uv run ruff check . && uv run ruff format . && uv run mypy src/`
2. **Scope A: docstring only.** Scope B (opt-in): only the `description=` string of `Field(...)`; never type/default/constraint/logic. Signatures, return types, behavior unchanged in both.
3. Docstrings stay **self-contained** — but keep the runtime-tool pointers (`get_sql_query_recipes`, etc.).
4. **Never commit/submit RED.** RED is a boundary signal; the adopted state is always the prior GREEN.
5. Tool count is unchanged → leave `EXPECTED_TOOL_NAMES` alone. Don't touch the `get_sql_query_recipes` guide body (the transport test asserts its content).

## Output format

1. **Step-0 result:** which surface each string ships on, the Scope-B param list (the no-op-for-Scope-A set), and the baseline char table.
2. **Per tool** (LLM-facing chars descending): before/after diff + one-line rationale ("what was cut and why") + adopted risk class (Lx) and final cut set (post-bisection) + char (and token, if available) savings.
3. **Loop log:** one row per iteration `[tool | cut (Lx set / bisect-subset / single restore) | Fast | Confirm | decision (commit/bisect/lock)]`; each RED cut + its diagnosis (which tool mis-selected and why) + the bisection outcome on one line.
4. **Final gate captures:** all 3 green at the last GREEN baseline.
5. **Total LLM-facing savings:** summed chars (+ token estimate) + per-tool before→after table.
