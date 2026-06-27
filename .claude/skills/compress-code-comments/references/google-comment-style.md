# Google-Style Comment Compression — Worked Examples

Reference for six principles in SKILL.md. Each show before → after on Python.
"Keep" = comment earn line. "Cut" = delete entirely.

## 1. Density — narrative → keyword fragments

```python
# Before
# We need to wait for a short period of time here after the mutation because
# Polarion's index is eventually consistent and an immediate read-back can
# return the stale version of the work item before the write has propagated.
await asyncio.sleep(POST_MUTATION_DELAY)

# After
# Post-mutation delay: Polarion index is eventually consistent; immediate
# read-back can be stale.
await asyncio.sleep(POST_MUTATION_DELAY)
```

Drop "We need to", "here", "because", "in order to". Lead with noun phrase.

## 2. Why, not what — delete code-restating comments

```python
# Cut — the code says this
count = count + 1        # increment count by one
items = []               # initialize an empty list
if user is None:         # check if user is None
    return

# Keep — the code can't say this
count += 1               # 1-based: Polarion pages start at 1, not 0
items: list[str] = []    # accumulates ids across pages; flushed per batch
if user is None:         # anonymous comment author resolves to None here
    return
```

Deleting comment lose nothing reader couldn't get from line? Delete it.

## 3. No step-by-step narration — lift one summary line

```python
# Before
def _build_payload(item):
    # First we copy the attributes
    attrs = dict(item.attributes)
    # Then we strip the None values so Polarion doesn't clear defaults
    attrs = {k: v for k, v in attrs.items() if v is not None}
    # Now we wrap it in the JSON:API data envelope
    return {"data": {"type": "workitems", "attributes": attrs}}

# After
def _build_payload(item):
    # Skip None/empty: Polarion reads them as "clear to default".
    attrs = {k: v for k, v in item.attributes.items() if v is not None}
    return {"data": {"type": "workitems", "attributes": attrs}}
```

Envelope line and copy line were "what" — gone. One non-obvious fact (why None
filtered) survive as single line.

## 4. One line per point — no paragraphs, no banner prefixes

```python
# Before
# ============================================================
# WARNING: IMPORTANT!!! READ BEFORE TOUCHING THE LINK ID
# ============================================================
# The link id is composite. It is made up of five segments joined by
# slashes. Do NOT try to parse it yourself. Always derive the target from
# the relationships block instead. This has bitten us before.

# After
# Link id is composite (5 slash-joined segments) — derive target from
# relationships, never parse.
```

No banner dividers (`# ====` or `# ----`), no `NOTE:`/`WARNING:`, no "this has bitten us before" dev-narrative, no `!!!`.

## 5. TODOs — Google format

```python
# Before
# todo: this is kind of hacky, should probably fix the retry logic at some point

# After
# TODO: replace ad-hoc retry with a backoff helper
```

Before name no owner, so don't invent one — keep ownerless with concrete action.
Use `# TODO(owner):` or `# TODO(#142):` only when context actually supply owner
or issue. Ownerless fine if still concrete: `# TODO: drop after
Polarion 2410 ships`.

## 6. Dev docstrings — one line per field, drop the obvious

```python
# Before
def _split_module_id(module_id):
    """Split a module id into its parts.

    Args:
        module_id: The module id string that we want to split. This is the
            id of the module as a string value.

    Returns:
        A tuple containing the project, space, and document name.
    """

# After
def _split_module_id(module_id):
    """Split a module id (project/space/name); document name may contain '/'."""
```

One fact worth keeping (name can contain `/`, so can't naive-split) now whole
docstring. Rest restated signature.

## What NOT to cut

- Anything explaining workaround, gotcha, or non-obvious constraint.
- Functional pseudo-comments: `# noqa`, `# type: ignore[...]`, `# pragma: no cover`,
  `# fmt: off`, shebangs, encoding lines. These drive tooling — preserve verbatim.
- `@mcp.tool` docstrings and `Field(description=...)` — owned by
  shrink-mcp-tool-docs; out of scope here.

Unsure if comment load-bearing? Keep it. Goal density, not amnesia.

## Boundary in practice — cut the body comment, preserve everything protected

Inside `@mcp.tool` function you may still tidy `#` comments in body — but
docstring, every `Field(description=...)`, and functional pseudo-comments stay
byte-identical. Gate fails if any move.

```python
# Before
@mcp.tool
async def get_work_item(
    work_item_id: str = Field(description="Verbose id description shipped to the LLM."),
) -> WorkItem:
    """Long verbose tool docstring that ships to the client LLM."""
    raw = await client.get(url)  # noqa: E501
    # First we parse the raw json into our model object and then we return it
    # back to the calling function for further processing.
    return parse(raw)

# After
@mcp.tool
async def get_work_item(
    work_item_id: str = Field(description="Verbose id description shipped to the LLM."),
) -> WorkItem:
    """Long verbose tool docstring that ships to the client LLM."""
    raw = await client.get(url)  # noqa: E501
    return parse(raw)
```

Only body "what"-comment cut. Docstring, `Field(...)`, and `# noqa: E501` untouched.