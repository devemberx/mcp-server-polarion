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

One process, so respx intercepts Polarion HTTP. The router runs with
`assert_all_mocked=False` — agent LLM traffic hits the real provider while no
request reaches a real Polarion; mutations are recorded but never applied.
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
```

The gate fails (exit 1) if any case falls below its `min_pass_rate`. A JSON
report is written to `evals/reports/gate-<sha>-<model>.json` (gitignored).

## Model

The gate runs `openai/gpt-5.6-luna` at reasoning effort `medium`; both are
switchable through the LiteLLM adapter, and both need `OPENAI_API_KEY`.

```bash
uv run python -m evals.run                                     # defaults above
EVAL_MODEL=openai/gpt-5.6-terra uv run python -m evals.run     # other model
EVAL_REASONING_EFFORT=high uv run python -m evals.run          # other effort
```

Use the `-luna` id, not the bare `openai/gpt-5.6` alias — that alias routes to
the Sol tier.

`reasoning_effort` is always sent explicitly, for two reasons: GPT-5.6 rejects
function tools on `/v1/chat/completions`, and LiteLLM only bridges the call to
the Responses API when the parameter is present. `temperature` is not sent at
all (the gpt-5 family accepts `1` only), so gate stability rests on the fixed
effort plus `parallel_tool_calls` off — a model can otherwise emit the same call
twice in one parallel block. `drop_params` is deliberately not set: a silently
dropped `reasoning_effort` would run the whole gate unreasoned and still report
a pass.

## Limits

An agent can loop; cloud providers return 429 when TPM/RPM is exhausted.
Each case is bounded fail-closed (via `<agent-error: ...>`).

| Env var | Default | Cap |
| --- | --- | --- |
| `EVAL_MAX_CYCLES` | `10` | Model calls per case. |
| `EVAL_CASE_TIMEOUT` | `600` | Wall-clock seconds per case. |
| `EVAL_NUM_RETRIES` | `10` | LiteLLM retries; OpenAI SDK sleeps `min(0.5·2ⁿ, 8)s` ±25 % jitter, or honours `Retry-After`. |
| `EVAL_LLM_TIMEOUT` | `180` | Wall-clock seconds per model call. |
| `EVAL_REASONING_EFFORT` | `medium` | Reasoning depth; higher raises latency and output-token cost. |

The timeout defaults assume reasoning at `medium` — a raised effort needs them
raised too. `EVAL_LLM_TIMEOUT` is per attempt, so worst-case per model call is
`EVAL_NUM_RETRIES × EVAL_LLM_TIMEOUT`. Raise `EVAL_CASE_TIMEOUT` in lockstep when
bumping either, or the case fail-closes first.

## Release pipeline

- **Hard gate** — the `gate` job in [`publish.yml`](../.github/workflows/publish.yml)
  calls [`publish-gate.yml`](../.github/workflows/publish-gate.yml) on tag push;
  every later publish job depends on it.
- **On-demand** — [`evals-on-demand.yml`](../.github/workflows/evals-on-demand.yml)
  is `workflow_dispatch`: pick a `model`, `reasoning_effort` and `runs` from the
  Actions tab before tagging.

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
[`harness/fake_polarion.py`](harness/fake_polarion.py) serves them. Mirror the
real server's *structure*; keep content synthetic. Bulk POSTs echo one id per
submitted entry, so bulk cases exercise the count-match rule.

- Project `MCP_Test_Project`, doc `FakeDoc` (anchored intro paragraph).
- `MCPT-200` free-floating task (`custom_fields={"acceptance_criteria_id": "AC-1"}`,
  one `ref_ext` hyperlink), `MCPT-201` heading, `MCPT-202` ghost-typed task;
  comment thread `1`→`2`; enum `hyperlink-role`.
- Orchestration adds doc `FakeParentDoc`; `MCPT-300` (in `FakeDoc`, links
  `satisfies`→`MCPT-400` and `verifies`→`MCPT-500`), `MCPT-301` (no test-case link
  — coverage-gap signal), `MCPT-400` (parent, in `FakeParentDoc`), test case
  `MCPT-500`; a `FakeDoc` `/parts` `Section A` heading (`heading_MCPT-100`, the
  positional-move anchor); enum `workitem-link-role`. Forward links from
  `SEEDS.links`; back direction via `query=linkedWorkItems:{wi}`.
