# CLAUDE.md

MCP server: AI read/write Polarion ALM. FastMCP 3.0, strict async, fully typed.

## Commands

```bash
uv sync --dev                                            # install deps
uv run pytest                                            # all tests
uv run ruff check . && uv run ruff format . && uv run mypy src/  # lint + format + types
uv run pytest --cov=src/mcp_server_polarion --cov=evals --cov-report=xml \
  && uv run diff-cover coverage.xml --compare-branch=origin/main --fail-under=90  # changed-line gate — run before push
uv run mcp-server-polarion                               # run server (stdio)
```

CI: same order + `ruff format --check` + `pytest --cov-fail-under=90`.
diff-cover changed lines ≥90% cover `src/` + `evals/` both — incl parser defensive branches + `evals/harness` request handlers.

## Architecture

- `core/client.py` — async httpx; serialize + pace + retry per Gotchas.
- `core/config.py` — `POLARION_URL`/`POLARION_TOKEN`/`POLARION_MAX_REQUESTS_PER_SECOND`.
- `core/logging.py` — stderr-only.
- `tools/` — domain module per resource; `tools/__init__.py` import registers `@mcp.tool`s.
- `_build_*_payload` = unit-test seam.
- `tools/_shared/` — `parse.py` (JSON:API→models), `pagination.py`, `fields.py`/`custom_fields.py` (sparse-fieldset + custom-field policy), `cache.py` (`TTLCache`), `sql.py`.
- `tools/_shared/guard/` — pre-write validation, submodule per domain axis; new guards compose `_http.py` `guarded_get`/`guarded_pages` + `_custom_keys.py` `check_custom_keys`.
- `tools/guides/` — on-demand data served by `recipes.py`.
- `middleware.py` — compact tool-arg `ValidationError` to one-line summary (raw Pydantic dump = token waste).
- `utils/html.py` — Markdown↔HTML, `stamp_block_ids`, `first_anchorless_block`.
- `models/` — `PaginatedResult[T]` wrap list responses; re-export from `models/__init__.py`.
- `server.py` — FastMCP instance; lifespan owns `PolarionClient`.

## Non-Negotiable Rules

- NEVER `print()` — stdout = MCP JSON-RPC; log to stderr.
- NEVER `typing.Any` — concrete types or `object`.
- NEVER ship delete tool for unrecoverable data (attachment, work item, document) even where REST allow it — removal = human via portal.
  - `dry_run` + `destructiveHint` = advisory, model still call with `dry_run=False`; withhold capability = only hard guarantee.
  - Reversible relationship delete allowed (`delete_work_item_links` — recreate from context, zero data loss).
  - Deliberate posture, not gap — no re-litigate per review round.
- All functions: full annotations + `from __future__ import annotations`. Tool functions: `async def` return Pydantic model.
- Body fields asymmetric by tool purpose:
  - Round-trip: `get_*(include_*_html=True)` return raw Polarion HTML; `update_*(*_html=...)` accept verbatim — no sanitize/convert.
  - Greenfield create (Markdown): `markdown_to_html` + `sanitize_html`. Post-create edits = raw-HTML round-trip; formats never mix.
  - Synthesis (READ-ONLY): `read_*` convert HTML→Markdown; feed output back to writes lose Polarion markup.
- Write payloads skip `None`/empty (Polarion read empty as "clear default").
- Resource POSTs wrap in `{"data": [...]}`; action endpoints (`.../actions/<name>`) take flat object.
- Every list tool: `page_size` (max 100) + `page_number` → `PaginatedResult[T]` with `has_more`.
- Every write tool: `dry_run: bool = False` — return payload, no hit Polarion.
- Timestamps (where Polarion serve both): `list_*` summary = `updated` only; metadata-bearing `get_*`/`read_*` detail add `created`.
  - `read_document` body-only synthesis exempt. Domain without `get_*` tool: list expose what API serve (comments `created`-only). API without timestamps: omit.
- Error mapping: `PolarionNotFoundError`→`ValueError`, `PolarionAuthError`→`PermissionError`, `PolarionError`→`RuntimeError`; user-fixable status allowed narrower map (attachment 409 dup fileName→`ValueError`).
- Guards fail closed: validation GET error block write; only successful empty option set defer to Polarion.

### Docstrings

- Docstrings = LLM manual, Google-style; only prose above `Args:` ship — keep tight; return-field bullets sync with model.
- Field descriptions one line, skip when name + type say all.
- Tool description template (order, skip empty): [1] verb-first what + hard limits; [2] sibling routing ("— use X instead"); [3] call strategy only if behavior-change (REPLACE / "Call X first" / Atomic); [4] round-trip format rules; [5] returns + follow-up; [6] errors as prevention-form ("resolve via list_*_enum_options first").
- Shipped text = docstring prose + `Field(description=...)` + input spec-model class docstrings (`$defs` ship them).
- Bans + budgets (read ≤~50, write ≤~150 words) enforced by `test_tool_description_style.py`: no exception class names / raw HTTP codes / RST double-backticks, params single-line, `dry_run` byte-exact. Caps only NEVER/REPLACE/Atomic.
- Eval-FAIL-restored phrase = lock via docstring contract test.
- No `WARNING:`/`NOTE:` prefixes, dev-narrative, banner dividers.
- CLAUDE.md dev-only — MCP-user info live in `@mcp.tool` docstrings. Module docstring = why module exist; constraints inline next to what they constrain.
- Comments + dev docstrings caveman-style: drop articles/filler — `# Custom key match standard attr = silent shadow.`
  - Why not what; never restate self-evident code; one distinct fact per line. Technical terms/ids/API names/numbers exact; no invented abbreviations.
  - Exempt (LLM-facing, eval-gated): `@mcp.tool` docstrings + `Field(description=...)` — normal prose per Docstrings rule.
  - `TODO` = `# TODO(#issue): concrete action`. No dead code; comments sync when code change.

## Naming Rules (LLM surface: params + model fields)

- Cross-resource ref = full-noun `<resource>_id` (`project_id`, `work_item_id`, `test_run_id`, `test_case_id`, `comment_id`, `field_id`, `defect_id`, `template_id`).
- Documents own no id — address = `space_id` + `document_name` pair; `module_*` never on LLM surface (API `moduleName` map inside payload builder only).
- Other location = `target_*` prefix (`target_project_id`, `target_space_id`, `target_document_name`, `target_work_item_id`).
- Own id in own Summary/Detail model = bare `id`.
- Composite resource (work_item_**link**, test_**record**) drop parent prefix in own echo/selector fields (`link_id(s)`, `record_id(s)`); tool names + cross-domain refs stay full.
- Create spec mirror resource attributes (client-supplied id = bare `id`, e.g. `TestRunCreateSpec.id`); update spec = target selector (`work_item_id`/`test_run_id`/`record_id`) + changed attributes.
- Polarion camelCase → snake_case split at case boundary exact (`homePageContent` → `home_page_content`, `finishedOn` → `finished_on`); ad-hoc compounds banned (never "homepage").
- Person fields = `<role>_id`/`<role>_name` parallel scalar pairs (author, assignee(s), last_updated_by, executed_by); list/summary = name only, detail add id.
- Bools: state field = API-derived (`is_template`, `resolved`, `suspect`); read-expansion flag = `include_*_html`; list filter param describe result set (`templates`) — 1:1 field-name match not required.

## Polarion API Gotchas

Baseline: Polarion REST API v2506 — assume that version behavior. Rules below hold everywhere; per-domain contracts live in `docs/polarion-api/` and MUST be read before touching that domain. New domain doc = copy `docs/polarion-api/_TEMPLATE.md`, keep its section order.

| Domain | Read before editing |
|---|---|
| attachments (document / work item / test record) | [docs/polarion-api/attachments.md](docs/polarion-api/attachments.md) |
| test runs, test records, e-signature | [docs/polarion-api/test-runs.md](docs/polarion-api/test-runs.md) |
| documents, `renderingLayouts`, copy | [docs/polarion-api/documents.md](docs/polarion-api/documents.md) |
| comments (all three resources) | [docs/polarion-api/comments.md](docs/polarion-api/comments.md) |
| work items, links | [docs/polarion-api/work-items.md](docs/polarion-api/work-items.md) |

- JSON:API v1. HTML stored as `{"type": "text/html", "value": "..."}`.
- Linked-work-item ids = 5 segments — derive targets via `relationships.workItem.data.id`, never parse.
- Module ids = 3 segments, doc names may contain `/` — use `split_module_id`.
- Lucene: trailing wildcards OK, leading 400. `module`/`description` not indexed — use `query="SQL:(...)"`; recipes via `get_sql_query_recipes`.
- Sparse fieldset drop `relationships` block — list relationship names explicit.
- To-many need `include=`; nested dot-path drop intermediate resource (`module,module.author`, not `module.author` alone).
- Resource with every requested attr unset ship no `attributes` block at all — parsers default it.
- Custom fields inline under `attributes` (no `customFields` container; `@all` tokens dropped).
- Polarion validate neither custom-field ids (unknown keys persist; wrong-type 400), nor enum values, nor link targets/roles — `guard/` validate pre-write.
- `getAvailableOptions` = only key→enum-options API (non-enum/unknown → 404). Link/hyperlink roles not there — use `GET /projects/{p}/enumerations/~/{enumName}/~` (`data` = dict, not list).
- Server throttle deployment-configured (vary per instance), no concurrency, no `Retry-After`/rate-limit headers served — client pacing = only defense (`core/client.py` serialize + pace + write post-delay + 429/5xx backoff).

## Testing

- `tests/mcp_server_polarion/` mirror `src` package one-to-one (`tests/` also hold `claude_hooks/`, `github_scripts/`, `evals/`).
- Shared fixtures `tests/conftest.py`; `mock_client`/`mock_ctx` + autouse guard-cache reset in `tools/conftest.py`.
- `pytest-asyncio` `mode=auto`. Tool tests call functions directly (`@mcp.tool` return original); client tests use `respx`.
- Pydantic `Field` constraints bypass JSON Schema on direct call — verify via `TypeAdapter` reconstruction.
- New `@mcp.tool` needs update `EXPECTED_TOOL_NAMES` in `test_mcp_transport.py` + README tool-table row (marker-anchored sync test, same file).
- `tests/evals/` open with `pytest.importorskip` (`evals` group; CI sync `--group evals`).

## Evals — deploy gate

`evals/` drive real LLM through in-memory server against mocked Polarion; deterministic checks, no judge. Hard gate before PyPI publish (`triggers`/`safety` min_pass_rate 1.0; `efficiency`/`orchestration` 0.8). New-case + coverage rules in [evals/README.md](evals/README.md); `tests/evals/test_coverage.py` enforce every tool covered or deferred.

## Repo Conventions

Full rules in [.github/CONTRIBUTING.md](.github/CONTRIBUTING.md); enforced by `.githooks/commit-msg` + `.claude/hooks/`. Agent-specific deltas:

- Branches off `main`: `<type>/<short-kebab-summary>`; type = feat|fix|refactor|test|docs|chore|ci (pre-push enforce; `feature/` reject).
- Commits: `type(scope): summary` ≤50 chars + 2-bullet body (motivation, change).
- PR checklist: flip `[ ]`→`[x]`; don't delete unchecked options.
- NEVER `--subject` to `gh pr merge`. Force-push feature branches only with explicit authorization; never `main`.
- Outward text (PR/issue/commit/release/gist/repo-description, branch names at push) NEVER carry private deployment names (real Polarion project/space/document ids) — generic wording ("live testdrive project"). `block_sensitive_text.py` hook scan vs untracked `.claude/sensitive-patterns.local`.
