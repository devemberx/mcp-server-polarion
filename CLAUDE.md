# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

MCP server providing AI assistants (Claude, Cursor, Copilot) with read and write access to Polarion ALM via the Model Context Protocol. Built on FastMCP 3.0 with a strict async-only, fully-typed Python codebase.

## Commands

```bash
uv sync --dev                                            # install deps
uv run pytest                                            # all tests
uv run pytest tests/tools/test_read.py::TestGetWorkItem  # single class
uv run pytest -k test_page_size_rejects_above_max        # single test by name substring
uv run ruff check . && uv run ruff format . && uv run mypy src/  # lint + format + types
uv run mcp-server-polarion                               # run server (stdio)
```

CI runs: `ruff check` → `ruff format --check` → `mypy` → `pytest`.

## Architecture

- **`core/`** — `client.py` (async httpx wrapper, 429/5xx backoff, post-mutation delay, maps responses to `PolarionError` / `PolarionAuthError` / `PolarionNotFoundError`), `config.py` (Pydantic settings: `POLARION_URL`, `POLARION_TOKEN`, `POLARION_VERIFY_SSL`), `logging.py` (stderr-only configuration; called from server lifespan, never from tool code), `exceptions.py`. Every module obtains its logger via `logging.getLogger("mcp_server_polarion.<module>")`.
- **`tools/`** — `read.py` (8 read tools incl. `read_document` for flowing Markdown), `write.py` (4 write tools, each with its `_build_*_payload` helper), `_helpers.py` (sparse-fieldset constants, JSON:API extractors, pagination helpers, custom-field merge).
- **`utils/html.py`** — Markdown ↔ HTML (markdownify + BeautifulSoup4 sanitization).
- **`models.py`** — Pydantic v2 models. `PaginatedResult[T]` wraps all list responses.
- **`server.py`** — FastMCP instance with lifespan that opens/closes `PolarionClient`.

## Non-Negotiable Rules

- **NEVER `print()`** — stdout is reserved for MCP JSON-RPC; log to stderr.
- **NEVER `typing.Any`** — use concrete types or `object`.
- All functions: full type annotations + `from __future__ import annotations`. All tool functions: `async def`. All tool returns: Pydantic models, never raw `dict`.
- **Body fields are asymmetric by tool purpose**:
  - **Round-trip pair** (lossless): `get_*(include_*_html=True)` returns raw Polarion HTML; matching `update_*(*_html=...)` accepts the same shape verbatim — no sanitization, no Markdown conversion. XSS filtering is delegated to Polarion's renderer, so never route untrusted input through these parameters.
  - **Greenfield create** (Markdown): `create_work_item(description=...)` accepts Markdown; runs through `markdown_to_html` + `sanitize_html` before storage.
  - **Synthesis paths** (Markdown): `read_document` / `get_document_parts` convert HTML→Markdown via `html_to_markdown()`. Output is READ-ONLY — feeding it back to a write tool loses Polarion-specific markup.
- **Write payloads** — skip `None`/empty values; Polarion may interpret empty as "clear default". JSON:API resource POSTs (`/workitems`) wrap in `{"data": [...]}`; **action endpoints** (`.../actions/<name>`) take a flat object — do NOT wrap. The `cast(dict[str, object], payload)` shim at the `client.post(json=...)` call site is intentional (dict invariance vs `JsonValue`).
- Every list tool must support `page_size` (max 100) and `page_number`; return `PaginatedResult[T]` with `has_more`.
- **Every write tool** must support `dry_run: bool = False`. When True, build and return the JSON:API payload in the result model (`dry_run=True`, `payload=…`) without hitting Polarion. The matching `_build_*_payload` helper is the unit-testable seam.
- Tool docstrings are the LLM's manual — Google-style with Args/Returns/Raises. Keep return-field bullets in sync with the Pydantic model.
- **Error handling**: Map domain exceptions at the tool layer — `PolarionNotFoundError` → `ValueError`, `PolarionAuthError` → `PermissionError`, `PolarionError` → `RuntimeError`.

## Comment & Docstring Style

Applies to ALL comments / docstrings (tool docstrings, helpers, inline, CLAUDE.md).

- **Field descriptions stay one line.** Longer explanations belong in the surrounding function docstring; skip the description entirely when field name + type say everything. The cross-model invariant in `tests/test_models.py::test_field_descriptions_are_non_empty_when_set` only requires non-empty *when set*.
- **No `WARNING:` / `FOOTGUN:` / `NOTE:` prefix upgrades.** State the same fact in a single plain sentence — the prefix adds visual weight without information.
- **No dev-narrative.** Phrases like "verified via smoke test", "we tried X then switched to Y", or "as of vN" belong in commit messages and PR descriptions, not in source comments. Use the declarative form in code.
- **No banner-divider comments.** `# ----------------` / `# Section name` / `# === Section ===` sandwiches are visual noise; function and class headers already provide structure.
- **CLAUDE.md is dev-only.** Other MCP hosts (Cursor / Copilot / generic FastMCP clients) never load this file. Anything an MCP-user LLM needs to know (round-trip pairs, replace-all semantics, body-edit pitfalls, etc.) must live inside the `@mcp.tool` docstring — even if it duplicates CLAUDE.md content.
- **Module docstrings explain *why* the module exists.** Specific timing numbers, observed file sizes, refactor history, and other implementation detail belong in inline comments next to the thing they constrain.

## Polarion API & Gotchas

- **Endpoints (JSON:API v1)**: `/projects`, `/projects/{p}/workitems[?query=...]`, `/projects/{p}/workitems/{wi}[/linkedworkitems]`, `/projects/{p}/spaces/{s}/documents/{d}[/parts]`.
- **HTML payloads**: stored as `{"type": "text/html", "value": "..."}`.
- **Linked WI IDs**: 5 segments — derive the target via `relationships.workItem.data.id`, never by parsing.
- **Module IDs**: 3 segments and document names may contain `/`, so always use `split_module_id` (splits on the first two slashes only).
- **Lucene**: trailing wildcards (`title:SRS*`) work; leading wildcards return HTTP 400. The `module` field is not indexed.
- **Server limits**: ≤3 API calls/second, no concurrent requests; `PolarionClient` retries 429/5xx with backoff but does NOT serialize client-side.

### JSON:API quirks

**Sparse fieldset filters both attributes AND relationships.** `fields[workitems]=title,type,status` removes *all* `relationships` from the response, not just other attributes. List relationship names explicitly (see `WI_LIST_FIELDS`); forgetting silently empties derived fields like `space_id` / `document_name` / `assignee_ids` / `author_id`.

**To-many relationships need `include=`.** Polarion does not inline `data` for to-many relationships (e.g. `assignee`) — only `links` come back. Pass `"include": "assignee"` to populate `relationships.assignee.data`. To-one relationships (`module`, `author`, `project`) are inlined without `include`.

**`/backlinkedworkitems` is not supported on this server.** `get_linked_work_items` returns one direction per call: forward links use `/projects/{p}/workitems/{wi}/linkedworkitems`; back links fall back to a `query=linkedWorkItems:{wi}` search that does not expose the originating role, so back items return with `role=None`. Call twice when both directions are needed.

**Server-side enum validation is lenient.** Polarion does NOT strictly validate `type` / `status` / `priority` / `severity` on create — unrecognised values are silently coerced (`priority="not_a_number"` → project default) or stored verbatim (`type="not_a_real_type"` → ghost type). Use `Literal[...]` on Pydantic Fields for closed sets that matter.

### Custom fields surface inline under `attributes`

This server inlines project-defined customs as top-level keys in `attributes` — no `customFields` container, and `customFields.@all` / `@custom` / `@additional` tokens are silently dropped. The MCP server fetches with `fields[*]=@all` and filters out canonical attributes via the `STANDARD_WORKITEM_ATTRS` / `STANDARD_DOCUMENT_ATTRS` allowlists in `_helpers.py`; anything outside the allowlist is exposed on `*.custom_fields`. Values are kept raw (primitives or `{type: 'text/html', value: ...}` dicts) so the shape round-trips. A future Polarion release adding new standard attributes would misclassify them as custom until the allowlist is updated.

**Write side**: `create_work_item` / `update_work_item` / `update_document` accept `custom_fields: dict[str, object]` mirroring the read shape; `merge_custom_fields` inlines entries and raises `ValueError` on collision with a standard attribute. `None` inside the dict is skipped (explicit clearing unsupported). **Ghost customs**: Polarion does NOT validate custom-field IDs server-side, so an unknown key is silently stored and reappears on every subsequent `get_*` indistinguishable from a real one — always take keys from a prior read response. Wrong-type values DO get rejected (HTTP 400).

### Document content search — pick the right tool

Polarion Lucene does NOT index `description`, so `list_work_items` cannot filter by body text. Route content searches by scope:

| Goal | Tool | Notes |
|---|---|---|
| Find WIs by metadata (title/type/status) | `list_work_items` | Lucene query against `title`, `type`, `status`, etc. — not `description`. |
| Read the document end-to-end | `read_document` | Renders interleaved headings + embedded WI bodies + prose as flowing Markdown. Paginated by part (default 100/page). The canonical "let me read this doc" tool. |
| Get document metadata only | `get_document` | Title/type/status. `include_homepage_content_html=True` returns the `homePageContent` as **raw Polarion HTML** in `content_html` for round-trip editing via `update_document(home_page_content_html=...)`. Incomplete for end-to-end reading (heading text + embedded WI bodies live in separate work items, not in `homePageContent`) — use `read_document` for that. |
| Search inside a document with structural metadata | `get_document_parts` | Each `workitem` part carries `description` as Markdown — **no follow-up `get_work_item` call needed**. Embedded WIs are fetched with the tight `WI_PART_FIELDS` sparse set (`title,type,status,description,outlineNumber`), not `@all`, to keep payloads small. `outlineNumber` lets `DocumentPart.outline_number` carry the hierarchical position (e.g. `'1.2.3'`) so `read_document` can prefix heading titles with it. Use when you need part IDs (for `move_work_item_to_document`), heading levels, or per-WI status/type. For plain reading, prefer `read_document`. |

### Document body writes go through `homePageContent` PATCH (NOT `/parts`)

Body edits use `PATCH /projects/{p}/spaces/{s}/documents/{d}` with `attributes.homePageContent.value` carrying the full body HTML — exposed at the tool layer as `update_document(home_page_content_html=...)`. The companion endpoints `/parts` POST and `actions/moveToDocument` are convenience wrappers that internally edit `homePageContent`; both reject heading-type WIs ("Cannot move headings" / "Creation of heading Parts is not supported"). Setting `relationships.module` directly on a WI links ownership only — it does NOT create a body part. `PATCH /workitems/{wi}` IS allowed on heading WIs (`update_work_item` can edit a heading's attributes); the lock is specific to body-part creation/relocation. The tool layer rejects `home_page_content_html=""` to stop an accidental wipe from orphaning every heading. Removing an `<hN>` later removes the part but leaves the heading WI as an orphan (module-linked, no `outline_number`).

**Two `update_document` body-edit pitfalls**:

1. **Plain `<hN>` is safe; ID-anchor-less `<p>` IS NOT.** Appending `<h3>Heading</h3>` alone is fine — Polarion auto-creates a heading WI with `module` and `outline_number` set, and the new `heading_MCPT-N` part renders correctly. But adding even one anchorless `<p>Body</p>` in the same PATCH lets the PATCH return 200 while the next `GET .../parts` returns HTTP 500. Polarion's stored paragraphs all carry `id="polarion_..."` anchors; raw `<p>` blocks break server-side part derivation. For body text, create a new work item and attach via `create_work_item` + `move_work_item_to_document`.

2. **Injecting `<div id="polarion_wiki macro name=module-workitem;params=id=NEW-WI">` does NOT set the WI's `module` relationship.** The new part appears in `get_document_parts` as `workitem_<NEW-WI>`, but `get_work_item(<NEW-WI>)` reports `space_id=""`, `document_name=""`, `outline_number=""` — an inconsistent half-attached state. WI body parts must be added via `move_work_item_to_document`, which is the only path that updates `homePageContent`, sets `module`, and assigns `outline_number` atomically.

### `PATCH /workitems/{wi}` quirks

PATCH bodies need at least one `attributes` / `relationships` entry — Polarion 400s otherwise even when only `workflowAction` / `changeTypeTo` is set; `update_work_item` validates this at the tool layer. Setting `changeTypeTo` also resets `status` to the new type's initial workflow state (e.g. `task[status=approved]` → `defect[status=open]`); callers wanting to preserve status must re-apply it in a follow-up call.

## Testing

`pytest-asyncio` in `mode=auto`. **Tool tests** (`tests/tools/`) call tool functions directly with an injected `mock_client` (FastMCP 3.0's `@mcp.tool` returns the original function unchanged). **Client tests** (`tests/core/test_client.py`) use `respx` to mock `httpx`. Shared fixtures live in `tests/conftest.py`; pass `write_delay=0` for real `PolarionClient` instances. Pydantic `Field` constraints (`min_length` / `ge` / `le`) bypass FastMCP's JSON Schema on direct calls — verify them by reconstructing a `TypeAdapter` from `Annotated[type, FieldInfo]` (see `TestCreateWorkItemFieldValidation`).

## Repo Conventions

Branch strategy, full commit rules, and PR workflow are in [.github/CONTRIBUTING.md](.github/CONTRIBUTING.md). Quick reference for commit/PR generation:

- **Branches**: `<type>/<short-kebab-summary>` off latest `main` (e.g. `feature/read-fidelity`). Types: `feature | fix | refactor | docs | chore | ci`. One topic per branch.
- **Commits**:
  - Subject: `type(scope): summary` — lowercase imperative, ≤50 chars, no period. Types: `feat | fix | docs | refactor | perf | test | ci | chore`. Scopes: `tool | server | transport | config | deps | utils | model | project | meta | git`.
  - Body: blank line + **exactly 2 bullets** (motivation, then change) — no `Why:` / `What:` prefixes. Each bullet ≤120 chars.
- **PR Type of Change checklist** ([.github/PULL_REQUEST_TEMPLATE.md](.github/PULL_REQUEST_TEMPLATE.md)): flip `[ ]` → `[x]` for matching items; do not delete unchecked options.
- **Force push** allowed on feature branches only after explicit user authorization. Never force-push to `main`.
