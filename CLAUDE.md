# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

MCP server providing AI assistants (Claude, Cursor, Copilot) with read and write access to Polarion ALM via the Model Context Protocol. Built on FastMCP 3.0 with a strict async-only, fully-typed Python codebase.

## Commands

```bash
uv sync --dev          # install dependencies
uv run pytest          # run all tests
uv run pytest tests/tools/test_read.py::TestGetWorkItem -v       # single class
uv run pytest tests/tools/test_read.py::TestGetWorkItem::test_returns_work_item_detail  # single test
uv run ruff check .    # lint
uv run ruff format .   # format
uv run mypy src/       # type check
uv run mcp-server-polarion  # run server (stdio transport)
```

CI runs: `ruff check` → `ruff format --check` → `mypy` → `pytest`.

## Architecture

Three-layer design:

**`core/`** — Infrastructure
- `client.py`: Async `httpx` wrapper. Constructor takes `(config, *, write_delay=1.5)`. Implements exponential backoff for retryable statuses (429/5xx) and a `write_delay` after each mutation. Maps responses to `PolarionError` subclasses (401/403 → auth, 404 → not-found).
- `config.py`: Pydantic `PolarionConfig` loading `POLARION_URL` + `POLARION_TOKEN` from env / `.env`.
- `exceptions.py`: `PolarionError`, `PolarionAuthError`, `PolarionNotFoundError`.

**`tools/`** — MCP tools registered via `@mcp.tool()`
- `read.py`: 7 read-only tools (`list_projects`, `list_documents`, `get_document`, `get_document_parts`, `list_work_items`, `get_work_item`, `get_linked_work_items`).
- `write.py`: 2 write tools (`create_work_item`, `move_work_item_to_document`). Each tool is preceded by its private `_build_*_payload` helper and (for create-style endpoints) `_extract_created_*_id` helper. Mirrors `read.py`'s tool-then-helper layout.
- `_helpers.py`: Shared utilities — `WI_LIST_FIELDS` / `WI_DETAIL_FIELDS` (sparse fieldsets including relationship names), pagination (`compute_has_more`, `has_links_next`, `extract_total_count`), JSON:API helpers (`extract_relationship_id`, `extract_relationship_ids`, `split_module_id`, `extract_short_id`, `build_included_workitem_map`), and `build_work_item_summary_kwargs` (returns a `WorkItemSummaryKwargs` TypedDict shared between list / detail tools so `WorkItemDetail` stays a strict superset of `WorkItemSummary`).

**`utils/html.py`** — HTML ↔ Markdown conversion (markdownify + BeautifulSoup4 sanitization).

**`models.py`** — Pydantic v2 input/output models. `PaginatedResult[T]` wraps all list responses. `WorkItemDetail` extends `WorkItemSummary`; `Hyperlink` is a structured shape for work-item external links.

**`server.py`** — FastMCP instance with lifespan that initializes/closes `PolarionClient`.

## Non-Negotiable Rules

- **NEVER `print()`** — stdout is reserved for MCP JSON-RPC; log to stderr only.
- **NEVER `typing.Any`** — use concrete types or `object`.
- **All functions** must have full type annotations + `from __future__ import annotations`.
- **All tool functions** must be `async def`.
- **Return Pydantic models**, never raw `dict`.
- **HTML on read**: convert to Markdown via `html_to_markdown()`.
- **HTML on write**: convert from Markdown + sanitize (allowed tags: p, br, b, i, u, strong, em, ul, ol, li, h1-h4, table, tr/td/th/thead/tbody, a, span, div, pre, code; allowed schemes: http, https, mailto).
- **Write payloads** — skip `None`/empty values rather than sending them; Polarion may interpret empty as "clear default". JSON:API resource POSTs (e.g. `/workitems`) wrap in `{"data": [...]}`; **action endpoints** (`.../actions/<name>`) take a flat object — do NOT wrap. The `cast(dict[str, object], payload)` shim at the `client.post(json=...)` call site is intentional (dict invariance vs `JsonValue`).
- **Every list tool** must support `page_size` (max 100) and `page_number`; return `PaginatedResult[T]` with `has_more`.
- **Tool docstrings** are the LLM's manual — Google-style with Args/Returns/Raises sections. Keep return-field bullets in sync with the Pydantic model.

## Error Handling Pattern

Map domain exceptions to user-facing ones at the tool layer:
- `PolarionNotFoundError` → `ValueError` with actionable message
- `PolarionAuthError` → `PermissionError`
- `PolarionError` → `RuntimeError`

## Polarion API & Gotchas

JSON:API v1 format. Key endpoints:
- `GET /projects` — list projects
- `GET /projects/{projectId}/workitems?query=...` — Lucene search
- `GET /projects/{projectId}/workitems/{wiId}` — single work item
- `GET /projects/{projectId}/workitems/{wiId}/linkedworkitems` — outgoing links
- `GET /projects/{projectId}/spaces/{spaceId}/documents/{documentName}` — document metadata
- `GET /projects/{projectId}/spaces/{spaceId}/documents/{documentName}/parts` — doc structure

**Lucene query restrictions:** trailing wildcards (`title:SRS*`) are supported; **leading wildcards** (`*SRS*`) cause HTTP 400. The `module` field is **not indexed** and cannot be queried.

**HTML payloads** are stored as `{ "type": "text/html", "value": "..." }`.

**Linked work-item IDs** have 5 segments: `{projectId}/{sourceWiId}/{role}/{targetProjectId}/{targetWiId}`. Always derive the target via `relationships.workItem.data.id`, never by parsing the raw ID.

**Module IDs** have 3 segments: `{projectId}/{spaceId}/{documentName}`. Document names may contain `/` — use `split_module_id` (splits on first two slashes only).

### Sparse fieldset filters BOTH attributes AND relationships

`fields[workitems]=title,type,status` removes **all** `relationships` from the response, not just other attributes. To receive a relationship, list its name explicitly:

```python
WI_LIST_FIELDS = "title,type,status,priority,updated,module,assignee"
```

Forgetting this causes `relationships.module` / `relationships.assignee` / `relationships.author` to silently disappear from responses, leaving derived fields like `space_id` / `document_name` / `assignee_ids` / `author_id` empty.

### To-many relationships need `include=`

Polarion does not inline `data` for to-many relationships (e.g. `assignee`) — only `links` come back. To populate `relationships.assignee.data` (so `extract_relationship_ids` can read it), pass `"include": "assignee"` in the request params. To-one relationships (`module`, `author`, `project`) are inlined without `include`.

### `/backlinkedworkitems` is NOT supported on this server

The newer Polarion endpoint that exposes back-links with their original role is unavailable on the deployed server version. `get_linked_work_items` therefore falls back to a `query=linkedWorkItems:{wi}` search, which returns the source WI list **without role information**. Back-direction items are returned with `role=None` (intentional — do not reintroduce the legacy `"backlink"` placeholder; the `None` is an explicit "unknown" signal that future code can fill once the endpoint becomes available).

### Server-side validation is lenient on enum-like fields

Polarion does NOT strictly validate `type`, `status`, `priority`, or `severity` strings on create. Unrecognised values are silently coerced (`priority="not_a_number"` → project default) or stored verbatim (`type="not_a_real_type"` → ghost type). Use a `Literal[...]` annotation on the Pydantic Field for closed sets where the docstring claim of validation matters; otherwise add a WARNING to the field description. Discovered during PR #10 e2e against `MCP_Test_Project`.

### Heading work items are server-locked

Polarion rejects all API attempts to create or relocate heading parts:
- `POST .../documents/{d}/parts` with `type="heading"` → 400 "Creation of heading Parts is not supported"
- `POST .../documents/{d}/parts` attaching a heading WI as `type="workitem"` → 400 "Cannot add external Work Item of type Heading"
- `POST .../actions/moveToDocument` for a heading WI → 400 "Cannot move headings"

Headings appear immovable once Polarion creates them. The planned workaround is to create the heading WI inside the target document at creation time via the `module` relationship on `create_work_item` (a future `module_id` parameter — see PR #11 discussion).

## Server-Side Constraints (informational)

The company's Polarion server enforces:
- **≤ 3 API calls/second**
- **No concurrent requests**

`PolarionClient` does **not** currently implement client-side rate limiting or request serialization — callers must respect these limits or rely on Polarion's HTTP 429 responses (the client retries with exponential backoff). When adding bulk-operation tools, factor pacing into the design.

## Testing

`pytest-asyncio` runs in `mode=auto`. Two test patterns coexist:

1. **Tool tests (`tests/tools/`)** call tool functions directly with a `mock_client` (an `AsyncMock(spec=PolarionClient)`) injected via a `mock_ctx`. FastMCP 3.0's `@mcp.tool()` returns the original function unchanged, so direct invocation works. Tests set `mock_client.get.return_value = {...}` (or `side_effect = [...]` for multi-call flows) with hand-built JSON:API payloads.
2. **Client tests (`tests/core/test_client.py`)** use `respx` to mock the underlying `httpx` transport for retry/backoff/error-mapping behavior.

Shared fixtures live in `tests/conftest.py` (`polarion_config`, `polarion_client`). Tool tests define their own `mock_client` / `mock_ctx` at the file level. Pass `write_delay=0` when constructing a real `PolarionClient` in tests to skip the post-write sleep.

### Pydantic Field constraint tests

Direct function calls bypass FastMCP's JSON Schema gate, so `min_length=1` / `ge` / `le` constraints aren't naturally exercised. Reconstruct a `TypeAdapter` from the parameter's `Annotated[type, FieldInfo]` (extract via `inspect.signature` + `get_type_hints`) and assert it rejects bad input at the schema layer. See `TestCreateWorkItemFieldValidation` for the canonical pattern.

## Repo Conventions

- **Commit messages** follow `.vscode/git_commit_guide.md`: `type(scope): subject` (lowercase imperative ≤50 chars, no period) + blank line + exactly 2 bullet points (Why + What). Types: `feat|fix|docs|refactor|perf|test|ci|chore`. Common scopes: `tool|server|transport|config|deps|utils|model|project|meta|git`.
- **PRs** use `.github/pull_request_template.md` — fill in Summary, check the Type-of-Change box, list Changes, complete the Testing checkboxes (especially `dry_run` for write tools), and confirm Golden Rule Compliance.
- **Force push** is allowed on feature branches only after explicit user authorization (e.g. amending after a design pivot). Never force-push to `main`.
