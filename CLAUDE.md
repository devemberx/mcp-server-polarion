# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

MCP server giving AI assistants read/write access to Polarion ALM via MCP. FastMCP 3.0, strict async, fully typed.

## Commands

```bash
uv sync --dev                                            # install deps
uv run pytest                                            # all tests
uv run pytest tests/mcp_server_polarion/tools/test_read.py::TestGetWorkItem  # single class
uv run pytest -k test_page_size_rejects_above_max        # single test
uv run ruff check . && uv run ruff format . && uv run mypy src/  # lint + format + types
uv run mcp-server-polarion                               # run server (stdio)
```

CI: `ruff check` → `ruff format --check` → `mypy` → `pytest`.

## Architecture

- **`core/`** — `client.py` (async httpx wrapper: 429/5xx backoff, post-mutation delay, maps to `PolarionError`/`PolarionAuthError`/`PolarionNotFoundError`), `config.py` (Pydantic settings `POLARION_URL`/`POLARION_TOKEN`), `logging.py` (stderr-only), `exceptions.py`. Every module: `logging.getLogger("mcp_server_polarion.<module>")`.
- **`tools/`** — `read.py` / `write.py` (each write tool has a `_build_*_payload` helper as the unit-test seam), `_helpers.py` (sparse-fieldset constants, JSON:API extractors, pagination, custom-field merge), `_cache.py` (generic `TTLCache[K, V]` + all cache state behind get/store/record wrappers), `_guard.py` (enum / custom-field write guards; reads `_cache.py`).
- **`utils/html.py`** — Markdown ↔ HTML (markdownify + BeautifulSoup4 sanitize), `stamp_block_ids` (write-side anchor injection), `first_anchorless_block` (reject anchorless body blocks).
- **`models.py`** — Pydantic v2. `PaginatedResult[T]` wraps all list responses.
- **`server.py`** — FastMCP instance; lifespan opens/closes `PolarionClient`.

## Non-Negotiable Rules

- **NEVER `print()`** — stdout is MCP JSON-RPC; log to stderr.
- **NEVER `typing.Any`** — concrete types or `object`.
- All functions: full annotations + `from __future__ import annotations`. All tool functions: `async def` returning a Pydantic model.
- **Body fields are asymmetric by tool purpose**:
  - **Round-trip** (lossless): `get_*(include_*_html=True)` returns raw Polarion HTML; `update_*(*_html=...)` accepts it verbatim — no sanitize/convert. XSS is Polarion's renderer's job.
  - **Greenfield create** (Markdown): `create_work_items(description=...)` and `create_document(home_page_content=...)` run `markdown_to_html` + `sanitize_html`. Post-create edits switch to raw-HTML round-trip; the formats never mix.
  - **Synthesis** (Markdown, READ-ONLY): `read_document` / `read_document_parts` / `read_work_item` convert HTML→Markdown. Feeding output back to writes loses Polarion markup.
- **Write payloads** skip `None`/empty (Polarion reads empty as "clear default"). Resource POSTs wrap in `{"data": [...]}`; action endpoints (`.../actions/<name>`) take a flat object.
- Every list tool: `page_size` (max 100) + `page_number`; returns `PaginatedResult[T]` with `has_more`.
- Every write tool: `dry_run: bool = False` — build & return the `_build_*_payload` JSON:API payload without hitting Polarion.
- Tool docstrings are the LLM's manual — Google-style. **Only prose above `Args:` ships to clients** (FastMCP strips `Args:`/`Returns:`/`Raises:`); keep it tight (largest always-loaded payload). Keep return-field bullets in sync with the Pydantic model.
- **Error mapping**: `PolarionNotFoundError`→`ValueError`, `PolarionAuthError`→`PermissionError`, `PolarionError`→`RuntimeError`.

## Comment & Docstring Style

Applies to ALL comments/docstrings (tools, helpers, inline, CLAUDE.md).

- Field descriptions stay one line; skip when name + type say everything. Cross-model invariant: `tests/mcp_server_polarion/test_models.py::test_field_descriptions_are_non_empty_when_set`.
- No `WARNING:` / `FOOTGUN:` / `NOTE:` prefix upgrades — state the fact plainly.
- No dev-narrative ("verified via smoke test", "we tried X then switched to Y", "as of vN") — belongs in commit messages and PR descriptions.
- No banner-divider comments (`# ---`, `# === Section ===`).
- **CLAUDE.md is dev-only.** Other MCP hosts (Cursor / Copilot / generic FastMCP clients) never load it — anything an MCP-user LLM needs must live inside the `@mcp.tool` docstring, even if it duplicates content here.
- Module docstrings explain *why* the module exists; specific timing / sizing / refactor history goes inline next to the thing it constrains.

## Polarion API & Gotchas

- **Endpoints** (JSON:API v1): `/projects`, `/projects/{p}/workitems[?query=]`, `/projects/{p}/workitems/{wi}[/linkedworkitems]`, `/projects/{p}/spaces/{s}/documents/{d}[/parts]`. HTML stored as `{"type": "text/html", "value": "..."}`.
- **ID shapes**: linked-work-item ids are 5 segments — derive targets via `relationships.workItem.data.id`, never parse. Module ids are 3 segments, document names may contain `/` — use `split_module_id`.
- **Lucene**: trailing wildcards OK, leading 400. `module` / `description` not indexed — use `query="SQL:(...)"` for module/custom-field/traceability/body searches. SQL fundamentals (escaping, `C_DESCRIPTION` CLOB caveat, `LIKE` rejected inside `EXISTS`→use top-level `WHERE` + `INNER JOIN`) live in the `list_work_items` docstring; full recipe gallery via the `get_sql_query_recipes` tool (loads `tools/guides/sql_query_recipes.md` on demand — keeps the gallery out of always-loaded context while staying model-callable in every MCP host, unlike a resource).
- **Server limits**: ≤3 req/s, no concurrency. `PolarionClient` retries 429/5xx but does NOT serialize client-side.

### JSON:API quirks

- **Sparse fieldset drops relationships too.** `fields[workitems]=title,type` silently drops the `relationships` block — list relationship names explicitly (`WORK_ITEM_LIST_FIELDS`), else derived fields (`space_id`/`document_name`/`assignee_ids`/`author_id`) empty out.
- **To-many relationships need `include=`**; to-one (`module`, `author`, `project`) inline without it.
- **Backlinks**: `/backlinkedworkitems` unsupported. `list_work_item_links` falls back to `query=linkedWorkItems:{wi}` for the back direction, so back results have `role=None`.
- **Custom fields inline under `attributes`** (no `customFields` container; `@all` tokens dropped). Server fetches `fields[*]=@all` and splits via `STANDARD_*_ATTRIBUTES` allowlists. Polarion does NOT validate custom-field ids — unknown keys persist as silent ghosts; wrong-type values 400.
  - Guarded updates: `update_work_item.custom_fields`→`guard_work_item_custom_field_keys` (keys in a `(project, type)` set from `get_work_item`); `update_document.custom_fields`→`guard_document_custom_field_keys` (`(project, space, document)` set from `get_document`). Cache miss = one priming GET. Create paths can't be validated (no config endpoint) → stderr warning only.
- **Enum validation is absent in Polarion**, enforced at the tool layer. Unknown `type`/`status`/`severity`/`priority`/`resolution` would persist and never match Lucene; `guard_work_item_enums` / `guard_document_enums` fetch `getAvailableOptions` and raise `ValueError` with the valid ids. `type` is checked first (an invalid `change_type_to` raises before being reused as the scoping axis; status/severity/resolution are scoped by target type).
  - **Link / hyperlink roles** aren't in `getAvailableOptions`: `guard_work_item_link_roles` / `guard_hyperlink_roles` fetch `GET /projects/{p}/enumerations/~/{enumName}/~` (`workitem-link-role`/`hyperlink-role`) via `fetch_project_enum_option_ids`. That response's `data` is a **dict** (`data.attributes.options[].id`), unlike `getAvailableOptions`'s list.
- **Guards are fail-closed.** A validation GET that errors after backoff blocks the write: auth failure → `PermissionError`, else → `RuntimeError` (a ghost write is invisible in the UI and unrecoverable). Lone lenient case: a *successful* empty option set defers to Polarion. TTL `_GUARD_TTL_SECONDS` = 60s; tests drive expiry by patching `tools._cache._now`.

### Document writes

- **Body edits go through `homePageContent` PATCH, not `/parts`.** `update_document(home_page_content_html=...)` carries the full body. `/parts` POST and `actions/moveToDocument` are wrappers that reject heading-type items. Empty `home_page_content_html` is rejected (would orphan every heading).
- **`<hN>` alone is safe; anchorless `<p>`/`<ul>`/`<ol>`/`<table>`/`<div>`/`<blockquote>`/`<pre>` are NOT** — PATCH 200, next `GET .../parts` 500. Each needs a unique non-empty `id=`. `create_document` runs `stamp_block_ids`; `update_document` is raw HTML, so the tool hard-rejects anchorless blocks via `first_anchorless_block` (`ValueError` before PATCH, on `dry_run` too). For body text, prefer `create_work_items` + `move_work_item_to_document`.
- **`module` cannot be set via `PATCH /workitems/{wi}` nor by injecting `<div ...module-workitem...>`** (leaves a half-attached `space_id=""`/`outline_number=""` state). Use the action pair `moveToDocument` / `moveFromDocument` — only `move_work_item_to_document` sets `module` + `outline_number` + `homePageContent` atomically. `create_work_item` doesn't expose `module` (would land in recycle bin) — create free-floating then move. `moveFromDocument` isn't idempotent (400 if already detached). `moveToDocument` auto-creates one link to the enclosing heading (project-config role, removed on detach); a later same-role `create_work_item_links` 201s but isn't persisted (phantom success).

### Work item & comment quirks

- **Link tools**: `create_work_item_links` (bulk), `update_work_item_link` (single), `delete_work_item_links` (idempotent 204). Composite ids `<srcProj>/<srcWI>/<role>/<tgtProj>/<tgtWI>` from `WorkItemLinkRef`. Bulk tools cap at `MAX_BULK_ITEMS` (50). Create POST is atomic — a 4xx (e.g. duplicate `(role,target)` 409) rolls back the batch, so re-query `list_work_item_links` before retry. Polarion validates neither target existence nor role, so the create path guards both (`guard_work_item_link_targets`, `guard_work_item_link_roles`); `update_work_item_link` is unguarded (an unknown role there 404s loudly). `delete_work_item_links` pre-reads outgoing links (`partition_delete_links`) → `deleted_link_ids` / `not_found_link_ids` (no-op reported, never raised); pre-read is fail-closed and runs on `dry_run`.
- **`update_work_item_link.suspect`** is tri-state on update (`None`=unchanged) vs `WorkItemLinkSpec.suspect: bool = False` on create. At least one of `suspect`/`revision` required.
- **`PATCH /workitems`** needs ≥1 `attributes`/`relationships` entry — 400s even when only `workflowAction`/`changeTypeTo` is set (tool validates). `changeTypeTo` resets `status` to the new type's initial state — re-apply in a follow-up if preservation matters.
- **`update_document_comment`** PATCH takes only the full 4-segment id and only on root comments (replies 400 → `RuntimeError`); resolving a root marks the whole thread resolved.

## Testing

`tests/` mirrors every source tree one-to-one — `tests/mcp_server_polarion/` (with `core/`, `tools/`, `utils/`) mirrors the package, `tests/evals/` mirrors `evals/` (`evaluators/`, `harness/`, `cases/`), and `tests/claude_hooks/` + `tests/github_scripts/` mirror the loose scripts under `.claude/hooks/` + `.github/scripts/`. One test module per source module; `conftest.py` stays at the `tests/` root so its shared fixtures reach the whole tree.

`pytest-asyncio` in `mode=auto`. **Tool tests** (`tests/mcp_server_polarion/tools/`) call tool functions directly with an injected `mock_client` (FastMCP 3.0's `@mcp.tool` returns the original function). **Client tests** (`tests/mcp_server_polarion/core/test_client.py`) use `respx`. Shared fixtures live in `tests/conftest.py`; pass `write_delay=0` for real `PolarionClient` instances. Pydantic `Field` constraints bypass FastMCP's JSON Schema on direct calls — verify via `TypeAdapter` reconstruction (see `TestCreateWorkItemFieldValidation`).

**Transport tests** (`tests/mcp_server_polarion/test_mcp_transport.py`) drive the server through `fastmcp.Client(mcp)` in-memory transport so registration → JSON Schema → lifespan → `get_client(ctx)` → real `PolarionClient` → mocked HTTP runs end to end. Adding a new `@mcp.tool` requires updating `EXPECTED_TOOL_NAMES` — that is the forcing function. The fixture monkeypatches `_WRITE_DELAY_SECONDS` because the lifespan constructs `PolarionClient` itself.

Tests for the eval harness (`tests/evals/`) import `strands` / `strands_evals`, only present in the `evals` dependency group, so they open with `pytest.importorskip` — they run in CI because `ci.yml` syncs `--group evals`, and skip (rather than error) on a bare `uv sync --dev`.

## Evals — Tier-1 deploy gate

`evals/` (separate group, not in the wheel) drives a real LLM agent through the in-memory server against a mocked Polarion and deterministically asserts no forbidden/footgun action — **no LLM judge**. Hard gate before the PyPI publish jobs in `.github/workflows/publish.yml`; each case passes at `min_pass_rate=1.0`. `EVAL_MODEL` switches model (CI `openai/gpt-4o-mini`). Adding a case: register a check in `evals/evaluators/checks.py::REGISTRY`, add a `Case` to `cases/tier1_prohibitions.py`, and **phrase the task neutrally** (stating the rule tests the prompt, not the docstrings). Detail in [evals/README.md](evals/README.md).

## Repo Conventions

Full rules in [.github/CONTRIBUTING.md](.github/CONTRIBUTING.md); enforced by `.githooks/commit-msg` + the `.claude/hooks/` PR hooks (English-only bodies, template checkboxes, squash-only merges). Quick reference:

- **Branches**: `<type>/<short-kebab-summary>` off `main`, one topic each. Types: `feature|fix|refactor|docs|chore|ci`.
- **Commits**: `type(scope): summary` ≤50 chars, lowercase imperative, no period. Types: `feat|fix|docs|refactor|perf|test|ci|chore`. Scopes: `tool|server|transport|config|deps|utils|model|project|meta|git`. Body: blank line + exactly 2 bullets (motivation, then change), ≤120 chars each, no prefixes.
- **PR template checklist**: flip `[ ]`→`[x]`; don't delete unchecked options.
- **Squash merge only** — let the PR title become the subject; NEVER pass `--subject` to `gh pr merge`.
- **Force push** feature branches only after explicit authorization; never `main`.
