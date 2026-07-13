# Spec — <tool name or feature>

Copy this file to `.pipeline/spec.md` and fill every slot from the
spec-researcher report. Sections marked **required** must survive — the
reviewer judges spec fidelity section by section. Delete optional sections
that don't apply. For a non-tool feature (refactor, multi-file change),
replace Signature/Params/Response with a single **Behavior contract** section.

## Goal — required

One or two sentences: what it does, for whom, and sibling routing if any
("— use X instead for Y").

## Tool signature — required

```python
async def <name>(
    ctx: Context,
    <param>: <type> = Field(description="..."),
    page_size: int = ...,   # list tools: max 100 + page_number
    dry_run: bool = False,  # write tools only
) -> PaginatedResult[<Model>] | <Model>
```

## Request params — required

| Param | Type / default | Maps to (API param / path) | Notes |
|---|---|---|---|

## Response model — required

Minimal fields — defer detail to a future `get_*`. One bullet per field:
name, type, source path in the JSON:API payload (`attributes.x`,
`relationships.y.data.id`).

## Error mapping — required

| Polarion condition | Exception raised | Prevention hint for docstring |
|---|---|---|

## Write policy — write tools only

Guard checks (enum/link/custom-field), payload skip-None rules, `{"data": [...]}`
vs flat action body, post-write delay implications.

## Files to touch — required

- `src/...` — what changes
- `tests/...` — what changes
- Registration: `tools/__init__.py`, `EXPECTED_TOOL_NAMES`, eval coverage entry

## UNVERIFIED — required, may be empty

Open questions the researcher could not confirm from docs. Each item becomes
a live-verification entry in the plan's Verification section.
