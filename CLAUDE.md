# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

MCP server providing AI assistants (Claude, Cursor, Copilot) with read and write access to Polarion ALM via the Model Context Protocol. Built on FastMCP 3.0 with a strict async-only, fully-typed Python codebase.

## Commands

```bash
uv sync --dev                                            # install deps
uv run pytest                                            # all tests
uv run pytest tests/tools/test_read.py::TestGetWorkItem  # single class
uv run ruff check . && uv run ruff format . && uv run mypy src/  # lint + format + types
uv run mcp-server-polarion                               # run server (stdio)
```

CI runs: `ruff check` → `ruff format --check` → `mypy` → `pytest`.

## Architecture

- **`core/`** — `client.py` (async httpx wrapper, 429/5xx backoff, post-mutation delay, maps responses to `PolarionError` / `PolarionAuthError` / `PolarionNotFoundError`), `config.py` (Pydantic settings: `POLARION_URL`, `POLARION_TOKEN`), `exceptions.py`.
- **`tools/`** — `read.py` (8 read tools incl. `read_document` for flowing Markdown), `write.py` (4 write tools, each with its `_build_*_payload` helper), `_helpers.py` (sparse-fieldset constants, JSON:API extractors, pagination helpers, custom-field merge).
- **`utils/html.py`** — Markdown ↔ HTML (markdownify + BeautifulSoup4 sanitization).
- **`models.py`** — Pydantic v2 models. `PaginatedResult[T]` wraps all list responses.
- **`server.py`** — FastMCP instance with lifespan that opens/closes `PolarionClient`.

## Non-Negotiable Rules

- **NEVER `print()`** — stdout is reserved for MCP JSON-RPC; log to stderr.
- **NEVER `typing.Any`** — use concrete types or `object`.
- All functions: full type annotations + `from __future__ import annotations`. All tool functions: `async def`. All tool returns: Pydantic models, never raw `dict`.
- **Body fields are asymmetric by tool purpose**:
  - **Round-trip pair** (lossless): `get_work_item(..., include_description_html=True)` / `get_document(..., include_homepage_content_html=True)` return **raw Polarion HTML** in `description_html` / `content_html`. `update_work_item(description_html=...)` / `update_document(home_page_content_html=...)` accept the same shape and send it verbatim — NO sanitization, NO Markdown conversion. Sanitizing would strip Polarion-specific spans / `data-*` attrs and defeat the round-trip; never pass untrusted input through these update parameters. **Security boundary**: XSS / script-tag filtering is delegated to Polarion's own rendering layer — this MCP server is NOT a defense-in-depth boundary, and a prompt-injected LLM that gets HTML routed through these parameters can plant stored payloads that fire in any client (including Polarion's web UI) that re-renders the value.
  - **Greenfield create** (Markdown): `create_work_item(description=...)` accepts Markdown, converts via `markdown_to_html` + `sanitize_html` (allowed tags: p, br, b, i, u, strong, em, ul, ol, li, h1-h4, table+children, a, span, div, pre, code; schemes: http, https, mailto).
  - **Synthesis paths** (Markdown): `read_document` and `get_document_parts` convert HTML→Markdown via `html_to_markdown()` for LLM consumption. Output is READ-ONLY — feeding it back to a write tool will lose Polarion-specific markup.
  - Same precedent already applies to `custom_fields` rich-text values (`{type: 'text/html', value: ...}` passed through verbatim on both read and write).
- **Write payloads** — skip `None`/empty values; Polarion may interpret empty as "clear default". JSON:API resource POSTs (`/workitems`) wrap in `{"data": [...]}`; **action endpoints** (`.../actions/<name>`) take a flat object — do NOT wrap. The `cast(dict[str, object], payload)` shim at the `client.post(json=...)` call site is intentional (dict invariance vs `JsonValue`).
- Every list tool must support `page_size` (max 100) and `page_number`; return `PaginatedResult[T]` with `has_more`.
- Tool docstrings are the LLM's manual — Google-style with Args/Returns/Raises. Keep return-field bullets in sync with the Pydantic model.

## Error Handling Pattern

Map domain exceptions at the tool layer: `PolarionNotFoundError` → `ValueError`, `PolarionAuthError` → `PermissionError`, `PolarionError` → `RuntimeError`.

## Polarion API & Gotchas

JSON:API v1. Key paths: `/projects`, `/projects/{p}/workitems[?query=...]`, `/projects/{p}/workitems/{wi}[/linkedworkitems]`, `/projects/{p}/spaces/{s}/documents/{d}[/parts]`.

**HTML payloads**: stored as `{"type": "text/html", "value": "..."}`. **Linked WI IDs** have 5 segments `{projectId}/{sourceWi}/{role}/{targetProject}/{targetWi}` — derive the target via `relationships.workItem.data.id`, never by parsing. **Module IDs** have 3 segments `{projectId}/{spaceId}/{documentName}`; document names may contain `/`, use `split_module_id` (splits on first two slashes only).

**Lucene**: trailing wildcards (`title:SRS*`) work; **leading wildcards** (`*SRS*`) → HTTP 400. The `module` field is **not indexed** and cannot be queried.

### Sparse fieldset filters BOTH attributes AND relationships

`fields[workitems]=title,type,status` removes **all** `relationships` from the response, not just other attributes. To receive a relationship, list its name explicitly (see `WI_LIST_FIELDS`). Forgetting this silently empties derived fields like `space_id`/`document_name`/`assignee_ids`/`author_id`.

### Custom fields surface inline under `attributes`

This server inlines project-defined customs as top-level keys in `attributes` (e.g. `attributes.riskLevel`) — there is no `customFields` container, and `customFields.@all` / `@custom` / `@additional` tokens are silently dropped. The MCP server fetches with `fields[*]=@all`, then filters out canonical Polarion attributes via the `STANDARD_WORKITEM_ATTRS` / `STANDARD_DOCUMENT_ATTRS` allowlists in `_helpers.py`. Anything outside the allowlist is reported on `*.custom_fields`. Values are heterogeneous: primitives or `{type: 'text/html', value: ...}` dicts, kept raw so the shape round-trips. A future Polarion release adding new standard attributes would misclassify them as custom until the allowlist is updated.

**Write side**: `create_work_item` / `update_work_item` / `update_document` accept `custom_fields: dict[str, object] | None` mirroring the read shape. `merge_custom_fields` inlines entries into `attributes` and raises `ValueError` on a collision with a standard attribute (so an explicit `title=` is never silently shadowed). Rich-text values must be `{type: 'text/html', value: ...}` dicts. `None` inside the dict is skipped — explicit clearing is not supported by the helper. On `update_*`, omitted keys preserve their server-side value (JSON:API omit-preserve). `update_work_item`'s post-PATCH GET reuses `@all` so the returned `current.custom_fields` is fresh.

**Ghost custom fields**: Polarion does NOT validate custom-field IDs server-side — an unknown key like `definitely_not_a_real_field_xyz` is stored as an inline attribute and reappears on every subsequent `get_*` indistinguishable from a real custom. Wrong-type values DO get rejected with HTTP 400. There is no schema-discovery endpoint, so always take custom-field keys from a prior read response. Clearing a ghost requires sending the key with explicit `null`, which the write tools skip — use a direct `PolarionClient.patch` if cleanup is needed.

### To-many relationships need `include=`

Polarion does not inline `data` for to-many relationships (e.g. `assignee`) — only `links` come back. Pass `"include": "assignee"` to populate `relationships.assignee.data`. To-one relationships (`module`, `author`, `project`) are inlined without `include`.

### `/backlinkedworkitems` is NOT supported on this server

`get_linked_work_items` accepts a `direction: Literal["forward", "back"]` parameter and returns `PaginatedResult[LinkedWorkItemSummary]` for one direction per call. Forward links are read from `/projects/{p}/workitems/{wi}/linkedworkitems`; back links fall back to a `query=linkedWorkItems:{wi}` search, which loses role information — back-direction items return with `role=None` (intentional — do not reintroduce the legacy `"backlink"` placeholder; `None` is an explicit "unknown" signal). Callers needing both directions issue two calls.

### Document content search — pick the right tool

Polarion Lucene does NOT index `description`, so `list_work_items` cannot filter by body text. Route content searches by scope:

| Goal | Tool | Notes |
|---|---|---|
| Find WIs by metadata (title/type/status) | `list_work_items` | Lucene query against `title`, `type`, `status`, etc. — not `description`. |
| Read the document end-to-end | `read_document` | Renders interleaved headings + embedded WI bodies + prose as flowing Markdown. Paginated by part (default 100/page). The canonical "let me read this doc" tool. |
| Get document metadata only | `get_document` | Title/type/status. `include_homepage_content_html=True` returns the `homePageContent` as **raw Polarion HTML** in `content_html` for round-trip editing via `update_document(home_page_content_html=...)`. Incomplete for end-to-end reading (heading text + embedded WI bodies live in separate work items, not in `homePageContent`) — use `read_document` for that. |
| Search inside a document with structural metadata | `get_document_parts` | Each `workitem` part carries `description` as Markdown — **no follow-up `get_work_item` call needed**. Embedded WIs are fetched with the tight `WI_PART_FIELDS` sparse set (`title,type,status,description`), not `@all`, to keep payloads small. Use when you need part IDs (for `move_work_item_to_document`), heading levels, or per-WI status/type. For plain reading, prefer `read_document`. |

### Server-side validation is lenient on enum-like fields

Polarion does NOT strictly validate `type`, `status`, `priority`, or `severity` on create. Unrecognised values are silently coerced (`priority="not_a_number"` → project default) or stored verbatim (`type="not_a_real_type"` → ghost type). Use `Literal[...]` on Pydantic Field for closed sets where validation matters; otherwise add a WARNING to the field description.

### Document body writes go through `homePageContent` PATCH (NOT `/parts`)

Body edits use `PATCH /projects/{p}/spaces/{s}/documents/{d}` with `attributes.homePageContent.value` carrying the full body HTML — exposed at the tool layer as `update_document(home_page_content_html=...)`. The companion endpoints `/parts` POST and `actions/moveToDocument` are convenience wrappers that internally edit `homePageContent`; both reject heading-type WIs ("Cannot move headings" / "Creation of heading Parts is not supported"). Setting `relationships.module` directly on a WI links ownership only — it does NOT create a body part. `PATCH /workitems/{wi}` IS allowed on heading WIs (`update_work_item` can edit a heading's attributes); the lock is specific to body-part creation/relocation. The tool layer rejects `home_page_content_html=""` to stop an accidental wipe from orphaning every heading. Removing an `<hN>` later removes the part but leaves the heading WI as an orphan (module-linked, no `outline_number`).

**Two `update_document` body-edit pitfalls**:

1. **Plain `<hN>` is safe; ID-anchor-less `<p>` IS NOT.** Appending `<h3>Heading</h3>` alone is fine — Polarion auto-creates a heading WI with `module` and `outline_number` set, and the new `heading_MCPT-N` part renders correctly. But adding even one anchorless `<p>Body</p>` in the same PATCH lets the PATCH return 200 while the next `GET .../parts` returns HTTP 500. Polarion's stored paragraphs all carry `id="polarion_..."` anchors; raw `<p>` blocks break server-side part derivation. For body text, create a new work item and attach via `create_work_item` + `move_work_item_to_document`.

2. **Injecting `<div id="polarion_wiki macro name=module-workitem;params=id=NEW-WI">` does NOT set the WI's `module` relationship.** The new part appears in `get_document_parts` as `workitem_<NEW-WI>`, but `get_work_item(<NEW-WI>)` reports `space_id=""`, `document_name=""`, `outline_number=""` — an inconsistent half-attached state. WI body parts must be added via `move_work_item_to_document`, which is the only path that updates `homePageContent`, sets `module`, and assigns `outline_number` atomically.

### `PATCH /workitems/{wi}` requires a non-empty body

Polarion rejects PATCH bodies that have neither `attributes` nor `relationships` ("At least one of the members is required: 'attributes, relationships'"), even when only the `workflowAction` / `changeTypeTo` query parameter is set. Action-only transitions must be paired with at least one body field. `update_work_item` validates this at the tool layer (raises `ValueError`) so the caller gets an actionable message instead of a Polarion 400.

### `changeTypeTo` resets the workflow status

Setting `changeTypeTo` on `update_work_item` resets the WI's `status` to the new type's initial workflow state (e.g. `task[status=approved]` → `defect[status=open]`). The docstring warns about this; callers that need to preserve the prior status must re-apply it in a follow-up `update_work_item` call.

## Server-Side Constraints

The deployed Polarion server enforces **≤3 API calls/second** and **no concurrent requests**. `PolarionClient` does NOT implement client-side rate limiting or request serialization — callers must respect these limits or rely on Polarion's HTTP 429 responses (the client retries with exponential backoff).

## Testing

`pytest-asyncio` in `mode=auto`. Two patterns coexist: **tool tests** (`tests/tools/`) call tool functions directly with a `mock_client` (`AsyncMock(spec=PolarionClient)`) injected via a `mock_ctx` — FastMCP 3.0's `@mcp.tool()` returns the original function unchanged, so direct invocation works; **client tests** (`tests/core/test_client.py`) use `respx` to mock the `httpx` transport. Shared fixtures live in `tests/conftest.py`. Pass `write_delay=0` when constructing a real `PolarionClient` in tests.

**Pydantic Field constraint tests**: direct function calls bypass FastMCP's JSON Schema gate, so `min_length=1` / `ge` / `le` aren't naturally exercised. Reconstruct a `TypeAdapter` from the parameter's `Annotated[type, FieldInfo]` (via `inspect.signature` + `get_type_hints`) and assert it rejects bad input at the schema layer — see `TestCreateWorkItemFieldValidation`.

## Repo Conventions

- **Commits**: `.vscode/git_commit_guide.md` — `type(scope): subject` (lowercase imperative ≤50 chars, no period) + blank line + exactly 2 bullets (Why + What), **each bullet a single line ≤~120 chars** — move longer rationale to the PR body. Types: `feat|fix|docs|refactor|perf|test|ci|chore`. Common scopes: `tool|server|transport|config|deps|utils|model|project|meta|git`.
- **PRs**: `.github/pull_request_template.md` — fill Summary, Type-of-Change, Changes, Testing (esp. `dry_run` for write tools), Golden Rule Compliance.
- **Force push** allowed on feature branches only after explicit user authorization. Never force-push to `main`.
