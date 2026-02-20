## Summary

> Briefly describe **what** this PR does and **why** it's needed.

Closes #<!-- issue number — delete this line if there is no linked issue -->

---

## Type of Change

- [ ] Bug fix (non-breaking change that fixes an issue)
- [ ] New feature (non-breaking change that adds functionality)
- [ ] Breaking change (fix or feature that would cause existing functionality to change)
- [ ] Refactor (no functional changes, code quality improvement)
- [ ] Documentation update
- [ ] CI / tooling / dependency update

---

## Changes

> List the key changes made in this PR.

- 
- 
- 

---

## Testing

> Describe how you tested these changes.

- [ ] Unit tests added / updated (`tests/`)
- [ ] `dry_run=True` path verified for all write tools
- [ ] `uv run pytest` passes locally
- [ ] `uv run mypy src --strict` passes with no errors
- [ ] `uv run ruff check src tests` passes with no warnings

```bash
# Command(s) used to test
uv run pytest tests/ -v
```

---

## Golden Rule Compliance

> Confirm you have followed the project's Golden Rules.
> Items already enforced by `mypy --strict` or `ruff` are omitted — passing those checks in the Testing section is sufficient.

- [ ] No `print()` calls — all logging goes to `stderr` via `logging`
- [ ] `from __future__ import annotations` present in every modified module
- [ ] All tool inputs/outputs are Pydantic models (no raw `dict`)
- [ ] All new tool functions are `async def`
- [ ] Tool docstrings follow Google style with Args / Returns / Raises
- [ ] HTML converted via `html_to_text()` in read paths
- [ ] HTML sanitized via `text_to_polarion_html()` or `sanitize_html()` in write paths
- [ ] Any new list tool supports `page_size` and `page_number` pagination
- [ ] No secrets hardcoded — env vars via `pydantic-settings` only

---

## Screenshots / Logs

> If applicable, paste relevant logs or screenshots (e.g., MCP tool call output, Polarion API response).

<details>
<summary>Expand</summary>

```
# paste here
```

</details>

---

## Notes for Reviewers

> Anything the reviewer should pay special attention to, known limitations, or follow-up work.

