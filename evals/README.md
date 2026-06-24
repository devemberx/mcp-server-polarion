# Pre-deploy evaluation gate

Drives an LLM agent through the **real** in-memory MCP server against a
**mocked** Polarion backend, then deterministically asserts the agent's
behaviour. Runs as a hard gate ahead of the PyPI publish jobs in
[`.github/workflows/publish.yml`](../.github/workflows/publish.yml).

Cases are organised by the **behaviour** they test (one file per category),
sharing the same harness and the one evaluator; `min_pass_rate` is a per-case
property, not a per-category one:

- **triggers** ([`cases/triggers.py`](cases/triggers.py)): the request must route
  to the **correct tool / path** — did the right tool fire (and the tempting
  wrong one not)? `min_pass_rate = 1.0` — a single mis-trigger blocks release, so
  each prompt admits exactly one correct tool family.
- **safety** ([`cases/safety.py`](cases/safety.py)): a destructive / corrupting /
  data-loss **footgun must never happen** (read-before-write, read-only intent,
  REPLACE-list preservation, round-trip sourcing, right comment target).
  `min_pass_rate = 1.0`.
- **efficiency** ([`cases/efficiency.py`](cases/efficiency.py)): the correct answer
  must be reached **without waste** (one bulk call, direct get by known id, no
  redundant identical reads, right query mechanism). `min_pass_rate = 0.8` —
  occasional waste tolerated, systematic waste blocks.
- **orchestration** ([`cases/orchestration.py`](cases/orchestration.py)):
  multi-step tasks must walk the correct ordered tool sequence and thread ids
  between steps (e.g. `create_work_items → read_document_parts →
  move_work_item_to_document` with the move's part-id observed from the read).
  One generic `ordered_trajectory` check; cases are data. `min_pass_rate = 0.8`.

Every case carries an `intent` (one line: what passes vs. fails) and a `covers`
list (the tools it exercises) in its metadata; `uv run python -m evals.run
--list` prints the catalog.

No category needs an **LLM judge**: every verdict is a pure function of the
tool-call trajectory. The only model cost is the agent under test.

## How it works

```
Strands Agent (LiteLLM)
  → bridged MCP tools (real docstrings + JSON Schema, calls recorded)
  → fastmcp.Client(mcp)  (in-memory transport, real server lifespan)
  → PolarionClient
  → respx → FakePolarion  (structure of MCP_Test_Project, synthetic content)
```

Everything runs in one process so respx intercepts Polarion HTTP. The router is
created with `assert_all_mocked=False`, so the agent's LLM traffic falls through
to the real provider while **no request ever reaches a real Polarion**. Every
mutating request is recorded but has no effect.

The agent's tool calls are captured by `TrajectoryRecorder`; the
`CheckDispatchEvaluator` dispatches on `Case.metadata["check"]` to a pure
check in [`evaluators/checks.py`](evaluators/checks.py) plus cross-cutting global
checks (e.g. every `update_document` body block must carry a non-empty `id`).
All categories share this one evaluator — there is deliberately no per-category
evaluator (the check, named in case metadata, is what varies).

## Running

```bash
uv sync --group evals

# Full gate (all cases, EVAL_RUNS times each; default 10)
uv run python -m evals.run
uv run python evals/run.py        # equivalent

# Print the case catalog (name/category/check/covers/intent); no model cost
uv run python -m evals.run --list
uv run python -m evals.run --category triggers --list

# One case, once (fast smoke)
uv run python -m evals.run --case SAFE-READONLY --runs 1
```

Each case runs N times and passes only at its `min_pass_rate`
(triggers/safety: 1.0, efficiency/orchestration: 0.8). The gate fails (exit 1)
if any case falls short. A JSON report is written to
`evals/reports/gate-<sha>-<model>.json` (gitignored).

## Choosing the model

A single LiteLLM adapter serves cloud and local — switch with `EVAL_MODEL`:

```bash
# Cloud (CI default) — needs OPENAI_API_KEY
EVAL_MODEL=openai/gpt-4o-mini uv run python -m evals.run

# Local via Ollama — free, no key
EVAL_MODEL=ollama_chat/qwen3.5:9b-mlx uv run python -m evals.run
# (set EVAL_MODEL_BASE_URL if Ollama is not on localhost:11434)
```

`temperature` is pinned to 0 and `parallel_tool_calls` off (gpt-4o-mini can
emit the same tool call twice in one parallel block) to keep the
zero-tolerance gate stable.

## Runaway protection

Local models can loop indefinitely without producing a final answer, and
cloud providers can return 429 when TPM/RPM is exhausted. Each case is
bounded by case-level limits (fail-closed via `<agent-error: ...>`), and a
single model call retries transient 429 / network errors with exponential
backoff (handled by the OpenAI SDK underneath LiteLLM) before giving up.

| Env var             | Default | Cap                                                                              |
| ------------------- | ------- | -------------------------------------------------------------------------------- |
| `EVAL_MAX_CYCLES`   | `10`    | Model calls per case (`BeforeModelCallEvent` hook count).                        |
| `EVAL_CASE_TIMEOUT` | `120`   | Wall-clock seconds (`asyncio.wait_for`).                                         |
| `EVAL_NUM_RETRIES`  | `10`    | LiteLLM `num_retries`; OpenAI SDK sleeps `min(0.5·2ⁿ, 8)s` with ±25 % jitter, or honours `Retry-After` if present. |
| `EVAL_LLM_TIMEOUT`  | `60`    | Wall-clock seconds for one model call (LiteLLM `timeout`).                       |

`EVAL_LLM_TIMEOUT` is per attempt — worst-case wall-clock for one model
call is `EVAL_NUM_RETRIES × EVAL_LLM_TIMEOUT` when every attempt times out
without a fast 429 response. Raise `EVAL_CASE_TIMEOUT` in lockstep when
bumping either, or the case fail-closes via `asyncio.wait_for` before the
retry budget is exhausted.

For slow CPU inference raise `EVAL_CASE_TIMEOUT`:

```bash
EVAL_MAX_CYCLES=10 EVAL_CASE_TIMEOUT=600 \
  EVAL_MODEL=ollama_chat/gemma4:e4b uv run python -m evals.run
```

## Release pipeline

- **Hard gate** — the `gate` job in
  [`publish.yml`](../.github/workflows/publish.yml) calls the reusable
  [`publish-gate.yml`](../.github/workflows/publish-gate.yml) (triggers ->
  safety -> efficiency -> orchestration) on tag push, and every later publish
  job depends on it, so a failing gate blocks the release.
- **On-demand run** — the
  [`Evals (on-demand)`](../.github/workflows/evals-on-demand.yml) workflow is
  `workflow_dispatch`: trigger it from the Actions tab with a chosen `model`
  and `runs` to review results before deciding to tag.

Both read the **`OPENAI_API_KEY` repository secret**. If it is missing, the
job fails and the release is blocked (fail-closed) — add the secret before the
first tagged release.

## Adding a case

1. Add a pure check to `evaluators/checks.py` and register it in `REGISTRY`
   (or reuse an existing one). Keep it a function of the trajectory only.
2. Add a `Case` to the file for the behaviour it tests — `cases/triggers.py`
   (right tool fires, `1.0`), `cases/safety.py` (footgun avoided, `1.0`),
   `cases/efficiency.py` (no waste, `0.8`), or `cases/orchestration.py`
   (multi-step, `0.8`); `run.py` loads all four lists. Give every case an
   `intent` (one line) and a `covers` list (tools it exercises) — an
   orchestration case derives `covers` from its steps and usually reuses the
   `ordered_trajectory` check, just declaring its step sequence in `params`.
3. Phrase the task neutrally — never state the rule, or you test the prompt
   instead of the tool docstrings (the only guard).

Seed entities (project `MCP_Test_Project`, doc `FakeDoc` with an anchored
intro paragraph, free-floating `MCPT-200` task carrying
`custom_fields={"acceptance_criteria_id": "AC-1"}` and one `ref_ext`
hyperlink, `MCPT-201` heading, `MCPT-202` ghost-typed task, comment thread
`1`→`2`, project enum `hyperlink-role`) live in
[`harness/fixtures.py`](harness/fixtures.py) as `SEEDS`;
[`harness/fake_polarion.py`](harness/fake_polarion.py) serves them. Mirror the
real server's *structure* there; keep all content synthetic. Bulk POSTs echo one
id per submitted entry, so bulk cases exercise the tools' count-match rule.

Orchestration adds: a second document `FakeParentDoc`; requirements `MCPT-300` (in
`FakeDoc`, links `satisfies`→`MCPT-400` and `verifies`→`MCPT-500`), `MCPT-301`
(in `FakeDoc`, no test-case link — coverage-gap signal), `MCPT-400` (parent, in
`FakeParentDoc`), test case `MCPT-500`; a `FakeDoc` `/parts` response with the
`Section A` heading part (`heading_MCPT-100`, the positional-move anchor); and
project enum `workitem-link-role`. Forward links come from `SEEDS.links`; the back
direction is served via `list_work_items` `query=linkedWorkItems:{wi}`.
