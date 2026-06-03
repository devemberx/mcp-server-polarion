# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

MCP server giving AI assistants read/write access to Polarion ALM via the Model Context Protocol. FastMCP 3.0, strict async, fully typed.

## Commands

```bash
uv sync --dev                                            # install deps
uv run pytest                                            # all tests
uv run pytest tests/tools/test_read.py::TestGetWorkItem  # single class
uv run pytest -k test_page_size_rejects_above_max        # single test
uv run ruff check . && uv run ruff format . && uv run mypy src/  # lint + format + types
uv run mcp-server-polarion                               # run server (stdio)
```

CI: `ruff check` → `ruff format --check` → `mypy` → `pytest`.

## Architecture

- **`core/`** — `client.py` (async httpx wrapper, 429/5xx backoff, post-mutation delay, maps to `PolarionError` / `PolarionAuthError` / `PolarionNotFoundError`), `config.py` (Pydantic settings: `POLARION_URL`, `POLARION_TOKEN`), `logging.py` (stderr-only, called from server lifespan), `exceptions.py`. Every module: `logging.getLogger("mcp_server_polarion.<module>")`.
- **`tools/`** — `read.py` (12 read tools), `write.py` (11 write tools, each with its `_build_*_payload` helper as the unit-testable seam), `_helpers.py` (sparse-fieldset constants, JSON:API extractors, pagination, custom-field merge), `_cache.py` (generic `TTLCache[K, V]` primitive + all cache state — document-discovery, enum-option, and observed custom-field-key caches — behind get/store/record wrappers), `_guard.py` (server-side enum / custom-field write-guard logic; reads the caches in `_cache.py`).
- **`utils/html.py`** — Markdown ↔ HTML (markdownify + BeautifulSoup4 sanitization), `stamp_block_ids` for write-side anchor injection, and `first_anchorless_block` (the inverse predicate the write side uses to reject anchorless body blocks).
- **`models.py`** — Pydantic v2. `PaginatedResult[T]` wraps all list responses.
- **`server.py`** — FastMCP instance with lifespan that opens/closes `PolarionClient`.

## Non-Negotiable Rules

- **NEVER `print()`** — stdout is reserved for MCP JSON-RPC; log to stderr.
- **NEVER `typing.Any`** — use concrete types or `object`.
- All functions: full type annotations + `from __future__ import annotations`. All tool functions: `async def` returning a Pydantic model.
- **Body fields are asymmetric by tool purpose**:
  - **Round-trip pair** (lossless): `get_*(include_*_html=True)` returns raw Polarion HTML; matching `update_*(*_html=...)` accepts it verbatim — no sanitization, no Markdown conversion. XSS filtering is delegated to Polarion's renderer.
  - **Greenfield create** (Markdown): `create_work_items` (per-item `description=...`) and `create_document(home_page_content=...)` run through `markdown_to_html` + `sanitize_html`. After creation, edits switch to raw-HTML round-trip — the two formats never mix.
  - **Synthesis paths** (Markdown): `read_document` / `read_document_parts` / `read_work_item` convert HTML→Markdown. READ-ONLY — feeding output back to writes loses Polarion-specific markup.
- **Write payloads** — skip `None`/empty; Polarion may interpret empty as "clear default". Resource POSTs wrap in `{"data": [...]}`; **action endpoints** (`.../actions/<name>`) take a flat object.
- Every list tool: `page_size` (max 100) + `page_number`; returns `PaginatedResult[T]` with `has_more`.
- Every write tool: `dry_run: bool = False`. When True, build and return the JSON:API payload via `_build_*_payload` without hitting Polarion.
- Tool docstrings are the LLM's manual — Google-style with Args/Returns/Raises. Keep return-field bullets in sync with the Pydantic model.
- **Error mapping at tool layer**: `PolarionNotFoundError` → `ValueError`, `PolarionAuthError` → `PermissionError`, `PolarionError` → `RuntimeError`.

## Comment & Docstring Style

Applies to ALL comments / docstrings (tools, helpers, inline, CLAUDE.md).

- Field descriptions stay one line; skip when name + type say everything. Cross-model invariant: `tests/test_models.py::test_field_descriptions_are_non_empty_when_set`.
- No `WARNING:` / `FOOTGUN:` / `NOTE:` prefix upgrades — state the fact plainly.
- No dev-narrative ("verified via smoke test", "we tried X then switched to Y", "as of vN") — belongs in commit messages and PR descriptions.
- No banner-divider comments (`# ---`, `# === Section ===`).
- **CLAUDE.md is dev-only.** Other MCP hosts (Cursor / Copilot / generic FastMCP clients) never load it — anything an MCP-user LLM needs must live inside the `@mcp.tool` docstring, even if it duplicates content here.
- Module docstrings explain *why* the module exists; specific timing / sizing / refactor history goes inline next to the thing it constrains.

## Polarion API & Gotchas

- **JSON:API v1 endpoints**: `/projects`, `/projects/{p}/workitems[?query=...]`, `/projects/{p}/workitems/{wi}[/linkedworkitems]`, `/projects/{p}/spaces/{s}/documents/{d}[/parts]`. HTML payloads stored as `{"type": "text/html", "value": "..."}`.
- **ID shapes**: linked-work-item ids are 5 segments — derive targets via `relationships.workItem.data.id`, never by parsing. Module ids are 3 segments and document names may contain `/` — always use `split_module_id`.
- **Lucene**: trailing wildcards OK, leading wildcards 400. `module` and `description` are not indexed — use `query="SQL:(...)"` on `list_work_items` for module-scoped / custom-field / traceability / body searches (recipe gallery in the tool's docstring; schema in [SQLQueryExamples.pdf](https://testdrive.polarion.com/polarion/sdk/doc/database/SQLQueryExamples.pdf)). `LIKE` is rejected inside `EXISTS (SELECT ...)` — keep `LIKE` in the top-level `WHERE` via `INNER JOIN`.
- **Server limits**: ≤3 req/s, no concurrent requests. `PolarionClient` retries 429/5xx with backoff but does NOT serialize client-side.

### JSON:API quirks

- **Sparse fieldset filters relationships too.** `fields[workitems]=title,type` silently drops the entire `relationships` block. List relationship names explicitly (see `WORK_ITEM_LIST_FIELDS`); forgetting empties derived fields like `space_id` / `document_name` / `assignee_ids` / `author_id`.
- **To-many relationships need `include=`.** To-one (`module`, `author`, `project`) inline without it.
- **Backlinks**: `/backlinkedworkitems` is unsupported. `list_work_item_links` falls back to `query=linkedWorkItems:{wi}` for the back direction, so back results have `role=None`. Call twice for both directions.
- **Custom fields inline under `attributes`.** No `customFields` container; `customFields.@all` tokens dropped. Server fetches `fields[*]=@all` and splits via `STANDARD_*_ATTRIBUTES` allowlists in `_helpers.py`. Polarion does NOT validate custom-field IDs — unknown keys persist as silent ghosts; always take keys from a prior read. Wrong-type values DO get rejected (400). **mcp-server hard-guards updates**: `update_work_item.custom_fields` via `_guard.py:guard_work_item_custom_field_keys` (keys must be in a `(project, work_item_type)` set populated by `get_work_item`); `update_document.custom_fields` via `guard_document_custom_field_keys` (keys in a `(project, space, document)` set populated by `get_document`). On cache miss each does one inline priming GET. Create paths cannot be schema-validated (no project-config endpoint, no prior read) and fall back to a stderr warning. Keys recorded via `_cache.py:record_work_item_custom_field_keys` / `record_document_custom_field_keys` from the read tools.
- **Server-side enum validation is absent in Polarion**, so the mcp-server tool layer enforces it. Unknown `type` / `status` / `severity` / `priority` / `resolution` ids would persist verbatim in Polarion and never match Lucene; **before forwarding any write, `_guard.py:guard_work_item_enums` / `guard_document_enums` fetches `getAvailableOptions` and raises `ValueError` listing the valid ids if the supplied value is unknown.** `type` is checked first so an invalid `change_type_to` raises before being reused as the scoping axis; with `change_type_to` set, status/severity/resolution are scoped by the target type. Cached per `(project, resource, field, type)`. Guards run on `dry_run=True` too so previews surface the same correction signal (so dry_run needs the validation endpoint reachable). The docstring rule "call `list_*_enum_options` first" is now belt-and-suspenders. Use `Literal[...]` only for closed sets stable across projects.
- **The guards are fail-closed.** If a validation GET errors after the client's 429/5xx backoff, the guard logs a warning and raises `RuntimeError` — the write is blocked, never forwarded. A ghost write is invisible in Polarion's UI and unrecoverable, so an unverifiable write is refused rather than risked. The one lenient case is a *successful* empty option set (a field with no configured options), where the guard defers to Polarion. TTL is `_cache.py:_GUARD_TTL_SECONDS` (60s) — sized to bound the stale-accept window after a mid-session config change, distinct from `_DOCUMENT_LIST_TTL_SECONDS` (same value, freshness-driven). All caches are `TTLCache` instances; tests drive expiry by patching `tools._cache._now`.

### Document writes — the load-bearing rules

- **Body edits go through `homePageContent` PATCH, not `/parts`.** `update_document(home_page_content_html=...)` carries the full body HTML. `/parts` POST and `actions/moveToDocument` are wrappers that reject heading-type work items. The tool layer rejects empty `home_page_content_html` to stop accidental wipe orphaning every heading.
- **`<hN>` alone is safe; anchorless `<p>` / `<ul>` / `<ol>` / `<table>` / `<div>` / `<blockquote>` / `<pre>` are NOT** — PATCH returns 200, next `GET .../parts` returns 500. Each such block needs a unique non-empty `id=`. `create_document` runs `stamp_block_ids` automatically; `update_document` is raw HTML so the caller stamps ids, and the tool layer **hard-rejects** any anchorless block via `first_anchorless_block` (raises `ValueError` before the PATCH, on `dry_run` too). For body text, prefer `create_work_items` + `move_work_item_to_document`.
- **Injecting `<div id="polarion_wiki macro name=module-workitem;params=id=...">` does NOT set the work item's `module`** — leaves a half-attached state with `space_id=""` / `outline_number=""`. Only `move_work_item_to_document` updates `homePageContent`, sets `module`, and assigns `outline_number` atomically.
- **`module` cannot be set via `PATCH /workitems/{wi}`** — use the action pair `moveToDocument` / `moveFromDocument`. `create_work_item` does NOT expose `module` (would land in recycle bin); always create free-floating then move. `moveFromDocument` is not idempotent (400 on already-detached). `moveToDocument` auto-creates one outgoing link from the moved work item to its enclosing heading; role is project-config-dependent and silently removed on detach. Same-role collision with a subsequent `create_work_item_links` returns 201 but is not persisted (phantom success).

### Work item & comment write quirks

- **Link tools**: `create_work_item_links` is bulk; `update_work_item_link` is single-link; `delete_work_item_links` is silently idempotent (unmatched refs ignored, 204 regardless). All compose composite ids `<srcProj>/<srcWI>/<role>/<tgtProj>/<tgtWI>` from `WorkItemLinkRef` so the LLM never handles raw 5-segment strings. Both bulk tools cap at `MAX_BULK_ITEMS` (50) per request. The create POST is atomic: a Polarion-rejected link (e.g. a duplicate `(role, target)` → 409) rolls back the whole batch, so a 4xx means nothing committed — re-query `list_work_item_links(direction="forward")` before retrying. But Polarion validates neither target existence nor role, so a 201 can still leave a dangling link (nonexistent target) or a ghost link (unknown role stored verbatim, never matched by Lucene). `create_work_item_links` raises if a 2xx returns an id count differing from the number submitted; `delete_work_item_links` (idempotent 204, ids reconstructed client-side) pre-reads the source's outgoing links via `_guard.py:partition_delete_links` and splits the result into `deleted_link_ids` (matched) / `not_found_link_ids` (silent no-ops) — a no-op is reported, never raised (delete stays idempotent). The pre-read is fail-closed (unreachable backend → `RuntimeError` before any delete) and runs on `dry_run` too.
- **`update_work_item_link.suspect`** is tri-state on update (`None` = unchanged); distinct from `WorkItemLinkSpec.suspect: bool = False` on create. At least one of `suspect` / `revision` must be set or the tool raises `ValueError`.
- **`PATCH /workitems`** needs at least one `attributes` / `relationships` entry — Polarion 400s otherwise even when only `workflowAction` / `changeTypeTo` is set; `update_work_item` validates at the tool layer. `changeTypeTo` resets `status` to the new type's initial workflow state — re-apply in a follow-up if preservation matters.
- **`update_document_comment`** PATCH accepts only the full 4-segment id (`{project}/{space}/{document}/{commentId}`), and only on root comments — replies return 400 → `RuntimeError`. Tool layer doesn't pre-filter; docstring instructs callers to consult `list_document_comments` first. Resolving the root marks the entire thread resolved server-side.

## Testing

`pytest-asyncio` in `mode=auto`. **Tool tests** (`tests/tools/`) call tool functions directly with an injected `mock_client` (FastMCP 3.0's `@mcp.tool` returns the original function). **Client tests** (`tests/core/test_client.py`) use `respx`. Shared fixtures live in `tests/conftest.py`; pass `write_delay=0` for real `PolarionClient` instances. Pydantic `Field` constraints bypass FastMCP's JSON Schema on direct calls — verify via `TypeAdapter` reconstruction (see `TestCreateWorkItemFieldValidation`).

**Transport tests** (`tests/test_mcp_transport.py`) drive the server through `fastmcp.Client(mcp)` in-memory transport so registration → JSON Schema → lifespan → `get_client(ctx)` → real `PolarionClient` → mocked HTTP runs end to end. Adding a new `@mcp.tool` requires updating `EXPECTED_TOOL_NAMES` — that is the forcing function. The fixture monkeypatches `_WRITE_DELAY_SECONDS` because the lifespan constructs `PolarionClient` itself.

## Evals — Tier-1 deploy gate

`evals/` (separate `evals` dependency group, not shipped in the wheel) drives a real LLM agent through the in-memory MCP server against a mocked Polarion (`harness/fake_polarion.py` + respx) and deterministically asserts it never took a forbidden/footgun action — **no LLM judge**, every verdict is a pure function of the recorded tool-call trajectory. Hard gate ahead of the PyPI publish jobs in `.github/workflows/publish.yml`; a single forbidden action blocks the release. Each case must pass at `min_pass_rate=1.0` (zero tolerance). Switch model via `EVAL_MODEL` (CI default `openai/gpt-4o-mini`, needs `OPENAI_API_KEY`; local via `ollama_chat/...`). Adding a case: register a pure check in `evals/evaluators/checks.py::REGISTRY`, add a `Case` to `cases/tier1_prohibitions.py`, and **phrase the task neutrally** — stating the rule tests the prompt instead of the tool docstrings (the only guard). Full detail in [evals/README.md](evals/README.md).

## Repo Conventions

Full rules in [.github/CONTRIBUTING.md](.github/CONTRIBUTING.md). Quick reference:

- **Branches**: `<type>/<short-kebab-summary>` off `main`. Types: `feature | fix | refactor | docs | chore | ci`. One topic per branch.
- **Commits**: `type(scope): summary` ≤50 chars, lowercase imperative, no period. Types: `feat | fix | docs | refactor | perf | test | ci | chore`. Scopes: `tool | server | transport | config | deps | utils | model | project | meta | git`. Body: blank line + **exactly 2 bullets** (motivation, then change), each ≤120 chars, no `Why:` / `What:` prefixes. `.githooks/commit-msg` validates (enable once: `git config core.hooksPath .githooks`); PR-title budget on squash merge is 50 − ` (#NNN)`.
- **PR Type of Change checklist** ([.github/PULL_REQUEST_TEMPLATE.md](.github/PULL_REQUEST_TEMPLATE.md)): flip `[ ]` → `[x]`; don't delete unchecked options.
- **Squash merge only.** The squash commit follows the standard commit-message format above (subject + 2-bullet body). NEVER pass `--subject` to `gh pr merge` — let the PR title (already length-budgeted for `(#NNN)`) become the subject verbatim.
- **Force push** on feature branches only after explicit user authorization; never to `main`.
- **Claude PR hooks** ([.claude/hooks/](.claude/hooks/), wired via `.claude/settings.json` `PreToolUse` on `Bash`): `validate-pr-body.py` checks `gh pr/issue create|edit|comment` (and `gh api` PR/issue) bodies are English-only and preserve template checkboxes; `validate-pr-merge.py` guards `gh pr merge` — squash-only, no `--subject`, explicit 2-bullet conventional-format `--body`, Claude co-author trailer. They run as harness hooks (exit 2 blocks the call), not CI — covered by `tests/test_claude_hooks.py`.
