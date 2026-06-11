# CLAUDE.md

MCP server: AI read/write access to Polarion ALM. FastMCP 3.0, strict async, fully typed.

## Commands

```bash
uv sync --dev                                            # install deps
uv run pytest                                            # all tests
uv run pytest -k test_page_size_rejects_above_max        # single test
uv run ruff check . && uv run ruff format . && uv run mypy src/  # lint + format + types
uv run mcp-server-polarion                               # run server (stdio)
```

CI: `ruff check` → `ruff format --check` → `mypy` → `pytest`.

## Architecture

- `core/` — `client.py` (async httpx: 429/5xx backoff, post-mutation delay, maps to `PolarionError`/`PolarionAuthError`/`PolarionNotFoundError`), `config.py` (`POLARION_URL`/`POLARION_TOKEN`), `logging.py` (stderr-only). Loggers: `logging.getLogger("mcp_server_polarion.<module>")`.
- `tools/` — domain modules (`projects`, `work_items`, `documents`, `links`, `comments`, `moves`); each create/update tool has `_build_*_payload` helper = unit-test seam. Import in `tools/__init__.py` registers `@mcp.tool`s. `tools/_shared/`: `helpers.py` (JSON:API extractors, pagination, `MAX_BULK_ITEMS`), `cache.py` (`TTLCache`, all cache state behind wrappers), `guard.py` (write guards). `tools/guides/` = on-demand data (`sql_query_recipes.md`).
- `utils/html.py` — Markdown ↔ HTML, `stamp_block_ids`, `first_anchorless_block`.
- `models/` — Pydantic v2 by domain, re-exported from `models/__init__.py`. `PaginatedResult[T]` wraps all list responses.
- `server.py` — FastMCP instance; lifespan owns `PolarionClient`.

## Non-Negotiable Rules

- NEVER `print()` — stdout is MCP JSON-RPC; log to stderr.
- NEVER `typing.Any` — concrete types or `object`.
- All functions: full annotations + `from __future__ import annotations`. Tool functions: `async def` returning Pydantic model.
- Body fields asymmetric by tool purpose:
  - Round-trip: `get_*(include_*_html=True)` returns raw Polarion HTML; `update_*(*_html=...)` accepts verbatim — no sanitize/convert.
  - Greenfield create (Markdown): `create_work_items(description=...)` + `create_document(home_page_content=...)` run `markdown_to_html` + `sanitize_html`. Post-create edits = raw-HTML round-trip; formats never mix.
  - Synthesis (Markdown, READ-ONLY): `read_*` tools convert HTML→Markdown; feeding output back to writes loses Polarion markup.
- Write payloads skip `None`/empty (Polarion reads empty as "clear default"). Resource POSTs wrap in `{"data": [...]}`; action endpoints (`.../actions/<name>`) take flat object.
- Every list tool: `page_size` (max 100) + `page_number` → `PaginatedResult[T]` with `has_more`.
- Every write tool: `dry_run: bool = False` — return payload without hitting Polarion.
- Tool docstrings = LLM's manual, Google-style. Only prose above `Args:` ships to clients (FastMCP strips the rest); keep tight. Return-field bullets in sync with Pydantic model.
- Error mapping: `PolarionNotFoundError`→`ValueError`, `PolarionAuthError`→`PermissionError`, `PolarionError`→`RuntimeError`.

## Comment & Docstring Style

Applies to ALL comments/docstrings incl. CLAUDE.md.

- Field descriptions one line; skip when name + type say all.
- No `WARNING:`/`NOTE:` prefixes — state fact plainly. No dev-narrative ("we tried X then Y"). No banner-divider comments.
- CLAUDE.md is dev-only — other MCP hosts never load it; anything an MCP-user LLM needs must live in the `@mcp.tool` docstring, even if duplicated here.
- Module docstrings = why module exists; timing/sizing constraints go inline next to what they constrain.

## Polarion API & Gotchas

- JSON:API v1. HTML stored as `{"type": "text/html", "value": "..."}`.
- ID shapes: linked-work-item ids = 5 segments — derive targets via `relationships.workItem.data.id`, never parse. Module ids = 3 segments, document names may contain `/` — use `split_module_id`.
- Lucene: trailing wildcards OK, leading 400. `module`/`description` not indexed — use `query="SQL:(...)"`. SQL fundamentals in `list_work_items` docstring; recipes via `get_sql_query_recipes` tool.
- Server limits: ≤3 req/s, no concurrency. Client retries 429/5xx, does NOT serialize client-side.
- Sparse fieldset drops `relationships` block too — list relationship names explicitly (`WORK_ITEM_LIST_FIELDS`). To-many relationships need `include=`; to-one inline without it.
- Nested `include=` dot-path drops the intermediate resource — `module.author` alone includes users but not documents; list both (`module,module.author`).
- `/backlinkedworkitems` unsupported — back direction via `query=linkedWorkItems:{wi}`, so back results have `role=None`.
- Custom fields inline under `attributes` (no `customFields` container; `@all` tokens dropped). Polarion does NOT validate custom-field ids (unknown keys persist as silent ghosts; wrong-type values 400) — `guard_work_item_custom_fields` / `guard_document_custom_fields` validate keys against `(project, type)` schema cached from SQL sample, then enum-typed values (next bullet). Fail closed on SQL error AND empty schema; would-be-unknown key forces one fresh re-fetch before rejecting. Document axis = document-type, sampled via headings + `include=module` (`GET /projects/{p}/documents` absent on some builds).
- Enum validation absent in Polarion — `guard_work_item_enums` / `guard_document_enums` fetch `getAvailableOptions`, raise `ValueError` with valid ids. `type` checked first. Link/hyperlink roles not in `getAvailableOptions` — `fetch_project_enum_option_ids` hits `GET /projects/{p}/enumerations/~/{enumName}/~` (response `data` is dict, not list).
- `getAvailableOptions` works for custom fields too (only API mapping key → enum options); non-enum/unknown field → 404 "not an Enumeration field". `guard_*_custom_fields` checks enum values after keys: non-empty option set ⇒ field is enum ⇒ value must be option-id string or list of them (wrong shape/id → `ValueError`); 404 defers, cached `_ENUM_NOT_FOUND_TTL_SECONDS` (600s — stale worst case is the same deferral).
- Guards fail-closed: validation GET error blocks write (auth → `PermissionError`, else `RuntimeError`). Lone lenient case: successful empty option set defers to Polarion. TTL `_GUARD_TTL_SECONDS` = 60s; tests drive expiry by patching `tools._shared.cache._now`.

### Document writes

- Body edits via `homePageContent` PATCH, not `/parts` (those wrappers reject heading-type items). Empty `home_page_content_html` rejected (would orphan headings).
- Anchorless `<p>`/`<ul>`/`<ol>`/`<table>`/`<div>`/`<blockquote>`/`<pre>` break `/parts` (PATCH 200, next GET 500); each needs unique non-empty `id=`. Both `create_document` and `update_document` run `stamp_block_ids`. `stamp_block_ids` returns input verbatim when every target block already has a non-blank id (no `str(soup)` reserialize → no `&nbsp;`→`\xa0` drift on an anchored round-trip body), else stamps the gaps; both tools follow with a `first_anchorless_block` defensive guard. For body text, prefer `create_work_items` + `move_work_item_to_document`.
- `module` cannot be set via PATCH or HTML injection — only `move_work_item_to_document` / `move_work_item_from_document` (atomic action pair). `create_work_item` doesn't expose `module` (would land in recycle bin) — create free-floating, then move. `moveFromDocument` not idempotent (400 if detached). `moveToDocument` auto-creates one link to enclosing heading; later same-role `create_work_item_links` 201s but isn't persisted (phantom success).

### Work item & comment quirks

- Link ids composite `<srcProj>/<srcWI>/<role>/<tgtProj>/<tgtWI>`. Bulk tools cap at `MAX_BULK_ITEMS` (50). Link-create POST atomic — 4xx rolls back batch; re-query `list_work_item_links` before retry. Polarion validates neither link target nor role — create path guards both; `update_work_item_link` unguarded (unknown role 404s loudly). `delete_work_item_links` pre-reads (fail-closed, runs on `dry_run`) → `deleted_link_ids`/`not_found_link_ids`, never raises on no-op.
- `update_work_item_link.suspect` tri-state (`None`=unchanged) vs create default `False`. At least one of `suspect`/`revision` required.
- `PATCH /workitems` needs ≥1 `attributes`/`relationships` entry (400 otherwise, tool validates). `changeTypeTo` resets `status` to new type's initial state.
- `update_document_comment`: full 4-segment id, root comments only (replies 400 → `RuntimeError`); resolving root resolves whole thread.

## Testing

- `tests/` mirrors every source tree one-to-one; `conftest.py` at `tests/` root for shared fixtures.
- `pytest-asyncio` `mode=auto`. Tool tests call tool functions directly with injected `mock_client` (`@mcp.tool` returns original function); client tests use `respx`. `mock_client`/`mock_ctx` + autouse guard-cache reset in `tests/mcp_server_polarion/tools/conftest.py`. Pydantic `Field` constraints bypass JSON Schema on direct calls — verify via `TypeAdapter` reconstruction.
- Transport tests (`test_mcp_transport.py`) drive server via `fastmcp.Client(mcp)` in-memory. New `@mcp.tool` requires updating `EXPECTED_TOOL_NAMES`.
- `tests/evals/` opens with `pytest.importorskip` (`evals` dependency group; CI syncs `--group evals`).

## Evals — Tier-1 deploy gate

`evals/` drives real LLM agent through in-memory server against mocked Polarion; deterministic checks, no LLM judge. Hard gate before PyPI publish (`min_pass_rate=1.0`). New case: register check in `evals/evaluators/checks.py::REGISTRY`, add `Case` to `cases/tier1_prohibitions.py`, phrase task neutrally (stating rule tests prompt, not docstrings). Detail: [evals/README.md](evals/README.md).

## Repo Conventions

Full rules in [.github/CONTRIBUTING.md](.github/CONTRIBUTING.md); enforced by `.githooks/commit-msg` + `.claude/hooks/`.

- Branches: `<type>/<short-kebab-summary>` off `main`. Types: `feature|fix|refactor|test|docs|chore|ci`.
- Commits: `type(scope): summary` ≤50 chars, lowercase imperative, no period. Types: `feat|fix|docs|refactor|perf|test|ci|chore`. Scopes: `tool|server|transport|config|deps|utils|model|project|meta|git`. Body: blank line + exactly 2 bullets (motivation, then change), ≤120 chars each.
- PR template checklist: flip `[ ]`→`[x]`; don't delete unchecked options.
- Squash merge only; NEVER pass `--subject` to `gh pr merge`.
- Force push feature branches only after explicit authorization; never `main`.
