# Polarion MCP Server — Development Instructions

## Golden Rules

These rules apply to **every file** in the codebase. Violating any of them will break the MCP protocol or CI pipeline.

| # | Rule |
|---|---|
| 1 | **NEVER use `print()`** — stdout is reserved for MCP JSON-RPC. All output goes to `stderr` via `logging`. |
| 2 | **NEVER use `typing.Any`** — use concrete types; `object` if truly unknown. |
| 3 | **All functions must have full type annotations** — parameters AND return types. |
| 4 | **Use `from __future__ import annotations`** at the top of every module. |
| 5 | **Return Pydantic models, not `dict`** — all tool inputs and outputs are Pydantic models. |
| 6 | **All tool functions must be `async def`** — Polarion API calls use httpx async. |
| 7 | **Tool docstrings are the LLM's only manual** — Google-style with Args / Returns / Raises. |
| 8 | **Never expose raw HTML to the AI** — convert via `html_to_text()` in read tools. |
| 9 | **Never send unsanitized HTML to Polarion** — use `text_to_polarion_html()` or `sanitize_html()` in write tools. |
| 10 | **Every list tool must support pagination** — `page_size` and `page_number` with `Field()` constraints. |
| 11 | **Every write tool must support `dry_run`** — preview payload without mutating. |
| 12 | **Secrets in `.env` only** — never hardcode credentials. Load via `pydantic-settings`. |
| 13 | **Use `uv` exclusively** — no pip, poetry, or pipenv. |
| 14 | **Keep tool count minimal** — 12 tools total (8 read + 4 write). |

---

## Project Overview

A Model Context Protocol (MCP) server for reading, analyzing, and writing documents in Polarion ALM.

| Aspect | Choice |
|---|---|
| Framework | FastMCP 2.0 (`fastmcp>=2.0,<3`) |
| Transport | stdio (`uvx polarion-mcp`) |
| HTTP Client | httpx (async) |
| HTML Processing | BeautifulSoup4 |
| Validation | Pydantic v2 |
| Config | pydantic-settings (env vars) |
| Python | **3.12+** |
| Package Manager | **uv** (mandatory) |
| Testing | pytest + pytest-asyncio + respx |
| Linter/Formatter | ruff |
| Type Checker | mypy (`--strict`) |

---

## Tooling: `uv`

```bash
uv add "fastmcp>=2.0,<3" httpx "beautifulsoup4>=4.12" pydantic "pydantic-settings>=2.0"
uv add --dev pytest pytest-asyncio respx ruff mypy
uv run polarion-mcp          # local run
uv run pytest                 # tests
uvx polarion-mcp              # production (no install)
```

---

## Project Structure

```
mcp-server-polarion/
├── pyproject.toml
├── uv.lock
├── README.md
├── LICENSE
├── .env.example
├── .gitignore
├── .vscode/
│   └── mcp.json
├── .github/
│   ├── copilot-instructions.md
│   └── workflows/
│       ├── ci.yml
│       └── publish.yml
├── src/
│   └── polarion_mcp/
│       ├── __init__.py
│       ├── __main__.py           # Entry point → mcp.run(transport="stdio")
│       ├── server.py             # FastMCP instance + lifespan
│       ├── models.py             # All Pydantic models (input/output)
│       ├── core/                 # Infrastructure layer
│       │   ├── __init__.py       # Re-exports: PolarionClient, PolarionConfig, exceptions
│       │   ├── client.py         # PolarionClient — async httpx wrapper with retry
│       │   ├── config.py         # PolarionConfig(BaseSettings) — env var loading
│       │   ├── exceptions.py     # PolarionError / PolarionAuthError / PolarionNotFoundError
│       │   └── logging.py        # setup_logging() → stderr only
│       ├── utils/                # Pure utility functions
│       │   ├── __init__.py       # Re-exports: html_to_text, text_to_polarion_html, sanitize_html
│       │   └── html.py           # HTML ↔ plain text conversion
│       └── tools/                # MCP tool definitions
│           ├── __init__.py       # Imports read & write to register tools on mcp
│           ├── read.py           # 8 read tools
│           └── write.py          # 4 write tools
└── tests/
    ├── conftest.py               # Shared fixtures (mock client, MCP test client)
    ├── test_models.py            # Pydantic model validation
    ├── core/
    │   ├── __init__.py
    │   ├── test_client.py        # PolarionClient HTTP behavior, retry
    │   └── test_config.py        # Config loading, env var validation
    ├── utils/
    │   ├── __init__.py
    │   └── test_html.py          # HTML ↔ text conversion edge cases
    └── tools/
        ├── __init__.py
        ├── test_read.py          # 8 read tools
        └── test_write.py         # 4 write tools + dry_run verification
```

### Import Structure

- `server.py` creates the `mcp = FastMCP(...)` instance.
- `tools/read.py` and `tools/write.py` import it via `from polarion_mcp.server import mcp` to use the `@mcp.tool()` decorator.
- `tools/__init__.py` runs `import polarion_mcp.tools.read` and `import polarion_mcp.tools.write` to register all tools.
- `server.py` calls `import polarion_mcp.tools` at the very bottom of the module (to avoid circular imports).
- `core/__init__.py` re-exports `PolarionClient`, `PolarionConfig`, and exception classes.
- `utils/__init__.py` re-exports `html_to_text`, `text_to_polarion_html`, and `sanitize_html`.

**Import examples:**
- `from polarion_mcp.core import PolarionClient, PolarionConfig`
- `from polarion_mcp.core.exceptions import PolarionNotFoundError`
- `from polarion_mcp.utils import html_to_text, text_to_polarion_html`

---

## Code Style

| Setting | Value |
|---|---|
| Python target | `py312` |
| Line length | 88 |
| Linter/Formatter | ruff |
| Import order | stdlib → third-party → local (ruff `I` rule) |
| Type checker | mypy `--strict` |

### Naming Conventions

| Element | Convention | Example |
|---|---|---|
| Variables | `snake_case` | `work_item_id`, `page_size` |
| Functions (all) | `snake_case` | `list_projects`, `build_url`, `parse_response` |
| Classes | `PascalCase` | `PolarionClient`, `WorkItemDetail` |
| Pydantic models | `PascalCase` | `WorkItemDetail`, `PaginatedResult` |
| Module files | `snake_case` | `html.py`, `read.py` |
| Packages / dirs | `snake_case` | `core/`, `utils/`, `tools/` |
| Constants | `UPPER_SNAKE_CASE` | `ALLOWED_TAGS`, `DEFAULT_PAGE_SIZE` |
| Private attrs/methods | `_leading_underscore` | `_client`, `_request`, `_build_url` |
| Type aliases | `PascalCase` + `type` keyword | `type JsonPayload = dict[str, object]` |

### PEP 8 Conventions

**Type unions** — prefer `X | None` over `Optional[X]`; prefer `X | Y` over `Union[X, Y]` (Python 3.10+, enforced by ruff `UP007`).

**Comparisons (ruff `E711` / `E712`)**:
- Use `is None` / `is not None` — never `== None` or `!= None`.
- Use `if flag:` / `if not flag:` — never `if flag == True:` or `if flag is False:`.
- Use `if sequence:` for empty checks — never `if len(sequence) == 0:`.

**Exception chaining** — always `raise NewError("msg") from original` when re-raising inside an `except` block. Preserves the traceback and satisfies PEP 3134 (ruff `B904`).

**`__all__`** — every `__init__.py` that re-exports public symbols must define `__all__`. This makes the public API explicit and prevents star-import pollution (PEP 8 "Public and Internal Interfaces").

**`from __future__ import annotations`** — required in every module (Golden Rule 4). Enables PEP 563 lazy annotation evaluation for forward references. Pydantic v2 fully supports this; no `model_rebuild()` workarounds needed for standard model definitions.

---

## Architecture Principles

### MCP Server (FastMCP 2.0)

- `server.py` creates the `FastMCP` instance and initializes/cleans up `PolarionClient` via a `lifespan` context manager.
- All tools access the client via `ctx.request_context.lifespan_context["polarion_client"]`.
- `__main__.py` only calls `mcp.run(transport="stdio")`.
- Entry point: `polarion-mcp = "polarion_mcp.__main__:main"` (pyproject.toml).

### core/ — Infrastructure Layer

**`core/client.py` (PolarionClient)**
- Reuses a single `httpx.AsyncClient` instance.
- Bearer token authentication via default headers.
- Base URL: `{POLARION_URL}/polarion/rest/v1`.
- Maps HTTP status codes to custom exceptions: 401/403 → `PolarionAuthError`, 404 → `PolarionNotFoundError`, others → `PolarionError`.
- **Must implement exponential backoff retry for 429 and 5xx** (max 2 retries).
- Provides `get()`, `post()`, `patch()`, and `close()` methods.

**`core/config.py` (PolarionConfig)**
- `PolarionConfig(BaseSettings)` — loads `POLARION_URL`, `POLARION_TOKEN`, `POLARION_PROJECT_ID`, `POLARION_VERIFY_SSL` from environment variables.
- `.env` file is for local development only and must be listed in `.gitignore`.
- Commit `.env.example` to document all required variables.

**`core/logging.py`**
- Uses a single `logging.StreamHandler(sys.stderr)` handler.
- Logger name: `polarion_mcp` (sub-modules: `polarion_mcp.core.client`, `polarion_mcp.tools`, etc.).
- Set `propagate = False` to prevent propagation to the root logger.

**`core/exceptions.py`**
- `PolarionError(Exception)` — base class, includes a `status_code` attribute.
- `PolarionAuthError(PolarionError)` — 401/403.
- `PolarionNotFoundError(PolarionError)` — 404.
- Tools must catch domain exceptions and convert them into **actionable error messages** (`ValueError`, `PermissionError`, `RuntimeError`). Messages must suggest the next step (e.g., "Use `list_work_items` to discover valid IDs.").

### utils/ — Pure Utility Functions

**`utils/html.py`**
- `html_to_text(html: str) -> str` — BeautifulSoup `get_text(separator="\n", strip=True)`.
- `text_to_polarion_html(text: str) -> str` — wraps blank-line-separated paragraphs in `<p>` tags; converts single line breaks to `<br/>`.
- `sanitize_html(html: str) -> str` — unwraps any tags not in the `ALLOWED_TAGS` frozenset.
- `ALLOWED_TAGS`: `p, br, b, i, u, strong, em, ul, ol, li, h1-h4, table, tr, td, th, thead, tbody, a, span, div, pre, code`.

---

## Polarion REST API Reference

### JSON:API Format

All requests and responses follow the JSON:API structure:

```json
{
  "data": {
    "type": "workitems",
    "attributes": {
      "title": "Login Feature",
      "description": { "type": "text/html", "value": "<p>...</p>" },
      "status": "draft",
      "type": "requirement"
    }
  }
}
```

List response:

```json
{
  "data": [ { "type": "workitems", "id": "project/WI-001", "attributes": { ... } } ],
  "meta": { "totalCount": 42 },
  "links": { "self": "...", "next": "...", "prev": "..." }
}
```

### Pagination

All list endpoints support `page[size]` (default 50) and `page[number]` (1-based) query parameters.

### Common Query Parameters

- `fields[workitems]` — sparse fieldset (request only the attributes you need).
- `include` — include related resources (e.g., `linkedWorkItems`).
- `query` — Lucene query string.

### Tool → Endpoint Mapping

#### Read Tools (8)

| Tool | Method | Path |
|---|---|---|
| `list_projects` | GET | `/projects` |
| `list_spaces` | GET | `/projects/{projectId}/spaces` |
| `list_documents` | GET | `/projects/{projectId}/spaces/{spaceId}/documents` |
| `get_document` | GET | `/projects/{projectId}/spaces/{spaceId}/documents/{documentName}` |
| `get_document_parts` | GET | `/projects/{projectId}/spaces/{spaceId}/documents/{documentName}/parts` |
| `list_work_items` | GET | `/projects/{projectId}/workitems` |
| `get_work_item` | GET | `/projects/{projectId}/workitems/{workItemId}` |
| `search_work_items` | GET | `/projects/{projectId}/workitems?query={luceneQuery}` |

#### Write Tools (4)

| Tool | Method | Path |
|---|---|---|
| `create_work_item` | POST | `/projects/{projectId}/workitems` |
| `update_work_item` | PATCH | `/projects/{projectId}/workitems/{workItemId}` |
| `add_document_comment` | POST | `/projects/{projectId}/spaces/{spaceId}/documents/{documentName}/comments` |
| `link_work_items` | POST | `/projects/{projectId}/workitems/{workItemId}/linkedworkitems` |

### Key API Notes

- **The description field is always HTML**: `{ "type": "text/html", "value": "<p>...</p>" }`.
- **Space ID is required to access documents** — guide the user to call `list_spaces` first in tool docstrings.
- **`update_work_item` must GET current state first**, then PATCH only the changed fields.

---

## Pydantic Models

All tool inputs and outputs are defined as Pydantic `BaseModel` subclasses. Add `Field(description=...)` to every field so FastMCP can auto-generate JSON Schema for the LLM.

### Required Models

| Model | Purpose |
|---|---|
| `PaginatedResult[T]` | Common response wrapper for all list tools (`items`, `total_count`, `page`, `page_size`) |
| `ProjectSummary` | Item returned by `list_projects` (`id`, `name`) |
| `SpaceSummary` | Item returned by `list_spaces` (`id`, `name`) |
| `DocumentSummary` | Item returned by `list_documents` (`id`, `title`, `space_id`) |
| `DocumentDetail` | Response from `get_document` (`id`, `title`, `description`, `space_id`, `project_id`) |
| `DocumentPart` | Item returned by `get_document_parts` (`id`, `title`, `content`, `type`, `level`) |
| `WorkItemSummary` | Item returned by `list_work_items` and `search_work_items` (`id`, `title`, `type`, `status`) |
| `WorkItemDetail` | Response from `get_work_item` — extends `WorkItemSummary` (`description`, `project_id`) |
| `WorkItemCreateResult` | Response from `create_work_item` (`created`, `dry_run`, `work_item_id`, `payload_preview`) |
| `WorkItemUpdateResult` | Response from `update_work_item` (`updated`, `dry_run`, `current`, `changes`) |
| `CommentResult` | Response from `add_document_comment` (`created`, `dry_run`, `comment_id`, `payload_preview`) |
| `LinkResult` | Response from `link_work_items` (`created`, `dry_run`, `payload_preview`) |

### Model Rules

- Use PEP 695 generic syntax: `class PaginatedResult[T](BaseModel)`.
- Always return Pydantic models instead of raw `dict`.
- `Field(description=...)` is required on every field — this is the parameter documentation sent to the LLM.

---

## Tool Design Rules

### Docstring Standard (Google Style)

Every tool must have a Google-style docstring with these mandatory sections:

1. **First line**: What the tool does (imperative mood).
2. **Extended description**: When to use it, how it relates to other tools, and prerequisites.
3. **Args**: Every parameter with type context and example values.
4. **Returns**: Field descriptions of the returned model.
5. **Raises**: Every exception the tool may throw.
6. **Cross-reference other tools**: e.g., "Use `list_spaces` first to discover space IDs."

### Read Tool Rules

- Always return Pydantic models — never expose raw API responses.
- Wrap results in `PaginatedResult[T]` for pagination metadata.
- Convert HTML descriptions via `html_to_text()` before returning.
- Use sparse fieldsets (e.g., `fields[workitems]=title,description,type,status`).

### Write Tool Rules

- Every write tool must include `dry_run: bool = Field(default=False)`.
- When `dry_run=True`, return a payload preview without making any API call.
- `update_work_item` must GET the current state before issuing a PATCH.
- Convert plain text to HTML using `text_to_polarion_html()`.

### Error Handling in Tools

- `PolarionNotFoundError` → `ValueError` (actionable message + suggest an alternative tool).
- `PolarionAuthError` → `PermissionError` (advise checking token permissions).
- `PolarionError` → `RuntimeError` (log and re-raise).

---

## Testing Strategy

- **mcp_client fixture**: Use `fastmcp.Client(mcp)` for InMemoryTransport testing.
- **HTTP mocking**: Mock the Polarion API with `respx`.
- **dry_run tests are mandatory**: Every write tool must have a dry_run verification test.
- **asyncio_mode = "auto"**: Set in `pyproject.toml`.
- Test directories mirror the source structure: `tests/core/`, `tests/utils/`, `tests/tools/`.
- Test files: `core/test_client.py`, `core/test_config.py`, `utils/test_html.py`, `test_models.py`, `tools/test_read.py`, `tools/test_write.py`.

---

## Security

- All secrets are loaded from environment variables (`config.py` via `pydantic-settings`).
- `.env` must be in `.gitignore`; only `.env.example` is committed.
- Environment variables: `POLARION_URL`, `POLARION_TOKEN`, `POLARION_PROJECT_ID`, `POLARION_VERIFY_SSL`.

---

## pyproject.toml Key Settings

```toml
[project]
requires-python = ">=3.12"

[project.scripts]
polarion-mcp = "polarion_mcp.__main__:main"

[tool.ruff]
target-version = "py312"
line-length = 88

[tool.ruff.lint]
select = ["E", "W", "F", "I", "UP", "B", "SIM", "ANN"]

[tool.ruff.lint.per-file-ignores]
"tests/**" = ["ANN"]

[tool.mypy]
python_version = "3.12"
strict = true

[tool.pytest.ini_options]
asyncio_mode = "auto"
testpaths = ["tests"]
```

---

## VSCode MCP Configuration

`.vscode/mcp.json`:

```json
{
  "servers": {
    "polarion-mcp": {
      "type": "stdio",
      "command": "uv",
      "args": ["run", "polarion-mcp"],
      "env": {
        "POLARION_URL": "https://your-polarion-instance.com",
        "POLARION_TOKEN": "your-token",
        "POLARION_PROJECT_ID": "your-project-id"
      }
    }
  }
}
```
