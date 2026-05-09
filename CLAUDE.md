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

- **`core/`** — `client.py` (async httpx wrapper, exponential backoff for 429/5xx, `write_delay` after each mutation, maps responses to `PolarionError`/`PolarionAuthError`/`PolarionNotFoundError`), `config.py` (Pydantic settings reading `POLARION_URL`/`POLARION_TOKEN`), `exceptions.py`.
- **`tools/`** — `read.py` (8 read tools, including `read_document` which renders a document's parts as a single flowing Markdown stream), `write.py` (4 write tools: `create_work_item`, `update_work_item`, `move_work_item_to_document`, `update_document`; each preceded by its `_build_*_payload` helper), `_helpers.py` (sparse-fieldset constants `WI_LIST_FIELDS`/`WI_DETAIL_FIELDS`, JSON:API extractors, pagination helpers, `build_work_item_summary_kwargs` keeps `WorkItemDetail` a strict superset of `WorkItemSummary`).
- **`utils/html.py`** — Markdown ↔ HTML conversion (markdownify + BeautifulSoup4 sanitization).
- **`models.py`** — Pydantic v2 models. `PaginatedResult[T]` wraps all list responses.
- **`server.py`** — FastMCP instance with lifespan that opens/closes `PolarionClient`.

## Non-Negotiable Rules

- **NEVER `print()`** — stdout is reserved for MCP JSON-RPC; log to stderr.
- **NEVER `typing.Any`** — use concrete types or `object`.
- All functions: full type annotations + `from __future__ import annotations`. All tool functions: `async def`. All tool returns: Pydantic models, never raw `dict`.
- **HTML on read**: `html_to_markdown()`. **HTML on write**: convert from Markdown + sanitize (allowed tags: p, br, b, i, u, strong, em, ul, ol, li, h1-h4, table+children, a, span, div, pre, code; schemes: http, https, mailto).
- **Write payloads** — skip `None`/empty values; Polarion may interpret empty as "clear default". JSON:API resource POSTs (`/workitems`) wrap in `{"data": [...]}`; **action endpoints** (`.../actions/<name>`) take a flat object — do NOT wrap. The `cast(dict[str, object], payload)` shim at the `client.post(json=...)` call site is intentional (dict invariance vs `JsonValue`).
- Every list tool must support `page_size` (max 100) and `page_number`; return `PaginatedResult[T]` with `has_more`.
- Tool docstrings are the LLM's manual — Google-style with Args/Returns/Raises. Keep return-field bullets in sync with the Pydantic model.

## Error Handling Pattern

Map domain exceptions at the tool layer: `PolarionNotFoundError` → `ValueError` (with actionable message), `PolarionAuthError` → `PermissionError`, `PolarionError` → `RuntimeError`.

## Polarion API & Gotchas

JSON:API v1. Key paths: `/projects`, `/projects/{p}/workitems[?query=...]`, `/projects/{p}/workitems/{wi}[/linkedworkitems]`, `/projects/{p}/spaces/{s}/documents/{d}[/parts]`.

**HTML payloads**: stored as `{"type": "text/html", "value": "..."}`. **Linked WI IDs** have 5 segments `{projectId}/{sourceWi}/{role}/{targetProject}/{targetWi}` — derive the target via `relationships.workItem.data.id`, never by parsing. **Module IDs** have 3 segments `{projectId}/{spaceId}/{documentName}`; document names may contain `/`, use `split_module_id` (splits on first two slashes only).

**Lucene**: trailing wildcards (`title:SRS*`) work; **leading wildcards** (`*SRS*`) → HTTP 400. The `module` field is **not indexed** and cannot be queried.

### Sparse fieldset filters BOTH attributes AND relationships

`fields[workitems]=title,type,status` removes **all** `relationships` from the response, not just other attributes. To receive a relationship, list its name explicitly (see `WI_LIST_FIELDS`). Forgetting this silently empties derived fields like `space_id`/`document_name`/`assignee_ids`/`author_id`.

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
| Get document metadata only | `get_document` | Title/type/status. `include_content=True` returns the raw `homePageContent` source (incomplete for reading — heading text and embedded WI bodies live in separate work items, not in `homePageContent`; use this option for round-trip editing of the source, not for reading). |
| Search inside a document with structural metadata | `get_document_parts` | Each `workitem` part carries `description` as Markdown — **no follow-up `get_work_item` call needed**. Use when you need part IDs (for `move_work_item_to_document`), heading levels, or per-WI status/type. For plain reading, prefer `read_document`. |

### Server-side validation is lenient on enum-like fields

Polarion does NOT strictly validate `type`, `status`, `priority`, or `severity` on create. Unrecognised values are silently coerced (`priority="not_a_number"` → project default) or stored verbatim (`type="not_a_real_type"` → ghost type). Use `Literal[...]` on Pydantic Field for closed sets where validation matters; otherwise add a WARNING to the field description.

### Document body writes go through `homePageContent` PATCH (NOT `/parts`)

The canonical document-body write path is `PATCH /projects/{p}/spaces/{s}/documents/{d}` with `attributes.homePageContent.value` carrying the full body HTML. Polarion parses that HTML on save and auto-creates heading WIs from inline `<h1>..<h4>`, sets each new WI's `module` relationship, computes `outline_number`, and derives the `parts` view. `/parts` POST and `actions/moveToDocument` are convenience wrappers that edit `homePageContent` internally and impose extra restrictions both reject heading-type WIs (`/parts`: "Creation of heading Parts is not supported" / "Cannot add external Work Item of type Heading"; `moveToDocument`: "Cannot move headings"). Setting `relationships.module` directly on a WI (e.g. on `create_work_item`) only links ownership — it does NOT add a body part. Verified via smoke test: appending `<h1>...</h1>` to `homePageContent` yields a new `heading_MCPT-N` part + heading WI; removing the `<h1>` later removes the part but leaves the heading WI as an orphan (still module-linked, no `outline_number`). **`PATCH /workitems/{wi}` itself IS allowed on heading WIs** — `update_work_item` can edit a heading's attributes just like any other WI. The lock is specific to body-part creation/relocation.

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
