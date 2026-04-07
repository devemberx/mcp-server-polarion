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
| 8 | **Never expose raw HTML to the AI** — convert via `html_to_markdown()` in read tools. |
| 9 | **Never send unsanitized HTML to Polarion** — use `markdown_to_html()` or `sanitize_html()` in write tools. `sanitize_html()` also validates URL schemes (only `http`, `https`, `mailto` allowed in `href`). |
| 10 | **Every list tool must support pagination** — `page_size` and `page_number` with `Field()` constraints. |
| 11 | **Every write tool must support `dry_run`** — preview payload without mutating. |
| 12 | **Secrets in `.env` only** — never hardcode credentials. Load via `pydantic-settings`. |
| 13 | **Use `uv` exclusively** — no pip, poetry, or pipenv. |
| 14 | **Keep tool count minimal** — 12 tools total (7 read + 5 write). |

---

## Project Overview

A Model Context Protocol (MCP) server for reading, analyzing, and writing documents in Polarion ALM.

| Aspect | Choice |
|---|---|
| Framework | FastMCP 2.0 (`fastmcp>=2.0,<3`) |
| Transport | stdio (`uvx mcp-server-polarion`) |
| HTTP Client | httpx (async) |
| HTML Processing | markdownify (HTML→MD) + markdown-it-py (MD→HTML) + BeautifulSoup4 (sanitize) |
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
uv add "fastmcp>=2.0,<3" httpx "beautifulsoup4>=4.12" pydantic "pydantic-settings>=2.0" markdownify markdown-it-py
uv add --dev pytest pytest-asyncio respx ruff mypy
uv run mcp-server-polarion    # local run
uv run pytest                 # tests
uvx mcp-server-polarion       # production (no install)
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
│   └── mcp_server_polarion/
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
│       │   ├── __init__.py       # Re-exports: html_to_markdown, markdown_to_html, sanitize_html
│       │   └── html.py           # HTML ↔ Markdown conversion + HTML sanitization
│       └── tools/                # MCP tool definitions
│           ├── __init__.py       # Imports read to register tools on mcp
│           ├── _helpers.py       # Shared helpers (get_client, safe_str, extract_total_count, etc.)
│           └── read.py           # 7 read tools
└── tests/
    ├── conftest.py               # Shared fixtures (mock client, MCP test client)
    ├── test_models.py            # Pydantic model validation
    ├── core/
    │   ├── __init__.py
    │   ├── test_client.py        # PolarionClient HTTP behavior, retry
    │   └── test_config.py        # Config loading, env var validation
    ├── utils/
    │   ├── __init__.py
    │   └── test_html.py          # HTML ↔ Markdown conversion edge cases
    └── tools/
        ├── __init__.py
        └── test_read.py          # 7 read tools
```

### Import Structure

- `server.py` creates the `mcp = FastMCP(...)` instance.
- `tools/read.py` imports it via `from mcp_server_polarion.server import mcp` to use the `@mcp.tool()` decorator.
- `tools/_helpers.py` provides shared helpers used by `tools/read.py` (and future `tools/write.py`).
- `tools/__init__.py` runs `import mcp_server_polarion.tools.read` to register all tools.
- `server.py` calls `import mcp_server_polarion.tools` at the very bottom of the module (to avoid circular imports).
- `core/__init__.py` re-exports `PolarionClient`, `PolarionConfig`, and exception classes.
- `utils/__init__.py` re-exports `html_to_markdown`, `markdown_to_html`, and `sanitize_html`.

**Import examples:**
- `from mcp_server_polarion.core import PolarionClient, PolarionConfig`
- `from mcp_server_polarion.core.exceptions import PolarionNotFoundError`
- `from mcp_server_polarion.utils import html_to_markdown, markdown_to_html`
- `from mcp_server_polarion.tools._helpers import get_client, safe_str, extract_total_count`

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
- Entry point: `mcp-server-polarion = "mcp_server_polarion.__main__:main"` (pyproject.toml).

### core/ — Infrastructure Layer

**`core/client.py` (PolarionClient)**
- Reuses a single `httpx.AsyncClient` instance.
- Bearer token authentication via default headers.
- Base URL: `{POLARION_URL}/polarion/rest/v1`.
- Maps HTTP status codes to custom exceptions: 401/403 → `PolarionAuthError`, 404 → `PolarionNotFoundError`, others → `PolarionError`.
- **Must implement exponential backoff retry for 429 and 5xx** (max 2 retries).
- **Must add a 1–2 second delay between sequential write operations** to account for Polarion cluster propagation delay (~3s).
- Provides `get()`, `post()`, `patch()`, and `close()` methods.

**`core/config.py` (PolarionConfig)**
- `PolarionConfig(BaseSettings)` — loads `POLARION_URL`, `POLARION_TOKEN` from environment variables.
- `.env` file is for local development only and must be listed in `.gitignore`.
- Commit `.env.example` to document all required variables.

**`core/logging.py`**
- Uses a single `logging.StreamHandler(sys.stderr)` handler.
- Logger name: `mcp_server_polarion` (sub-modules: `mcp_server_polarion.core.client`, `mcp_server_polarion.tools`, etc.).
- Set `propagate = False` to prevent propagation to the root logger.

**`core/exceptions.py`**
- `PolarionError(Exception)` — base class, includes a `status_code` attribute.
- `PolarionAuthError(PolarionError)` — 401/403.
- `PolarionNotFoundError(PolarionError)` — 404.
- Tools must catch domain exceptions and convert them into **actionable error messages** (`ValueError`, `PermissionError`, `RuntimeError`). Messages must suggest the next step (e.g., "Use `list_work_items` to discover valid IDs.").

### utils/ — Pure Utility Functions

**`utils/html.py`**
- `html_to_markdown(html: str) -> str` — converts Polarion HTML to Markdown via `markdownify`. Preserves headings, lists, tables, and inline formatting as Markdown syntax. LLMs process Markdown far more efficiently than raw HTML.
- `markdown_to_html(text: str) -> str` — converts Markdown to Polarion-compatible HTML via ``markdown-it-py`` (CommonMark + GFM tables). Handles 2-space nested lists correctly (critical for LLM output). LLMs write Markdown naturally; this converts their output to the HTML format Polarion requires.
- `sanitize_html(html: str) -> str` — removes disallowed tags (decomposing `script`/`style` entirely, unwrapping others) and strips unsafe attributes. Validates `href` URLs against a safe-protocol allowlist (`http`, `https`, `mailto`) to prevent `javascript:` URI injection.
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

All list endpoints support `page[size]` and `page[number]` (1-based) query parameters. The Polarion server maximum page size is **100**. MCP tools should default to **100** to minimize API calls.

### Common Query Parameters

- `fields[workitems]` — sparse fieldset (request only the attributes you need).
- `include` — include related resources (e.g., `linkedWorkItems`).
- `query` — Lucene query string.

### Tool → Endpoint Mapping

#### Read Tools (7)

| Tool | Method | Path |
|---|---|---|
| `list_projects` | GET | `/projects` |
| `list_documents` | GET | `/projects/{projectId}/workitems?fields[workitems]=module&query=type:heading&sort=module` (indirect — parse `module` field to extract unique Space ID + Document Name pairs) |
| `get_document` | GET | `/projects/{projectId}/spaces/{spaceId}/documents/{documentName}` |
| `get_document_parts` | GET | `/projects/{projectId}/spaces/{spaceId}/documents/{documentName}/parts` |
| `list_work_items` | GET | `/projects/{projectId}/workitems` (optional `query` param for Lucene filtering) |
| `get_work_item` | GET | `/projects/{projectId}/workitems/{workItemId}` |
| `get_linked_work_items` | GET | `/projects/{projectId}/workitems/{workItemId}/linkedworkitems` |

> **Unsupported**: `GET /projects/{projectId}/documents` — not available on the target Polarion version. `list_documents` uses an indirect approach via heading work items.

#### Write Tools (5)

| Tool | Method | Path |
|---|---|---|
| `create_work_item` | POST | `/projects/{projectId}/workitems` |
| `update_work_item` | PATCH | `/projects/{projectId}/workitems/{workItemId}` |
| `add_document_comment` | POST | `/projects/{projectId}/spaces/{spaceId}/documents/{documentName}/comments` |
| `link_work_items` | POST | `/projects/{projectId}/workitems/{workItemId}/linkedworkitems` |
| `create_document_part` | POST | `/projects/{projectId}/spaces/{spaceId}/documents/{documentName}/parts` |

> **Unsupported**: `move_document_part` (`POST .../parts/{partId}/actions/move`) — not available on the target Polarion version. Use `create_document_part` with `next_part_id`/`previous_part_id` for positioning instead.

### Key API Notes

- **The description field is always HTML**: `{ "type": "text/html", "value": "<p>...</p>" }`.
- **Content-Type is `application/json`** — NOT `application/vnd.api+json`.
- **`POST /workitems` returns 201 with array response**: `{"data": [...]}` — parse accordingly.
- **`GET /projects/{projectId}/spaces` does NOT exist** — `list_documents` must use an indirect approach: query heading Work Items with `fields[workitems]=module` and `query=type:heading`, parse the `module` field (`{projectId}/{spaceId}/{documentName}` format), and extract unique (Space ID, Document Name) pairs.
- **Space ID and document name are required to access documents** — guide the user to call `list_documents` first in tool docstrings.
- **`update_work_item` must GET current state first**, then PATCH only the changed fields.
- **Document Recycle Bin**: Creating a Work Item with a `module` relationship places it in the Document's Recycle Bin — it is NOT visible. After `create_work_item`, call `create_document_part` separately to insert the Work Item into the document body as a visible Part.
- **`get_linked_work_items`** fetches forward links via `GET .../linkedworkitems` (with `fields[linkedworkitems]=@all&include=workItem&fields[workitems]=title,type,status` to get role, suspect, and target title) and back links via a **camelCase** Lucene query (`linkedWorkItems:{id}`) on the workitems endpoint. The `backlinkedworkitems` endpoint is **not available** on the target Polarion version.
- **Linked work item ID format is 5 segments**: `{projectId}/{sourceWiId}/{role}/{targetProjectId}/{targetWiId}` (e.g. `MCP_Test_Project/MCPT-9/parent/MCP_Test_Project/MCPT-1`). Use `attributes.role` for the role and `relationships.workItem.data.id` for the target WI — do NOT parse the raw ID for these values.
- **URL encoding required** for document names with spaces (e.g., `Software%20Requirement%20Specification`).
- **`get_document_parts`** uses `fields[document_parts]=@all&include=workItem&fields[workitems]=title,description,type,status` to fetch part attributes, relationships (`nextPart`, `previousPart`, `workItem`), and included work items for title/description resolution.
- **Document Part ID format**: `heading_MCPT-xxx` or `workitem_MCPT-xxx`.
- **`document_parts` (underscore)** is the JSON:API resource type name.

---

## Pydantic Models

All tool inputs and outputs are defined as Pydantic `BaseModel` subclasses. Add `Field(description=...)` to every field so FastMCP can auto-generate JSON Schema for the LLM.

### Required Models

| Model | Purpose |
|---|---|
| `PaginatedResult[T]` | Common response wrapper for all list tools (`items`, `total_count`, `page`, `page_size`, `has_more`) |
| `ProjectSummary` | Item returned by `list_projects` (`id`, `name`) |
| `DocumentSummary` | Item returned by `list_documents` (`space_id`, `document_name`) |
| `DocumentPartCreateResult` | Response from `create_document_part` (`created`, `dry_run`, `part_id`, `payload_preview`) |
| `DocumentDetail` | Response from `get_document` (`id`, `title`, `content`, `space_id`, `project_id`) |
| `DocumentPart` | Item returned by `get_document_parts` (`id`, `title`, `content`, `type`, `level`, `description`, `next_part_id`, `previous_part_id`) |
| `WorkItemSummary` | Item returned by `list_work_items` (`id`, `title`, `type`, `status`) |
| `WorkItemDetail` | Response from `get_work_item` — extends `WorkItemSummary` (`description`, `project_id`) |
| `WorkItemCreateResult` | Response from `create_work_item` (`created`, `dry_run`, `work_item_id`, `payload_preview`) |
| `WorkItemUpdateResult` | Response from `update_work_item` (`updated`, `dry_run`, `current`, `changes`) |
| `CommentResult` | Response from `add_document_comment` (`created`, `dry_run`, `comment_id`, `payload_preview`) |
| `LinkResult` | Response from `link_work_items` (`created`, `dry_run`, `payload_preview`) |
| `LinkedWorkItemSummary` | Item returned by `get_linked_work_items` (`id`, `title`, `role`, `direction`, `suspect`) |
| `LinkedWorkItemsList` | Response from `get_linked_work_items` (`items`, `forward_count`, `back_count`, `total_count`) |

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
6. **Cross-reference other tools**: e.g., "Use `list_documents` first to discover space IDs and document names."

### Read Tool Rules

- Always return Pydantic models — never expose raw API responses.
- Wrap results in `PaginatedResult[T]` for pagination metadata.
- Convert HTML descriptions via `html_to_markdown()` before returning.
- Use sparse fieldsets (e.g., `fields[workitems]=title,description,type,status`).

### Write Tool Rules

- Every write tool must include `dry_run: bool = Field(default=False)`.
- When `dry_run=True`, return a payload preview without making any API call.
- `update_work_item` must GET the current state before issuing a PATCH.
- Convert Markdown input to HTML using `markdown_to_html()`. LLMs write Markdown naturally; accept both Markdown and plain text.
- Sequential write operations must include a 1–2 second delay to account for Polarion cluster propagation delay.
- `create_work_item`: returns the created Work Item ID. To make it visible in a document, the user must call `create_document_part` separately afterward.
- `create_document_part`: inserts a Work Item into the Document Body as a Part. Uses the `document_partsListPostRequest` schema with `relationships.workItem` (required) and optional `nextPart`/`previousPart` for positioning. This is the tool that solves the Recycle Bin problem.

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
- Environment variables: `POLARION_URL`, `POLARION_TOKEN`

---

## pyproject.toml Key Settings

```toml
[project]
requires-python = ">=3.12"

[project.scripts]
mcp-server-polarion = "mcp_server_polarion.__main__:main"

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
    "mcp-server-polarion": {
      "type": "stdio",
      "command": "uv",
      "args": ["run", "mcp-server-polarion"],
      "env": {
        "POLARION_URL": "https://your-polarion-instance.com",
        "POLARION_TOKEN": "your-token"
      }
    }
  }
}
```
