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

- **`core/`** — `client.py` (async httpx wrapper, 429/5xx backoff, post-mutation delay, maps to `PolarionError` / `PolarionAuthError` / `PolarionNotFoundError`), `config.py` (Pydantic settings: `POLARION_URL`, `POLARION_TOKEN`, `POLARION_VERIFY_SSL`), `logging.py` (stderr-only, called from server lifespan), `exceptions.py`. Every module: `logging.getLogger("mcp_server_polarion.<module>")`.
- **`tools/`** — `read.py` (12 read tools), `write.py` (11 write tools, each with its `_build_*_payload` helper as the unit-testable seam), `_helpers.py` (sparse-fieldset constants, JSON:API extractors, pagination, custom-field merge).
- **`utils/html.py`** — Markdown ↔ HTML (markdownify + BeautifulSoup4 sanitization) plus `stamp_block_ids` for write-side anchor injection.
- **`models.py`** — Pydantic v2. `PaginatedResult[T]` wraps all list responses.
- **`server.py`** — FastMCP instance with lifespan that opens/closes `PolarionClient`.

## Non-Negotiable Rules

- **NEVER `print()`** — stdout is reserved for MCP JSON-RPC; log to stderr.
- **NEVER `typing.Any`** — use concrete types or `object`.
- All functions: full type annotations + `from __future__ import annotations`. All tool functions: `async def` returning a Pydantic model.
- **Body fields are asymmetric by tool purpose**:
  - **Round-trip pair** (lossless): `get_*(include_*_html=True)` returns raw Polarion HTML; matching `update_*(*_html=...)` accepts it verbatim — no sanitization, no Markdown conversion. XSS filtering is delegated to Polarion's renderer.
  - **Greenfield create** (Markdown): `create_work_item(description=...)` and `create_document(home_page_content=...)` run through `markdown_to_html` + `sanitize_html`. After creation, edits switch to raw-HTML round-trip — the two formats never mix.
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
- **Custom fields inline under `attributes`.** No `customFields` container; `customFields.@all` tokens dropped. Server fetches `fields[*]=@all` and splits via `STANDARD_*_ATTRIBUTES` allowlists in `_helpers.py`. Polarion does NOT validate custom-field IDs — unknown keys persist as silent ghosts; always take keys from a prior read. Wrong-type values DO get rejected (400). **mcp-server guards** `update_work_item.custom_fields` via `tools/_enum_guard.py:guard_update_custom_field_keys`: keys must appear in a session-cached set populated by `get_work_item` (or a one-shot inline GET on cache miss). `create_work_item.custom_fields` cannot be schema-validated (no project-config endpoint) and falls back to a stderr warning. 60s TTL; soft-fail on `PolarionError`.
- **Server-side enum validation is absent in Polarion**, so the mcp-server tool layer enforces it. Unknown `type` / `status` / `severity` / `priority` ids would persist verbatim in Polarion and never match Lucene; **before forwarding any write, `tools/_enum_guard.py:guard_work_item_enums` / `guard_document_enums` fetches `getAvailableOptions` and raises `ValueError` listing the valid ids if the supplied value is unknown.** Cached per `(project, resource, field, type)` for 60s; soft-fail on `PolarionError` (logged warning, write proceeds). Guards run on `dry_run=True` too so previews surface the same correction signal. The docstring rule "call `list_*_enum_options` first" is now belt-and-suspenders rather than load-bearing. Use `Literal[...]` only for closed sets stable across projects.

### Document writes — the load-bearing rules

- **Body edits go through `homePageContent` PATCH, not `/parts`.** `update_document(home_page_content_html=...)` carries the full body HTML. `/parts` POST and `actions/moveToDocument` are wrappers that reject heading-type work items. The tool layer rejects empty `home_page_content_html` to stop accidental wipe orphaning every heading.
- **`<hN>` alone is safe; anchorless `<p>` / `<ul>` / `<ol>` / `<table>` / `<div>` / `<blockquote>` / `<pre>` are NOT** — PATCH returns 200, next `GET .../parts` returns 500. Each such block needs a unique non-empty `id=`. `create_document` runs `stamp_block_ids` automatically; `update_document` is raw HTML so the caller stamps ids. For body text, prefer `create_work_item` + `move_work_item_to_document`.
- **Injecting `<div id="polarion_wiki macro name=module-workitem;params=id=...">` does NOT set the work item's `module`** — leaves a half-attached state with `space_id=""` / `outline_number=""`. Only `move_work_item_to_document` updates `homePageContent`, sets `module`, and assigns `outline_number` atomically.
- **`module` cannot be set via `PATCH /workitems/{wi}`** — use the action pair `moveToDocument` / `moveFromDocument`. `create_work_item` does NOT expose `module` (would land in recycle bin); always create free-floating then move. `moveFromDocument` is not idempotent (400 on already-detached). `moveToDocument` auto-creates one outgoing link from the moved work item to its enclosing heading; role is project-config-dependent and silently removed on detach. Same-role collision with a subsequent `create_work_item_links` returns 201 but is not persisted (phantom success).

### Work item & comment write quirks

- **Link tools**: `create_work_item_links` is bulk; `update_work_item_links` is single-link; `delete_work_item_links` is silently idempotent (unmatched refs ignored, 204 regardless). All compose composite ids `<srcProj>/<srcWI>/<role>/<tgtProj>/<tgtWI>` from `WorkItemLinkRef` so the LLM never handles raw 5-segment strings. Mixed-success on partial-fail is uncharacterised — on 4xx, re-query `list_work_item_links(direction="forward")` before retrying.
- **`update_work_item_links.suspect`** is tri-state on update (`None` = unchanged); distinct from `WorkItemLinkSpec.suspect: bool = False` on create. At least one of `suspect` / `revision` must be set or the tool raises `ValueError`.
- **`PATCH /workitems`** needs at least one `attributes` / `relationships` entry — Polarion 400s otherwise even when only `workflowAction` / `changeTypeTo` is set; `update_work_item` validates at the tool layer. `changeTypeTo` resets `status` to the new type's initial workflow state — re-apply in a follow-up if preservation matters.
- **`update_document_comment`** PATCH accepts only the full 4-segment id (`{project}/{space}/{document}/{commentId}`), and only on root comments — replies return 400 → `RuntimeError`. Tool layer doesn't pre-filter; docstring instructs callers to consult `list_document_comments` first. Resolving the root marks the entire thread resolved server-side.

## Testing

`pytest-asyncio` in `mode=auto`. **Tool tests** (`tests/tools/`) call tool functions directly with an injected `mock_client` (FastMCP 3.0's `@mcp.tool` returns the original function). **Client tests** (`tests/core/test_client.py`) use `respx`. Shared fixtures live in `tests/conftest.py`; pass `write_delay=0` for real `PolarionClient` instances. Pydantic `Field` constraints bypass FastMCP's JSON Schema on direct calls — verify via `TypeAdapter` reconstruction (see `TestCreateWorkItemFieldValidation`).

**Transport tests** (`tests/test_mcp_transport.py`) drive the server through `fastmcp.Client(mcp)` in-memory transport so registration → JSON Schema → lifespan → `get_client(ctx)` → real `PolarionClient` → mocked HTTP runs end to end. Adding a new `@mcp.tool` requires updating `EXPECTED_TOOL_NAMES` — that is the forcing function. The fixture monkeypatches `_WRITE_DELAY_SECONDS` because the lifespan constructs `PolarionClient` itself.

## Repo Conventions

Full rules in [.github/CONTRIBUTING.md](.github/CONTRIBUTING.md). Quick reference:

- **Branches**: `<type>/<short-kebab-summary>` off `main`. Types: `feature | fix | refactor | docs | chore | ci`. One topic per branch.
- **Commits**: `type(scope): summary` ≤50 chars, lowercase imperative, no period. Types: `feat | fix | docs | refactor | perf | test | ci | chore`. Scopes: `tool | server | transport | config | deps | utils | model | project | meta | git`. Body: blank line + **exactly 2 bullets** (motivation, then change), each ≤120 chars, no `Why:` / `What:` prefixes. `.githooks/commit-msg` validates (enable once: `git config core.hooksPath .githooks`); PR-title budget on squash merge is 50 − ` (#NNN)`.
- **PR Type of Change checklist** ([.github/PULL_REQUEST_TEMPLATE.md](.github/PULL_REQUEST_TEMPLATE.md)): flip `[ ]` → `[x]`; don't delete unchecked options.
- **Squash merge only.** The squash commit follows the standard commit-message format above (subject + 2-bullet body). NEVER pass `--subject` to `gh pr merge` — let the PR title (already length-budgeted for `(#NNN)`) become the subject verbatim.
- **Force push** on feature branches only after explicit user authorization; never to `main`.
