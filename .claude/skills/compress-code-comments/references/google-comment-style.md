# Google-Style Comment Compression — Worked Examples

Reference for the six principles in SKILL.md. Each shows a before → after on Python.
"Keep" = the comment earns its line. "Cut" = delete entirely.

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

Drop "We need to", "here", "because", "in order to". Lead with the noun phrase.

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

If deleting the comment loses nothing a reader couldn't get from the line, delete it.

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

The envelope line and the copy line were "what" — gone. The one non-obvious fact
(why None is filtered) survives as a single line.

## 4. One line per point — no paragraphs, no banner prefixes

```python
# Before
# NOTE: This is important!!! The link id is composite. It is made up of five
# segments joined by slashes. Do NOT try to parse it yourself. Always derive
# the target from the relationships block instead. This has bitten us before.

# After
# Link id is composite (5 slash-joined segments) — derive target from
# relationships, never parse.
```

No `NOTE:`/`WARNING:`, no "this has bitten us before" dev-narrative, no `!!!`.

## 5. TODOs — Google format

```python
# Before
# todo: this is kind of hacky, should probably fix the retry logic at some point

# After
# TODO(devemberx): replace ad-hoc retry with backoff helper
```

Concrete action, owner if context names one. Never invent an owner — ownerless is
fine if still concrete: `# TODO: drop after Polarion 2410 ships`.

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

The one fact worth keeping (the name can contain `/`, so you can't naive-split) is
now the whole docstring. The rest restated the signature.

## What NOT to cut

- Anything explaining a workaround, a gotcha, or a non-obvious constraint.
- Functional pseudo-comments: `# noqa`, `# type: ignore[...]`, `# pragma: no cover`,
  `# fmt: off`, shebangs, encoding lines. These drive tooling — preserve verbatim.
- `@mcp.tool` docstrings and `Field(description=...)` — owned by
  shrink-mcp-tool-docs; out of scope here.

When unsure whether a comment is load-bearing, keep it. Goal is density, not amnesia.
