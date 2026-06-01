# Contributing Guide

Thanks for contributing to `mcp-server-polarion`. This document describes the **branch strategy**, **commit message rules**, and **pull request workflow** used in this repository. For codebase architecture and engineering rules, see [CLAUDE.md](../CLAUDE.md).

---

## 1. Branch Strategy

We use a lightweight trunk-based flow: `main` is always releasable, and all work happens on short-lived topic branches.

### Branch naming

Format: `<type>/<short-kebab-summary>`

| Type        | When to use                                                    | Example                       |
| ----------- | -------------------------------------------------------------- | ----------------------------- |
| `feature/`  | New tool, capability, or user-visible behavior                 | `feature/read-fidelity`       |
| `fix/`      | Bug fix on existing behavior                                   | `fix/utils-html-attachments`  |
| `refactor/` | Internal restructuring with no functional change               | `refactor/tools`              |
| `docs/`     | Documentation-only changes                                     | `docs/contributing`           |
| `chore/`    | Dependency bumps, build tooling, repository housekeeping       | `chore/bump-fastmcp`          |
| `ci/`       | GitHub Actions / workflow / release-pipeline changes           | `ci/cache-uv-deps`            |

Rules:

- Use **lowercase kebab-case** for the summary segment.
- Keep the summary short (≤4 words). The PR title carries the full description.
- One topic per branch. Split unrelated changes into separate branches.
- Branch off the latest `main`; rebase (do not merge) `main` into the branch before opening a PR.
- **Branch prefixes use the long form (`feature/`, `refactor/`); commit types use the short Conventional Commits form (`feat`, `refactor`).** The asymmetry is intentional — branch names read as English nouns, commit types follow the Conventional Commits spec.

### Protection rules

- `main` is protected. Direct pushes are not allowed — every change must go through a PR.
- **Force push to `main` is forbidden.** Force push to your own feature branch is allowed only after explicit reviewer authorization (e.g. after `git rebase -i` cleanup).

---

## 2. Commit Message Rules

### Format

```
<type>(<scope>): <subject>

- <motivation — why the change is needed>
- <change — what concretely was done>
```

### Type (Conventional Commits)

| Type       | Meaning                                                                |
| ---------- | ---------------------------------------------------------------------- |
| `feat`     | New feature (e.g. a new MCP tool, resource, or prompt)                 |
| `fix`      | Bug fix (logic error, protocol non-compliance)                         |
| `docs`     | Documentation-only change (README, docstrings, internal guides)        |
| `refactor` | Code change that neither fixes a bug nor adds a feature                |
| `perf`     | Performance improvement                                                |
| `test`     | Adding missing tests or correcting existing tests                      |
| `ci`       | CI configuration / GitHub Actions changes                              |
| `chore`    | Build process, dependency updates, auxiliary tooling                   |

### Scope (never omit)

| Scope       | Area                                                                                       |
| ----------- | ------------------------------------------------------------------------------------------ |
| `tool`      | Executable tool functions exposed to the LLM (e.g. `read.py`, `write.py`)                  |
| `server`    | MCP server lifecycle, initialization, internal state                                       |
| `transport` | Communication layer (stdio / sse / http)                                                   |
| `config`    | Environment variables, `.env`, static settings                                             |
| `deps`      | Python package management (`pyproject.toml`, lock files)                                   |
| `utils`     | Shared helper modules (e.g. `utils/html.py`)                                               |
| `model`     | Pydantic schemas in `models.py`                                                            |
| `project`   | Cross-cutting changes touching multiple scopes simultaneously                              |
| `meta`      | Repository maintenance (`.github/`, licenses, root-level configs)                          |
| `git`       | Git-specific config (`.gitignore`, pre-commit hooks, commit templates)                     |

### Subject constraints

- Imperative, present tense (`add`, not `added` or `adds`).
- Start with **lowercase**.
- **No** trailing period.
- **≤50 characters**, including `type(scope):` prefix.

### Body constraints

- One blank line between subject and body.
- **Exactly 2 bullets**, in this order: motivation first, then change.
- Do **not** prefix bullets with literal `Why:` / `What:` — the order alone carries that meaning.
- Each bullet is a **single line, ≤120 characters**. Longer rationale goes in the PR description.

### Examples

**Correct**

```
feat(tool): add description_html flag to get_work_item

- Round-trip editing was lossy because Markdown conversion dropped Polarion macros.
- Return raw HTML when description_html=True so update_work_item can re-apply it verbatim.
```

```
fix(utils): preserve attachment imgs on read

- Sanitizer stripped <img src="attachment:..."> tags, breaking image references in reads.
- Allow attachment: scheme through the BeautifulSoup sanitization allowlist.
```

**Incorrect (and why)**

| Bad                                              | Reason                                                  |
| ------------------------------------------------ | ------------------------------------------------------- |
| `feat: add new tool`                             | Missing scope                                           |
| `feat(TOOL): Add new tool`                       | Scope and subject must be lowercase                     |
| `fix(tool): Fixed the prompt injection bug.`     | Wrong tense + trailing period                           |
| `feat(docs): add guide`                          | Never combine `feat` with documentation — use `docs`    |
| `docs(git): update readme`                       | Missing mandatory blank line and 2-bullet body          |
| `- Why: ...` / `- What: ...`                     | Drop the literal `Why:` / `What:` prefixes              |

### Enforcement

The repo ships a `commit-msg` hook in [`.githooks/`](../.githooks/commit-msg) that mechanically enforces the subject and bullet length limits. Enable it once per clone:

```bash
git config core.hooksPath .githooks
```

The hook rejects any commit whose subject exceeds 50 chars or whose body does not contain exactly two bullets ≤120 chars each. Merge / revert / fixup / squash / amend autosquash subjects are skipped.

---

## 3. Pull Request Guidelines

### Before opening

- Branch is rebased onto the latest `origin/main`.
- All checks pass locally:
  ```bash
  uv run ruff check . && uv run ruff format --check .
  uv run mypy src/
  uv run pytest
  ```
- For write-tool changes: `dry_run=True` path verified.
- Public-facing tool changes are reflected in the tool's docstring (the LLM-facing manual).

### Opening the PR

- **Title** follows the commit-subject format: `type(scope): summary`, ≤70 characters. Keep details for the body. Since the merge strategy is squash, the PR title becomes the squashed commit subject — if your title exceeds the 50-char commit-subject limit, shorten it (or edit the subject at squash time) before merging.
- **Base branch**: `main`.
- Use the [pull request template](PULL_REQUEST_TEMPLATE.md). It is auto-loaded by GitHub.
- Fill every section: **Summary**, **Type of Change**, **Changes**, **Testing**.
- In **Type of Change**, keep the full checkbox list as written — only flip `[ ]` → `[x]` for matching items. Do **not** delete unchecked options.
- Link related issues with `Closes #<n>` or `Refs #<n>` in the Summary. Remove the placeholder line if there is no linked issue.

### Review and merge

- At least one approving review is required.
- CI must be green: `ruff check` → `ruff format --check` → `mypy` → `pytest`.
- Resolve conversations before merging; do not auto-resolve reviewer threads.
- **Merge strategy: squash and merge.** The squashed commit message must follow the commit-message rules above (the PR title becomes the subject; the PR description supplies the 2-bullet body).
- Delete the topic branch after merge.

### Force push policy

- Allowed on your own feature branch after explicit reviewer authorization (typically to clean up history before merge).
- **Forbidden on `main`** under any circumstance.

---

## 4. Development Quickstart

```bash
uv sync --dev                                            # install deps
uv run pytest                                            # run all tests
uv run ruff check . && uv run ruff format . && uv run mypy src/   # lint + format + types
uv run mcp-server-polarion                               # run server (stdio)
```

Architectural rules, tool-design conventions, and Polarion API gotchas live in [CLAUDE.md](../CLAUDE.md). Read it before touching `tools/` or `core/`.
