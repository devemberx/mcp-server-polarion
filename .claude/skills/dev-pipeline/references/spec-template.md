# Spec — <tool name or feature>

Copy this file to `.pipeline/spec.md` and fill every slot from the
spec-researcher report. Sections marked **required** must survive — they are
the standard pipeline-reviewer judges the diff against. Delete optional
sections that don't apply, and delete this instruction paragraph once filled —
downstream agents read the filled file verbatim. One exception to "required
must survive": for a non-tool feature (refactor, multi-file change), replace
Signature/Params/Response with a single **Behavior contract** section.

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

## Related open issues — optional

Open `follow-up` (or other) issues touching this domain, from the Stage 1
queue check. One bullet per issue: `#N — absorbed into this spec` or
`#N — deferred: <reason>`.

## UNVERIFIED — required, may be empty

Open questions the researcher could not confirm from docs. Each item becomes
a live-verification entry in the plan's Verification section.
