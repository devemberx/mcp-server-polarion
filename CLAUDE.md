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
- **`tools/`** — `read.py` (12 read tools incl. `read_document` for flowing Markdown, `read_work_item` for a single work item's Markdown body, `list_work_items` with a `SQL:(...)` recipe gallery for module-scoped / custom-field / traceability searches Lucene cannot express, `list_document_comments` for review threads attached to a document, and `list_document_enum_options` / `list_work_item_enum_options` for resolving valid enum ids before writes), `write.py` (7 write tools incl. `update_document_comment` for resolving or re-opening review comments, each with its `_build_*_payload` helper), `_helpers.py` (sparse-fieldset constants, JSON:API extractors, pagination helpers, custom-field merge).
- **`utils/html.py`** — Markdown ↔ HTML (markdownify + BeautifulSoup4 sanitization).
- **`models.py`** — Pydantic v2 models. `PaginatedResult[T]` wraps all list responses.
- **`server.py`** — FastMCP instance with lifespan that opens/closes `PolarionClient`.

## Non-Negotiable Rules

- **NEVER `print()`** — stdout is reserved for MCP JSON-RPC; log to stderr.
- **NEVER `typing.Any`** — use concrete types or `object`.
- All functions: full type annotations + `from __future__ import annotations`. All tool functions: `async def`. All tool returns: Pydantic models, never raw `dict`.
- **Body fields are asymmetric by tool purpose**:
  - **Round-trip pair** (lossless): `get_*(include_*_html=True)` returns raw Polarion HTML; matching `update_*(*_html=...)` accepts the same shape verbatim — no sanitization, no Markdown conversion. XSS filtering is delegated to Polarion's renderer, so never route untrusted input through these parameters.
  - **Greenfield create** (Markdown): `create_work_item(description=...)` and `create_document(home_page_content=...)` accept Markdown; both run through `markdown_to_html` + `sanitize_html` before storage. After creation the round-trip pair switches to raw HTML (`update_work_item(description_html=...)` / `update_document(home_page_content_html=...)`) — the two formats never mix.
  - **Synthesis paths** (Markdown): `read_document` / `read_document_parts` / `read_work_item` convert HTML→Markdown via `html_to_markdown()`. Output is READ-ONLY — feeding it back to a write tool loses Polarion-specific markup.
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

- **Endpoints (JSON:API v1)**: `/projects`, `/projects/{p}/workitems[?query=...]`, `/projects/{p}/workitems/{work_item}[/linkedworkitems]`, `/projects/{p}/spaces/{s}/documents/{d}[/parts]`.
- **HTML payloads**: stored as `{"type": "text/html", "value": "..."}`.
- **Linked work item IDs**: 5 segments — derive the target via `relationships.workItem.data.id`, never by parsing.
- **Module IDs**: 3 segments and document names may contain `/`, so always use `split_module_id` (splits on the first two slashes only).
- **Lucene**: trailing wildcards (`title:SRS*`) work; leading wildcards return HTTP 400. The `module` field is not indexed. Workaround: pass `query="SQL:(...)"` to `list_work_items` for native SQL — module-scoped joins (`POLARION.REL_MODULE_WORKITEM`), leading-wildcard `LIKE`, custom-field joins (`POLARION.CF_WORKITEM`), and role-preserving traceability traversals (`POLARION.STRUCT_WORKITEM_LINKEDWORKITEMS`) all work. The `list_work_items` docstring carries a recipe gallery (4 recipes plus common adjustments on the module-scoped one); the Polarion SDK's [`SQLQueryExamples.pdf`](https://testdrive.polarion.com/polarion/sdk/doc/database/SQLQueryExamples.pdf) documents the schema and additional patterns (testrun / timepoint joins, `LUCENE_QUERY` table function, assignee joins). One server-side restriction: `LIKE` is rejected inside `EXISTS (SELECT ...)` ("Restricted SQL commands: LIKE") — use `INNER JOIN` so all `LIKE` filters live in the top-level `WHERE`.
- **Server limits**: ≤3 API calls/second, no concurrent requests; `PolarionClient` retries 429/5xx with backoff but does NOT serialize client-side.

### JSON:API quirks

**Sparse fieldset filters both attributes AND relationships.** `fields[workitems]=title,type,status` removes *all* `relationships` from the response, not just other attributes. List relationship names explicitly (see `WORK_ITEM_LIST_FIELDS`); forgetting silently empties derived fields like `space_id` / `document_name` / `assignee_ids` / `author_id`.

**To-many relationships need `include=`.** Polarion does not inline `data` for to-many relationships (e.g. `assignee`) — only `links` come back. Pass `"include": "assignee"` to populate `relationships.assignee.data`. To-one relationships (`module`, `author`, `project`) are inlined without `include`.

**`/backlinkedworkitems` is not supported on this server.** `list_work_item_links` returns one direction per call: forward links use `/projects/{p}/workitems/{work_item}/linkedworkitems`; back links fall back to a `query=linkedWorkItems:{wi}` search that does not expose the originating role, so back items return with `role=None`. Call twice when both directions are needed.

**`create_work_item_links` is bulk-only.** One POST on the same `/linkedworkitems` path carries `list[WorkItemLinkSpec]` (`min_length=1`); each spec composes one `linkedworkitems` resource. The returned composite ids `<srcProj>/<srcWI>/<role>/<tgtProj>/<tgtWI>` are the path identifiers for subsequent PATCH / DELETE of the same links. Mixed-success behavior (e.g. one duplicate among valid links) is **not currently characterised** on this server — on any 4xx response, assume nothing was committed and re-query with `list_work_item_links(direction="forward")` before retrying.

**`delete_work_item_links` is silently idempotent at the body level** (verified 2026-05-22 against testdrive). One DELETE with a JSON:API body listing composite ids; the tool re-assembles each id from `WorkItemLinkRef` (`role` + `target_work_item_id` + optional `target_project_id`) so the LLM never handles the raw 5-segment string. Refs whose composite id does not match an existing link are ignored and the request returns 204; refs that do match are deleted in the same call. The only 404 the tool surfaces is path-level (source WI missing). A malformed composite id in the body returns 400, but the tool layer constructs valid ids from structured refs so this is unreachable in normal use. The result's `link_ids` echoes the request, not necessarily what was actually deleted — cross-check with `list_work_item_links` if exact accounting is required.

**`update_work_item_links` is single-link.** Issues one PATCH against `/linkedworkitems/{role}/{tgtProj}/{tgtWI}`. To update multiple links, call the tool once per link. `suspect` is tri-state on update (`None` = leave unchanged) — distinct from `WorkItemLinkSpec.suspect: bool = False` on create. At least one of `suspect` / `revision` must be set; passing both as `None` raises `ValueError` before any PATCH is sent.

**Server-side enum validation is essentially absent.** Polarion stores unknown enum ids verbatim on every write path for `type` / `status` / `severity` / `resolution` and project-defined custom enums — they look real on subsequent reads but never match Lucene queries. The lone partial exception is `priority`: a non-numeric string (`priority="not_a_number"`) coerces to the project default, but a numeric string outside the enum set (`priority="999.0"`) is stored verbatim. Brand-new `custom_fields` keys — even on work item types that define no customs — also persist silently as ghosts. There is NO client-side enforcement. The `getAvailableOptions` action is wrapped twice: `list_document_enum_options` under `/documents/fields/{fieldId}/actions/getAvailableOptions` and `list_work_item_enum_options` under `/workitems/fields/{fieldId}/actions/getAvailableOptions`. Both share `EnumOption` + `_build_enum_option`, return identical sets for instance and type endpoints, do NOT filter by current workflow state, and silently fall back to the `~` no-type set for unknown types. The write tools' docstrings instruct the LLM to call them BEFORE every enum-shaped write, so docstring fidelity is the only guard — keep the resolution instructions in sync when extending write tools. Use `Literal[...]` on Pydantic Fields only for closed sets stable across projects.

### Custom fields surface inline under `attributes`

This server inlines project-defined customs as top-level keys in `attributes` — no `customFields` container, and `customFields.@all` / `@custom` / `@additional` tokens are silently dropped. The MCP server fetches with `fields[*]=@all` and filters out canonical attributes via the `STANDARD_WORK_ITEM_ATTRIBUTES` / `STANDARD_DOCUMENT_ATTRIBUTES` allowlists in `_helpers.py`; anything outside the allowlist is exposed on `*.custom_fields`. Values are kept raw (primitives or `{type: 'text/html', value: ...}` dicts) so the shape round-trips. A future Polarion release adding new standard attributes would misclassify them as custom until the allowlist is updated.

**Write side**: `create_work_item` / `update_work_item` / `update_document` accept `custom_fields: dict[str, object]` mirroring the read shape; `merge_custom_fields` inlines entries and raises `ValueError` on collision with a standard attribute. `None` inside the dict is skipped (explicit clearing unsupported). **Ghost customs**: Polarion does NOT validate custom-field IDs server-side, so an unknown key is silently stored and reappears on every subsequent `get_*` indistinguishable from a real one — always take keys from a prior read response. Wrong-type values DO get rejected (HTTP 400).

### Document content search — pick the right tool

Polarion Lucene does NOT index `description`, so `list_work_items` cannot filter by body text. Route content searches by scope:

| Goal | Tool | Notes |
|---|---|---|
| Filter work items inside a document by type/status/title/custom-field/links | `list_work_items` with `SQL:(...)` | Module-scoped joins via `REL_MODULE_WORKITEM`, custom-field joins via `CF_WORKITEM`, role-preserving traceability via `STRUCT_WORKITEM_LINKEDWORKITEMS`. Far smaller payload than `read_document_parts` when only metadata is needed. Recipe gallery in the tool's docstring; schema in the [SDK doc](https://testdrive.polarion.com/polarion/sdk/doc/database/SQLQueryExamples.pdf). |
| Find work items by metadata (title/type/status) | `list_work_items` | Lucene query against `title`, `type`, `status`, etc. — not `description`. |
| Read the document end-to-end | `read_document` | Renders interleaved headings + embedded work item bodies + prose as flowing Markdown. Paginated by part (default 100/page). The canonical "let me read this document" tool. |
| Get document metadata only | `get_document` | Title/type/status. `include_homepage_content_html=True` returns the `homePageContent` as **raw Polarion HTML** in `content_html` for round-trip editing via `update_document(home_page_content_html=...)`. Incomplete for end-to-end reading (heading text + embedded work item bodies live in separate work items, not in `homePageContent`) — use `read_document` for that. |
| Search inside a document with structural metadata | `read_document_parts` | Each `workitem` part carries `description` as Markdown — **no follow-up `get_work_item` call needed**. Embedded work items are fetched with the tight `WORK_ITEM_PART_FIELDS` sparse set (`title,type,status,description,outlineNumber`), not `@all`, to keep payloads small. `outlineNumber` lets `DocumentPart.outline_number` carry the hierarchical position (e.g. `'1.2.3'`) so `read_document` can prefix heading titles with it. Use when you need part IDs (for `move_work_item_to_document`), heading levels, or per-work item status/type. For plain reading, prefer `read_document`. |

### Document body writes go through `homePageContent` PATCH (NOT `/parts`)

Body edits use `PATCH /projects/{p}/spaces/{s}/documents/{d}` with `attributes.homePageContent.value` carrying the full body HTML — exposed at the tool layer as `update_document(home_page_content_html=...)`. The companion endpoints `/parts` POST and `actions/moveToDocument` are convenience wrappers that internally edit `homePageContent`; both reject heading-type work items ("Cannot move headings" / "Creation of heading Parts is not supported"). Setting `relationships.module` directly on a work item links ownership only — it does NOT create a body part. `PATCH /workitems/{work_item}` IS allowed on heading work items (`update_work_item` can edit a heading's attributes); the lock is specific to body-part creation/relocation. The tool layer rejects `home_page_content_html=""` to stop an accidental wipe from orphaning every heading. Removing an `<hN>` later removes the part but leaves the heading work item as an orphan (module-linked, no `outline_number`).

**Two `update_document` body-edit pitfalls**:

1. **Plain `<hN>` is safe; ID-anchor-less `<p>` IS NOT.** Appending `<h3>Heading</h3>` alone is fine — Polarion auto-creates a heading work item with `module` and `outline_number` set, and the new `heading_MCPT-N` part renders correctly. But adding even one anchorless `<p>Body</p>` in the same PATCH lets the PATCH return 200 while the next `GET .../parts` returns HTTP 500. The same applies to anchorless `<ul>`, `<ol>`, `<table>`, `<div>`, `<blockquote>`, and `<pre>` — every block in that set needs a unique non-empty `id=` (any prefix works; Polarion stores ids verbatim and rejects duplicates with HTTP 400). The two write paths split responsibility: `create_document(home_page_content=Markdown)` runs `stamp_block_ids` after `sanitize_html` and auto-fills `id="polarion_mcp_N"` on every such block, while `update_document(home_page_content_html=...)` is raw HTML so the caller must stamp ids themselves. For body text, prefer creating a new work item and attaching via `create_work_item` + `move_work_item_to_document`.

2. **Injecting `<div id="polarion_wiki macro name=module-workitem;params=id=NEW-WI">` does NOT set the work item's `module` relationship.** The new part appears in `read_document_parts` as `workitem_<NEW-WI>`, but `get_work_item(<NEW-WI>)` reports `space_id=""`, `document_name=""`, `outline_number=""` — an inconsistent half-attached state. work item body parts must be added via `move_work_item_to_document`, which is the only path that updates `homePageContent`, sets `module`, and assigns `outline_number` atomically.

### Document comment PATCH — `update_document_comment`

`PATCH .../comments/{commentId}` accepts `{"data": {"type": "document_comments", "id": "<4-segment-full-id>", "attributes": {"resolved": bool}}}`. The `id` field in the PATCH body must be the full path `{project}/{space}/{document}/{commentId}`, not the short form returned by `list_document_comments`. The endpoint returns 204 No Content — no body is parsed. `resolved` is the only mutable attribute; text and author cannot be changed after creation. The operation is idempotent.

### Work item ↔ document attach / detach

The `module` relationship cannot be modified via `PATCH /workitems/{work_item}` — Polarion rejects it. Use the action endpoint pair instead: `POST .../actions/moveToDocument` (body carries `targetDocument` + AT MOST one of `previousPart` / `nextPart`; both omitted appends at end) and `POST .../actions/moveFromDocument` (no body, 204). Exposed at the tool layer as `move_work_item_to_document` and `move_work_item_from_document`. Specifying `relationships.module` on `POST /workitems` is technically valid but lands the new work item in the document's recycle bin until a separate Document Part is created, so `create_work_item` does NOT expose `module` — always create free-floating and follow up with `move_work_item_to_document`. `move_work_item_from_document` is not idempotent: a second call against an already-free-floating work item returns HTTP 400 → `RuntimeError`. Detaching a heading-type work item is allowed (unlike `moveToDocument`, which rejects headings) and leaves the heading as a free-floating work item with `space_id=""` / `outline_number=""`.

### `PATCH /workitems/{work_item}` quirks

PATCH bodies need at least one `attributes` / `relationships` entry — Polarion 400s otherwise even when only `workflowAction` / `changeTypeTo` is set; `update_work_item` validates this at the tool layer. Setting `changeTypeTo` also resets `status` to the new type's initial workflow state (e.g. `task[status=approved]` → `defect[status=open]`); callers wanting to preserve status must re-apply it in a follow-up call.

## Testing

`pytest-asyncio` in `mode=auto`. **Tool tests** (`tests/tools/`) call tool functions directly with an injected `mock_client` (FastMCP 3.0's `@mcp.tool` returns the original function unchanged). **Client tests** (`tests/core/test_client.py`) use `respx` to mock `httpx`. Shared fixtures live in `tests/conftest.py`; pass `write_delay=0` for real `PolarionClient` instances. Pydantic `Field` constraints (`min_length` / `ge` / `le`) bypass FastMCP's JSON Schema on direct calls — verify them by reconstructing a `TypeAdapter` from `Annotated[type, FieldInfo]` (see `TestCreateWorkItemFieldValidation`).

## Repo Conventions

Branch strategy, full commit rules, and PR workflow are in [.github/CONTRIBUTING.md](.github/CONTRIBUTING.md). Quick reference for commit/PR generation:

- **Branches**: `<type>/<short-kebab-summary>` off latest `main` (e.g. `feature/read-fidelity`). Types: `feature | fix | refactor | docs | chore | ci`. One topic per branch.
- **Commits**:
  - Subject: `type(scope): summary` — lowercase imperative, ≤50 chars, no period. Types: `feat | fix | docs | refactor | perf | test | ci | chore`. Scopes: `tool | server | transport | config | deps | utils | model | project | meta | git`.
  - Body: blank line + **exactly 2 bullets** (motivation, then change) — no `Why:` / `What:` prefixes. Each bullet ≤120 chars.
  - **Length is strict** — before staging, draft the subject/bullets into a file and run `awk '{print length}' <file>` to verify. The `.githooks/commit-msg` validator (enable once per clone: `git config core.hooksPath .githooks`) rejects oversize commits mechanically. The PR-title length budget on a squash merge is 50 minus the auto-appended ` (#NNN)` suffix.
- **PR Type of Change checklist** ([.github/PULL_REQUEST_TEMPLATE.md](.github/PULL_REQUEST_TEMPLATE.md)): flip `[ ]` → `[x]` for matching items; do not delete unchecked options.
- **Force push** allowed on feature branches only after explicit user authorization. Never force-push to `main`.
