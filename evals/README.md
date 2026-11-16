# Pre-deploy evaluation gate

Drives an LLM agent through the **real** in-memory MCP server against a
**mocked** Polarion backend, then deterministically asserts the agent's
behaviour. Hard gate ahead of the PyPI publish jobs in
[`publish.yml`](../.github/workflows/publish.yml). No LLM judge — every verdict
is a pure function of the tool-call trajectory, so the only model cost is the
agent under test.

## Categories

One file per behaviour; all share the harness and the one evaluator.
`min_pass_rate` is per-case, not per-category.

| Category | Asserts | `min_pass_rate` |
| --- | --- | --- |
| [triggers](cases/triggers.py) | right tool / path fires (and the tempting wrong one doesn't) | 1.0 |
| [safety](cases/safety.py) | destructive / data-loss footgun never happens | 1.0 |
| [efficiency](cases/efficiency.py) | correct answer reached without waste | 0.8 |
| [orchestration](cases/orchestration.py) | multi-step tasks walk the correct ordered sequence, threading ids between steps | 0.8 |

Every case carries an `intent` (one line: what passes vs. fails) and a `covers`
list (tools it exercises). `uv run python -m evals.run --list` prints the
catalog; [`tests/evals/test_coverage.py`](../tests/evals/test_coverage.py) fails
if a registered tool has no `covers` entry and is not explicitly deferred.

## How it works

```
Strands Agent (LiteLLM)
  → bridged MCP tools (real docstrings + JSON Schema, calls recorded)
  → fastmcp.Client(mcp)  (in-memory transport, real server lifespan)
  → PolarionClient
  → respx → FakePolarion  (structure of MCP_Test_Project, synthetic content)
```

Each run lives in one process (the gate itself, or one spawned worker under
`--workers`), so respx intercepts that process's Polarion HTTP. LLM traffic
never touches respx — LiteLLM's default aiohttp transport replaces httpcore
under `httpx.AsyncClient`, and respx only patches httpcore — and hits the real
provider, while no request reaches a real Polarion
(`assert_all_mocked=False`). `FakePolarion` is stateful: each case gets a
fresh world copied from the seeds, writes apply with live Polarion semantics,
and later reads see them — an agent that verifies its own write finds it. The
runner clears the server's process-wide TTL caches per case so no case sees a
previous case's world. A write to a route the fake does not model answers
`501` instead of faking success.
`TrajectoryRecorder` captures the tool calls; `CheckDispatchEvaluator` dispatches
on `Case.metadata["check"]` to a pure check in
[`evaluators/checks.py`](evaluators/checks.py), plus cross-cutting global checks
(e.g. every `update_document` block must carry a non-empty `id`).

## Running

```bash
uv sync --group evals

uv run python -m evals.run                                  # full gate (each case EVAL_RUNS times, default 10)
uv run python -m evals.run --list                           # case catalog, no model cost
uv run python -m evals.run --case SAFE-READONLY --runs 1    # one case, once (smoke)
uv run python -m evals.run --workers 4                      # 4 worker processes
```

`--workers N` (or `EVAL_WORKERS`, default `8`) spreads the (case, run) pairs
over `N` spawned processes; verdicts match a serial run, only progress lines
interleave. `--workers 1` runs serially in-process. Processes, not threads:
respx routing and the harness env/module patches are process-global.

The gate fails (exit 1) if any case falls below its `min_pass_rate`. A JSON
report is written to `evals/reports/gate-<sha>-<model>-<effort>.json` (gitignored).
Besides per-case verdicts it records `workers`, `wall_s`, `peak_rss_mb` (largest
single process), `cpu_s` (all processes; both `null` on Windows), and per case
`durations_s` and `infra_retries` — enough to size `workers` for a runner.

## Model

The gate runs `openai/gpt-6-luna` at reasoning effort `medium`; both are
switchable through the LiteLLM adapter, and both need `OPENAI_API_KEY`.

```bash
uv run python -m evals.run                                     # defaults above
EVAL_MODEL=openai/gpt-6.1-sol uv run python -m evals.run       # other model
EVAL_REASONING_EFFORT=high uv run python -m evals.run          # other effort
```

`reasoning_effort` is always sent explicitly. GPT-6 Luna serves function tools
on `/v1/chat/completions` only at effort `none`, so a reasoning run has to go
through the Responses API. LiteLLM (>= 1.101.0) bridges the call there whenever
an effort is given; with the parameter unset it bridges only on hosts it
recognises as OpenAI, and the effort would follow the server default.
`temperature` is not sent at all (GPT-6 rejects it unless effort is `none`), so
gate stability rests on the fixed effort plus `parallel_tool_calls` off — a
model can otherwise emit the same call twice in one parallel block.
`drop_params` is deliberately not set: a silently dropped `reasoning_effort`
would run the whole gate unreasoned and still report a pass.

## Limits

An agent can loop; cloud providers return 429 when TPM/RPM is exhausted.
Each case is bounded fail-closed (via `<agent-error: ...>`). A provider-side
failure that outlives LiteLLM's retries — 408, 429, 5xx, connection error,
provider timeout, anywhere in the exception chain — is reported as
`<infra-error: ...>` instead: that run is re-run rather than counted as a FAIL
(only if the error came before any tool call; after one it stays a plain FAIL),
and fails closed once `EVAL_INFRA_RETRIES` re-runs are spent. A case timeout
before any tool call counts as infra too — the first model call never returned,
and at the defaults one stalled model call can outlast `EVAL_CASE_TIMEOUT`.
Two chains stay plain FAILs even when they carry a 429/5xx/connection error: a
Python programming error anywhere in the chain (LiteLLM wraps its own unmapped
exceptions as a 500 or connection error) and an `insufficient_quota` 429
(exhausted billing). The cycle cap and every other 4xx stay plain FAILs too.

| Env var | Default | Cap |
| --- | --- | --- |
| `EVAL_MAX_CYCLES` | `10` | Model calls per case. |
| `EVAL_CASE_TIMEOUT` | `600` | Wall-clock seconds per case. |
| `EVAL_NUM_RETRIES` | `10` | LiteLLM retries per model call (see below). |
| `EVAL_LLM_TIMEOUT` | `180` | Wall-clock seconds per model-call attempt. |
| `EVAL_REASONING_EFFORT` | `medium` | Reasoning depth; higher raises latency and output-token cost. |
| `EVAL_WORKERS` | `8` | Worker processes (`--workers`); CI action uses the same default. |
| `EVAL_INFRA_RETRIES` | `2` | Re-runs of a run that ended in `<infra-error: ...>`; not counted as runs. |
| `EVAL_INFRA_RETRY_DELAY` | `30` | Seconds slept before each infra re-run. |

On the default (Responses-bridged) model LiteLLM retries by itself: a 429 backs
off 1, 2, 4, 8, then 10 s per attempt (about 65 s in total at 10 retries,
`Retry-After` ignored), and every other error is retried with no wait. Only
models served on chat completions go through the OpenAI SDK's own retry
(`min(0.5·2ⁿ, 8)s` ±25 % jitter, or `Retry-After`) first.

The timeout defaults assume reasoning at `medium` — a raised effort needs them
raised too. `EVAL_LLM_TIMEOUT` is per attempt, so worst-case per model call is
`(1 + EVAL_NUM_RETRIES) × EVAL_LLM_TIMEOUT`. Raise `EVAL_CASE_TIMEOUT` in lockstep
when bumping either, or the case fail-closes first.

## Release pipeline

- **Hard gate** — the `gate` job in [`publish.yml`](../.github/workflows/publish.yml)
  calls [`publish-gate.yml`](../.github/workflows/publish-gate.yml) on tag push;
  every later publish job depends on it.
- **On-demand** — [`evals-on-demand.yml`](../.github/workflows/evals-on-demand.yml)
  is `workflow_dispatch`: pick a `model`, `reasoning_effort`, `runs` and
  `workers` from the Actions tab before tagging.

Both read the **`OPENAI_API_KEY` repository secret**; if it is missing the job
fails and the release is blocked (fail-closed).

## Adding a case

1. Add a pure check to [`evaluators/checks.py`](evaluators/checks.py) and register
   it in `REGISTRY` (or reuse one). Keep it a function of the trajectory only.
2. Add a `Case` to the behaviour file via its `_case` helper — a thin wrapper
   binding the category threshold (triggers/safety `1.0`,
   efficiency/orchestration `0.8`) over the shared factory in
   [`cases/_shared.py`](cases/_shared.py). Give it an `intent` and a `covers`
   list. An orchestration case instead takes typed `steps` (the `Step` dict),
   derives `covers` from them, and is fixed to the `ordered_trajectory` check.
3. Phrase the task neutrally — never state the rule, or you test the prompt
   instead of the tool docstrings (the only guard).
4. If the case covers a previously-uncovered tool, drop it from the `DEFERRED`
   map in [`tests/evals/test_coverage.py`](../tests/evals/test_coverage.py).

## Fixtures

Seeds live in [`harness/fixtures.py`](harness/fixtures.py) (`SEEDS`);
[`harness/fake_polarion/`](harness/fake_polarion/) serves them. Mirror the real
server's *structure*; keep content synthetic.

| Module | Owns |
| --- | --- |
| `world.py` | state + write rules (no HTTP) — a new rule is one `World` method |
| `body.py` | document body ↔ ordered blocks |
| `wire.py` | JSON:API response shapes and live response quirks |
| `routes/` | thin per-domain handlers on `router.py` — a new endpoint is one handler |

Live behaviour the world reproduces (probed on the testdrive project):

- A document body is the whole document. Work items sit in it as empty macro
  placeholders (`heading_html` / `work_item_html`); every other top-level block
  carries a unique `id`. `/parts` is the body split block by block, in order.
- A plain `<hN>` in a body write spawns a heading work item. A member dropped
  from the body stays in the document as an orphan (move-in answers `409`).
- `moveToDocument` without an anchor appends; an item under a heading gets an
  automatic link to it, role = the document's `structure_link_role`
  (`relates_to` for `FakeDoc`).
- Server-assigned ids continue across calls (`MCPT-9001`, …); a repeated test
  case gets the next record iteration; bulk POSTs echo one id per entry.

Seeds:

- Project `MCP_Test_Project`; doc `FakeDoc` = intro paragraph `p-1`, then the
  `Section A` heading `MCPT-100` (`heading_MCPT-100`, the positional-move
  anchor) over requirements `MCPT-300` and `MCPT-301`.
- `MCPT-200` free-floating task (`custom_fields={"acceptance_criteria_id": "AC-1"}`,
  one `ref_ext` hyperlink), `MCPT-201` heading, `MCPT-202` ghost-typed task;
  comment thread `1`→`2`; enum `hyperlink-role`.
- Orchestration adds doc `FakeParentDoc` (heading-less, so `list_documents`
  surfaces only `FakeDoc`); `MCPT-300` (links `satisfies`→`MCPT-400` and
  `verifies`→`MCPT-500`), `MCPT-301` (no test-case link — coverage-gap signal),
  `MCPT-400` (parent, in `FakeParentDoc`), test case `MCPT-500`; enum
  `workitem-link-role`. Forward links from `SEEDS.links` plus structure links;
  back direction via `query=linkedWorkItems:{wi}`.
