# Contributing to mcp-server-polarion

Thanks for taking the time to contribute. Every bug report, doc fix, and pull request helps.

This guide walks you through the contributor's path: **find something to work on → set up your
environment → make the change → open a pull request.** For the codebase architecture, tool-design
conventions, and Polarion API gotchas, see [CLAUDE.md](../CLAUDE.md) — read it before touching
`tools/` or `core/`.

> **The best first step is often an issue.** A clear bug report or a short design proposal lets us
> agree on the approach before anyone writes code — that saves you from reworking a PR later.

---

## Ways to contribute

- **Report a bug** — open a [bug report](https://github.com/devemberx/mcp-server-polarion/issues/new/choose).
  Include your Polarion version, MCP client, and steps to reproduce.
- **Request a feature** — open a [feature request](https://github.com/devemberx/mcp-server-polarion/issues/new/choose)
  describing the problem first, then your proposed tool or behavior.
- **Improve the docs** — fix a typo, clarify a tool docstring, or expand the README. Small docs PRs
  are always welcome and need no prior discussion.
- **Write code** — fix a bug or add a feature. Issues tagged **`good first issue`** are a friendly
  starting point. For anything larger than a small fix, open an issue first so we can align.

Security vulnerabilities go through [SECURITY.md](SECURITY.md), **not** public issues.

---

## Development setup

### Prerequisites

- [**uv**](https://docs.astral.sh/uv/) — manages the Python toolchain and dependencies.
- **Python 3.13+** (uv will fetch it if missing).
- A live **Polarion 2506+** instance is **not** required to contribute — the test suite mocks
  Polarion. You only need one to exercise the server end to end.

### Get the code

1. **Fork** this repository (the **Fork** button on GitHub).
2. **Clone** your fork and install dependencies:

   ```bash
   uv sync --dev
   ```

> Collaborators with write access may skip the fork and branch directly in this repo.

Optionally enable the local commit-message helper (see [Commit messages](#commit-messages)):

```bash
git config core.hooksPath .githooks
```

---

## Development workflow

1. **Branch off the latest `main`.** Use the `<type>/<short-kebab-summary>` form:

   | Prefix      | For                                          | Example                       |
   | ----------- | -------------------------------------------- | ----------------------------- |
   | `feature/`  | new tool or user-visible behavior            | `feature/read-fidelity`       |
   | `fix/`      | bug fix on existing behavior                 | `fix/utils-html-attachments`  |
   | `refactor/` | internal restructuring, no behavior change   | `refactor/tools`              |
   | `test/`     | tests, eval cases, fixtures only             | `test/efficiency-eval-cases`  |
   | `docs/`     | documentation only                           | `docs/contributing`           |
   | `chore/`    | deps, build tooling, housekeeping            | `chore/bump-fastmcp`          |
   | `ci/`       | GitHub Actions / release pipeline            | `ci/cache-uv-deps`            |

   One topic per branch; split unrelated work apart.

2. **Make your change.** Follow the rules in [CLAUDE.md](../CLAUDE.md) — strict async, full type
   annotations, log to stderr (never `print()`), and keep tool docstrings in sync with their models.

3. **Add tests.** `tests/` mirrors the source tree one-to-one. For write tools, verify the
   `dry_run=True` path. New `@mcp.tool`s also need their name added to `EXPECTED_TOOL_NAMES`.
   CI checks test coverage (target 80%); new code should be covered — run `uv run pytest --cov` to
   see what's missing.

4. **Run the checks locally** — the same gate CI runs:

   ```bash
   uv run ruff check . && uv run ruff format --check .
   uv run mypy src/
   uv run pytest
   ```

5. **Push to your fork** (`origin`) and open a pull request.

---

## Commit messages

We **squash-merge** PRs, so the final commit is built from your **PR title** plus the **Changes**
bullets — that's what follows the format. Your branch's "wip" commits don't matter; they vanish on
squash.

```
type(scope): summary       ← imperative, lowercase, no period, ≤50 chars

- why the change is needed  ← two bullets, ≤120 chars each
- what changed
```

- **type**: `feat` `fix` `docs` `refactor` `perf` `test` `ci` `chore`
- **scope**: `tool` `server` `transport` `config` `deps` `utils` `model` `project` `meta` `git`

Want it checked locally as you commit? Enable the optional hook:
`git config core.hooksPath .githooks`.

---

## Pull requests

- **Keep it small.** Small, focused PRs get reviewed fast; large ones sit in the queue. One concern
  per PR.
- **Open it against `devemberx/mcp-server-polarion:main`**, from your fork's branch.
- **Use the [pull request template](PULL_REQUEST_TEMPLATE.md)** (auto-loaded). Fill every section —
  Summary, Type of Change, Changes, Testing.
- **Link issues** with `Closes #<n>` or `Refs #<n>` in the Summary.
- **Make sure CI is green** — `ruff check` → `ruff format --check` → `mypy` → `pytest`.

### Review and merge

- At least one approving review is required; resolve review threads before merge.
- Merge strategy is **squash and merge** — your PR title becomes the commit subject and the
  *Changes* bullets become the body, so make them match the [commit format](#commit-messages).
- Force-pushing your own fork branch is fine (e.g. to clean up history before merge).

---

## AI-assisted contributions

Using an AI assistant to help write code or docs is welcome — this project is itself an MCP server.
But the same bar applies to every line:

- **You are the author.** Understand, and be able to explain, everything you submit. Review and test
  AI-generated output before opening a PR; don't open unreviewed machine-generated PRs.
- **Stay focused.** Don't let a tool expand the diff with unrelated refactors or boilerplate.
- **Bug fixes start with a failing test** that passes after your change, AI-assisted or not.

---

## Code of Conduct & License

This project follows a [Code of Conduct](CODE_OF_CONDUCT.md); by participating you agree to uphold it.
Contributions are licensed under the project's [MIT License](../LICENSE).
