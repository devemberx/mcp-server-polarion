# Pre-deploy evaluation gate (Tier 1)

Drives an LLM agent through the **real** in-memory MCP server against a
**mocked** Polarion backend, then deterministically asserts the agent never
took a destructive or footgun action. Runs as a hard gate ahead of the PyPI
publish jobs in [`.github/workflows/publish.yml`](../.github/workflows/publish.yml) —
a single forbidden action blocks the release.

Tier 1 needs **no LLM judge**: every verdict is a pure function of the tool-call
trajectory. The only model cost is the agent under test.

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
`ForbiddenBehaviorEvaluator` dispatches on `Case.metadata["check"]` to a pure
check in [`evaluators/checks.py`](evaluators/checks.py) plus cross-cutting global
checks (e.g. every `update_document` body block must carry a non-empty `id`).

## Running

```bash
uv sync --group evals

# Full gate (all cases, EVAL_RUNS times each; default 10)
uv run python -m evals.run
uv run python evals/run.py        # equivalent

# One case, once (fast smoke)
uv run python -m evals.run --case T1-READONLY --runs 1
```

Each case runs N times; a Tier-1 case passes only at `min_pass_rate` (1.0 —
zero tolerance). The gate fails (exit 1) if any case falls short. A JSON report
is written to `evals/reports/tier1-<sha>.json` (gitignored).

## Choosing the model

A single LiteLLM adapter serves cloud and local — switch with `EVAL_MODEL`:

```bash
# Cloud (CI default) — needs OPENAI_API_KEY
EVAL_MODEL=openai/gpt-4o-mini uv run python -m evals.run

# Local via Ollama — free, no key
EVAL_MODEL=ollama_chat/qwen3.5:9b-mlx uv run python -m evals.run
# (set EVAL_MODEL_BASE_URL if Ollama is not on localhost:11434)
```

`temperature` is pinned to 0 to keep the zero-tolerance gate stable.

## CI

- **Hard gate** — the `evals` job in
  [`publish.yml`](../.github/workflows/publish.yml) runs on tag push, and
  `publish-test` / `publish` depend on it, so a failing gate blocks the release.
- **Manual pre-deploy run** — the
  [`Tier-1 Evals`](../.github/workflows/evals.yml) workflow is
  `workflow_dispatch`: trigger it from the Actions tab with a chosen `model`
  and `runs` to review results before deciding to tag.

Both read the **`OPENAI_API_KEY` repository secret**. If it is missing, the
job fails and the release is blocked (fail-closed) — add the secret before the
first tagged release.

## Adding a case

1. Add a pure check to `evaluators/checks.py` and register it in `REGISTRY`
   (or reuse an existing one). Keep it a function of the trajectory only.
2. Add a `Case` to `cases/tier1_prohibitions.py` with
   `metadata={"check": "<name>", "params": {...}, "min_pass_rate": 1.0}`.
3. Phrase the task neutrally — never state the rule, or you test the prompt
   instead of the tool docstrings (the only guard).

Seed entities (project `MCP_Test_Project`, doc `FakeDoc`, free-floating
`MCPT-200/201/202`, comment thread `1`→`2`) live in
[`harness/fake_polarion.py`](harness/fake_polarion.py). Mirror the real
server's *structure* there; keep all content synthetic.
