# CLAUDE.md

MCP server: AI read/write access to Polarion ALM. FastMCP 3.0, strict async, fully typed.

## Commands

```bash
uv sync --dev                                            # install deps
uv run pytest                                            # all tests
uv run ruff check . && uv run ruff format . && uv run mypy src/  # lint + format + types
uv run mcp-server-polarion                               # run server (stdio)
```

CI: `ruff check` → `ruff format --check` → `mypy` → `pytest`.

## Architecture

- `core/` — `client.py` (async httpx, retries 429/5xx, maps to `PolarionError`/`PolarionAuthError`/`PolarionNotFoundError`), `config.py` (`POLARION_URL`/`POLARION_TOKEN`), `logging.py` (stderr-only). Loggers: `logging.getLogger("mcp_server_polarion.<module>")`.
- `tools/` — domain modules; `_build_*_payload` helpers = unit-test seam. Import in `tools/__init__.py` registers `@mcp.tool`s. `tools/_shared/`: `helpers.py` (client/string/path/lucene), `parse.py` (JSON:API→models), `pagination.py` (`make_page`), `fields.py`/`custom_fields.py` (sparse-fieldset constants + custom-field policy), `cache.py` (`TTLCache`), `guard.py` (write guards), `sql.py` (query recipes). `tools/guides/` = on-demand data.
- `utils/html.py` — Markdown ↔ HTML, `stamp_block_ids`, `first_anchorless_block`.
- `models/` — Pydantic v2, re-exported from `models/__init__.py`. `PaginatedResult[T]` wraps all list responses.
- `server.py` — FastMCP instance; lifespan owns `PolarionClient`.

## Non-Negotiable Rules

- NEVER `print()` — stdout is MCP JSON-RPC; log to stderr.
- NEVER `typing.Any` — concrete types or `object`.
- All functions: full annotations + `from __future__ import annotations`. Tool functions: `async def` returning Pydantic model.
- Body fields asymmetric by tool purpose:
  - Round-trip: `get_*(include_*_html=True)` returns raw Polarion HTML; `update_*(*_html=...)` accepts verbatim — no sanitize/convert.
  - Greenfield create (Markdown): `markdown_to_html` + `sanitize_html`. Post-create edits = raw-HTML round-trip; formats never mix.
  - Synthesis (READ-ONLY): `read_*` tools convert HTML→Markdown; feeding output back to writes loses Polarion markup.
- Write payloads skip `None`/empty (Polarion reads empty as "clear default"). Resource POSTs wrap in `{"data": [...]}`; action endpoints (`.../actions/<name>`) take flat object.
- Every list tool: `page_size` (max 100) + `page_number` → `PaginatedResult[T]` with `has_more`.
- Every write tool: `dry_run: bool = False` — return payload without hitting Polarion.
- Tool docstrings = LLM's manual, Google-style. Only prose above `Args:` ships to clients; keep tight. Return-field bullets in sync with Pydantic model.
- Error mapping: `PolarionNotFoundError`→`ValueError`, `PolarionAuthError`→`PermissionError`, `PolarionError`→`RuntimeError`.
- Guards fail closed: validation GET error blocks write; only successful empty option set defers to Polarion.

## Comment & Docstring Style

Applies to ALL comments/docstrings incl. CLAUDE.md.

- Field descriptions one line; skip when name + type say all.
- No `WARNING:`/`NOTE:` prefixes, no dev-narrative, no banner dividers.
- CLAUDE.md is dev-only; anything an MCP-user LLM needs must live in the `@mcp.tool` docstring, even if duplicated here.
- Module docstrings = why module exists; timing/sizing constraints inline next to what they constrain.

## Polarion API Gotchas

- JSON:API v1. HTML stored as `{"type": "text/html", "value": "..."}`.
- Linked-work-item ids = 5 segments — derive targets via `relationships.workItem.data.id`, never parse. Module ids = 3 segments, document names may contain `/` — use `split_module_id`.
- Lucene: trailing wildcards OK, leading 400. `module`/`description` not indexed — use `query="SQL:(...)"`; recipes via `get_sql_query_recipes`.
- Server limits: ≤3 req/s, no concurrency. Client retries 429/5xx, does NOT serialize client-side.
- Sparse fieldset drops `relationships` block — list relationship names explicitly. To-many relationships need `include=`; nested dot-path drops intermediate resource (`module,module.author`, not `module.author` alone).
- `/backlinkedworkitems` unsupported — back direction via `query=linkedWorkItems:{wi}`, so back results have `role=None`.
- Polarion validates neither custom-field ids (unknown keys persist silently; wrong-type 400), nor enum values, nor link targets/roles — `guard.py` validates pre-write. `getAvailableOptions` = only key→enum-options API (non-enum/unknown field → 404). Link/hyperlink roles not there — use `GET /projects/{p}/enumerations/~/{enumName}/~` (response `data` is dict, not list). Guard TTL caches in `tools/_shared/cache.py`; tests drive expiry by patching `tools._shared.cache._now`.
- Custom fields inline under `attributes` (no `customFields` container; `@all` tokens dropped). `GET /projects/{p}/documents` absent on some builds.

### Document writes

- Body edits via `homePageContent` PATCH, not `/parts` (rejects heading-type items). Empty `home_page_content_html` rejected (would orphan headings).
- Anchorless `<p>`/`<ul>`/`<ol>`/`<table>`/`<div>`/`<blockquote>`/`<pre>` break `/parts` (PATCH 200, next GET 500) — each needs unique non-empty `id=`; `create_document`/`update_document` run `stamp_block_ids` + `first_anchorless_block` guard. For body text, prefer `create_work_items` + `move_work_item_to_document`.
- `module` settable only via `move_work_item_to_document` / `move_work_item_from_document`. `create_work_item` doesn't expose `module` (would land in recycle bin) — create free-floating, then move. `moveFromDocument` not idempotent (400 if detached). `moveToDocument` auto-creates link to enclosing heading; later same-role `create_work_item_links` 201s but isn't persisted.

### Work item & comment quirks

- Link ids composite `<srcProj>/<srcWI>/<role>/<tgtProj>/<tgtWI>`. Bulk tools cap at `MAX_BULK_ITEMS` (50). Link-create POST atomic — 4xx rolls back batch; re-query `list_work_item_links` before retry.
- `PATCH /workitems` needs ≥1 `attributes`/`relationships` entry. `changeTypeTo` resets `status` to new type's initial state.
- `update_document_comment`/`update_work_item_comment`: root comments only (replies 400). Document resolve cascades to whole thread; work item resolve flips only that comment (no cascade).
- Comment ids: work item comments 3-segment (`<proj>/<wi>/<cmt>`), document comments 4-segment. `list_work_item_comments`/`list_document_comments` share the `Comment` model + `build_comments_page` parser; work item comments add `title` (own `WORK_ITEM_COMMENT_LIST_FIELDS`), documents send none.
- `create_document_comments`/`create_work_item_comments` share the `_comment_create_payload` helper; document takes base `CommentSpec` (no `title`), work item takes `WorkItemCommentSpec` (subclass adds `title`). Helper emits `title` only when the spec carries one. No `MAX_BULK_ITEMS` cap on either.

## Testing

- `tests/` mirrors source tree one-to-one; shared fixtures in `tests/` root `conftest.py`; `mock_client`/`mock_ctx` + autouse guard-cache reset in `tests/mcp_server_polarion/tools/conftest.py`.
- `pytest-asyncio` `mode=auto`. Tool tests call functions directly (`@mcp.tool` returns original); client tests use `respx`. Pydantic `Field` constraints bypass JSON Schema on direct calls — verify via `TypeAdapter` reconstruction.
- New `@mcp.tool` requires updating `EXPECTED_TOOL_NAMES` in `test_mcp_transport.py`.
- `tests/evals/` opens with `pytest.importorskip` (`evals` dependency group; CI syncs `--group evals`).

## Evals — Tier-1 deploy gate

`evals/` drives real LLM through in-memory server against mocked Polarion; deterministic checks, no judge. Hard gate before PyPI publish (`min_pass_rate=1.0`). New case: check in `evals/evaluators/checks.py::REGISTRY` + `Case` in `cases/tier1_prohibitions.py`; phrase task neutrally. Detail: [evals/README.md](evals/README.md).

## Repo Conventions

Full rules in [.github/CONTRIBUTING.md](.github/CONTRIBUTING.md); enforced by `.githooks/commit-msg` + `.claude/hooks/`.

- Branches: `<type>/<short-kebab-summary>` off `main`. Types: `feature|fix|refactor|test|docs|chore|ci`.
- Commits: `type(scope): summary` ≤50 chars, lowercase imperative, no period. Types: `feat|fix|docs|refactor|perf|test|ci|chore`. Scopes: `tool|server|transport|config|deps|utils|model|project|meta|git`. Body: blank line + exactly 2 bullets (motivation, then change), ≤120 chars each.
- PR template checklist: flip `[ ]`→`[x]`; don't delete unchecked options.
- Squash merge only; NEVER `--subject` to `gh pr merge`. Force-push feature branches only with explicit authorization; never `main`.
