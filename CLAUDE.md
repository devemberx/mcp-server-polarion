# CLAUDE.md

MCP server: AI read/write Polarion ALM. FastMCP 3.0, strict async, fully typed.

## Commands

```bash
uv sync --dev                                            # install deps
uv run pytest                                            # all tests
uv run pytest --cov --cov-report=term-missing            # tests + uncovered lines
uv run pytest --cov --cov-report=html                    # htmlcov/index.html (visual)
uv run ruff check . && uv run ruff format . && uv run mypy src/  # lint + format + types
uv run pytest --cov=src/mcp_server_polarion --cov=evals --cov-report=xml \
  && uv run diff-cover coverage.xml --compare-branch=origin/main --fail-under=90  # changed-line gate
uv run mcp-server-polarion                               # run server (stdio)
```

CI: `ruff check` → `ruff format --check` → `mypy` → `pytest` (`--cov-fail-under=90`) → `diff-cover` (every changed line ≥90%). Each PR line needs a test — incl. parser defensive branches and `evals/harness` request handlers, not only `src/`. Run the `diff-cover` command above before pushing.

## Architecture

- `core/` — `client.py` (async httpx, retries 429/5xx → `PolarionError`/`PolarionAuthError`/`PolarionNotFoundError`), `config.py` (`POLARION_URL`/`POLARION_TOKEN`), `logging.py` (stderr-only; loggers `mcp_server_polarion.<module>`).
- `tools/` — domain modules; `_build_*_payload` = unit-test seam; `tools/__init__.py` import registers `@mcp.tool`s. `_shared/`: `helpers.py`, `parse.py` (JSON:API→models), `pagination.py` (`make_page`), `fields.py`/`custom_fields.py` (sparse-fieldset + custom-field policy), `cache.py` (`TTLCache`), `guard.py` (write guards), `sql.py` (recipes). `tools/guides/` = on-demand data.
- `utils/html.py` — Markdown↔HTML, `stamp_block_ids`, `first_anchorless_block`.
- `models/` — Pydantic v2, re-exported from `models/__init__.py`; `PaginatedResult[T]` wraps list responses.
- `server.py` — FastMCP instance; lifespan owns `PolarionClient`.

## Non-Negotiable Rules

- NEVER `print()` — stdout is MCP JSON-RPC; log to stderr.
- NEVER `typing.Any` — concrete types or `object`.
- All functions: full annotations + `from __future__ import annotations`. Tool functions: `async def` returning Pydantic model.
- Body fields asymmetric by tool purpose:
  - Round-trip: `get_*(include_*_html=True)` returns raw Polarion HTML; `update_*(*_html=...)` accepts verbatim — no sanitize/convert.
  - Greenfield create (Markdown): `markdown_to_html` + `sanitize_html`. Post-create edits = raw-HTML round-trip; formats never mix.
  - Synthesis (READ-ONLY): `read_*` convert HTML→Markdown; feeding output back to writes loses Polarion markup.
- Write payloads skip `None`/empty (Polarion reads empty as "clear default"). Resource POSTs wrap in `{"data": [...]}`; action endpoints (`.../actions/<name>`) take flat object.
- Every list tool: `page_size` (max 100) + `page_number` → `PaginatedResult[T]` with `has_more`.
- Every write tool: `dry_run: bool = False` — return payload without hitting Polarion.
- Error mapping: `PolarionNotFoundError`→`ValueError`, `PolarionAuthError`→`PermissionError`, `PolarionError`→`RuntimeError`.
- Guards fail closed: validation GET error blocks write; only successful empty option set defers to Polarion.
- Docstrings = LLM manual, Google-style; only prose above `Args:` ships — keep tight; return-field bullets in sync with model. Field descriptions one line, skip when name + type say all.
- No `WARNING:`/`NOTE:` prefixes, no dev-narrative, no banner dividers. CLAUDE.md dev-only — MCP-user info lives in `@mcp.tool` docstring. Module docstrings = why module exists; constraints inline next to what they constrain.
- Comments: one line, explain why not what; never restate self-evident code. No dead code, no stray `TODO`s; keep comments in sync when code changes.

## Polarion API Gotchas

- Baseline: Polarion REST API v2506 — assume that version's behavior.
- JSON:API v1. HTML stored as `{"type": "text/html", "value": "..."}`.
- Linked-work-item ids = 5 segments — derive targets via `relationships.workItem.data.id`, never parse. Module ids = 3 segments, doc names may contain `/` — use `split_module_id`.
- Lucene: trailing wildcards OK, leading 400. `module`/`description` not indexed — use `query="SQL:(...)"`; recipes via `get_sql_query_recipes`.
- Server limits: ≤3 req/s, no concurrency. Client serializes via lock + paces every request to ≤3 req/s (start-based min-interval, so slow requests add no extra wait); writes also add a 1.5s post-delay; retries 429/5xx.
- Sparse fieldset drops `relationships` block — list relationship names explicitly. To-many need `include=`; nested dot-path drops intermediate resource (`module,module.author`, not `module.author` alone).
- `/backlinkedworkitems` unsupported — back direction via `query=linkedWorkItems:{wi}`, so back results have `role=None`.
- Polarion validates neither custom-field ids (unknown keys persist; wrong-type 400), nor enum values, nor link targets/roles — `guard.py` validates pre-write. `getAvailableOptions` = only key→enum-options API (non-enum/unknown → 404). Link/hyperlink roles not there — use `GET /projects/{p}/enumerations/~/{enumName}/~` (`data` is dict, not list).
- Custom fields inline under `attributes` (no `customFields` container; `@all` tokens dropped). `GET /projects/{p}/documents` absent on some builds.

## Testing

- `tests/` mirrors source one-to-one; shared fixtures in `tests/` `conftest.py`; `mock_client`/`mock_ctx` + autouse guard-cache reset in `tools/conftest.py`.
- `pytest-asyncio` `mode=auto`. Tool tests call functions directly (`@mcp.tool` returns original); client tests use `respx`. Pydantic `Field` constraints bypass JSON Schema on direct calls — verify via `TypeAdapter` reconstruction.
- New `@mcp.tool` requires updating `EXPECTED_TOOL_NAMES` in `test_mcp_transport.py`.
- `tests/evals/` opens with `pytest.importorskip` (`evals` group; CI syncs `--group evals`).

## Evals — deploy gate

`evals/` drives real LLM through in-memory server against mocked Polarion; deterministic checks, no judge. Hard gate before PyPI publish (`triggers`/`safety` min_pass_rate 1.0; `efficiency`/`orchestration` 0.8). New-case + coverage rules in [evals/README.md](evals/README.md); `tests/evals/test_coverage.py` enforces every tool covered or deferred.

## Repo Conventions

Full rules in [.github/CONTRIBUTING.md](.github/CONTRIBUTING.md); enforced by `.githooks/commit-msg` + `.claude/hooks/`.

- Branches off `main`: `<type>/<short-kebab-summary>`. Commits: `type(scope): summary` ≤50 chars + 2-bullet body (motivation, change).
- PR checklist: flip `[ ]`→`[x]`; don't delete unchecked options.
- Squash merge only; NEVER `--subject` to `gh pr merge`. Force-push feature branches only with explicit authorization; never `main`.