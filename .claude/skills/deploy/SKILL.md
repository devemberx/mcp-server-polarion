---
name: deploy
description: Deploy (release/version bump/tag) automation for mcp-server-polarion. Bumps version in pyproject.toml + uv.lock, commits with conventional message, creates structured annotated tag, and pushes to trigger PyPI publish workflow. Triggers on "/deploy", or any release/version bump/tag request.
---

# Deploy Skill

You are orchestrating a production release. Be deliberate. Never skip hooks. Never push without explicit user confirmation.

## Step 1 — Pre-flight checks (abort on any failure)

```bash
git status --porcelain                  # MUST be empty
git rev-parse --abbrev-ref HEAD         # MUST be 'main'
git fetch origin main
git rev-list --count HEAD..origin/main  # MUST be 0
git rev-list --count origin/main..HEAD  # MUST be 0
git config core.hooksPath               # SHOULD be '.githooks'
```

- Dirty tree → list offending files and stop. Do NOT auto-stash.
- Branch != main → ask user to switch manually. Do NOT switch.
- Behind origin → tell user to `git pull --ff-only`.
- Ahead of origin → tell user to push or reset; release commit must sit on synced main.
- `core.hooksPath` ≠ `.githooks` → WARN, ask user to run `git config core.hooksPath .githooks`. The commit-msg hook is the only enforcement of the 50/120 rule.

## Step 2 — Determine new version (interactive)

```bash
sed -n '3p' pyproject.toml
PREV_TAG=$(git describe --tags --abbrev=0 2>/dev/null || echo "")
[ -n "$PREV_TAG" ] && git log "$PREV_TAG..HEAD" --oneline || git log --oneline
```

Analyze commits since `$PREV_TAG`:
- `breaking` / `!:` marker → recommend **major**
- ≥2 `feat:` → recommend **minor**
- only `fix:` / `chore:` / `docs:` → recommend **patch**

Call **AskUserQuestion** with options `patch` / `minor` / `major` / `custom`, noting the recommendation in the question text. For `custom`, prompt for exact `X.Y.Z`.

Verify the tag does not already exist:

```bash
git rev-parse "v${NEW_VERSION}" 2>/dev/null && abort "tag exists" || true
```

If it exists, abort and tell the user to pick a different version or delete the stale local tag manually (likely from an aborted prior run).

## Step 3 — Bump version

Edit `pyproject.toml` line 3 to `version = "X.Y.Z"` using the Edit tool (replace only that line).

```bash
uv lock                          # syncs uv.lock self-entry
git diff pyproject.toml uv.lock  # show diff to user
```

## Step 4 — Draft commit (interactive)

Draft two body bullets:
- **Bullet 1** (motivation/themes, ≤120 chars) — derive from commits since `$PREV_TAG`.
- **Bullet 2** (mechanical, fixed) — "Bump project version from PREV to NEW in pyproject.toml and uv.lock."

Show both bullets to the user via AskUserQuestion (`approve` / `edit` / `cancel`). On `edit`, accept replacement text and re-validate.

Validate lengths before invoking git:

```bash
awk '{print length}' <<<"chore(meta): bump version to ${NEW_VERSION}"  # ≤50
awk '{print length}' <<<"- ${BULLET1}"                                  # ≤120
awk '{print length}' <<<"- ${BULLET2}"                                  # ≤120
```

Commit (never `--no-verify`):

```bash
git add pyproject.toml uv.lock
git commit -m "$(cat <<'EOF'
chore(meta): bump version to X.Y.Z

- <bullet 1>
- <bullet 2>

Co-Authored-By: Claude Opus 4.7 <noreply@anthropic.com>
EOF
)"
```

On commit-msg hook rejection: surface the hook output, redo `git commit` with corrected message. **Never `--amend`** — hook rejection means no commit was made, so amending would mutate the previous (unrelated) commit and silently corrupt history.

## Step 5 — Push commit (confirm #1)

AskUserQuestion: "Push commit to origin/main? Makes the bump public but does NOT trigger the publish workflow yet." Options: `push` / `cancel`.

```bash
git push origin main
```

No auto-retry on failure — surface error and let the user decide.

## Step 6 — Draft tag annotation (interactive)

Categorize each commit since `$PREV_TAG`:
- `breaking` or `!:` in subject → **Breaking**
- `feat(tool):` prefix → **New tools** (extract tool name and PR # from subject)
- other `feat:` → **New**
- `chore:` / `docs:` / `ci:` / `refactor:` / `test:` / `fix:` → **Misc**

Write the draft to `/tmp/tag-msg-v${NEW_VERSION}.txt` using the Write tool. A temp file is required: `git tag -a -m` repeated flattens to paragraphs and breaks the labeled-section layout; `-F` preserves exact whitespace and newlines.

Format:

```
<one-line headline, ≤72 chars, derived from highest-impact change>

Breaking:
- ...

New tools:
- tool_name (#PR) — short description

New:
- feature description

Misc:
- mechanical change
```

Omit empty sections entirely. Show file contents to the user via AskUserQuestion (`approve` / `edit` / `cancel`); on `edit`, rewrite the temp file with the user's replacement text.

## Step 7 — Create tag locally

```bash
git tag -a "v${NEW_VERSION}" -F "/tmp/tag-msg-v${NEW_VERSION}.txt"
git show "v${NEW_VERSION}" --no-patch  # verify annotation rendered correctly
```

## Step 8 — Push tag (confirm #2, with irreversibility warning)

AskUserQuestion with this exact warning text: **"Pushing tag v${NEW_VERSION} triggers `.github/workflows/publish.yml` → TestPyPI → PyPI. IRREVERSIBLE (PyPI blocks re-uploading the same version). Continue?"** Options: `push` / `cancel`.

```bash
git push origin "v${NEW_VERSION}"
```

On failure: local tag still exists; user can retry with `git push origin v${NEW_VERSION}` or delete locally via `git tag -d v${NEW_VERSION}`.

## Step 9 — Post-push summary

```bash
OWNER_REPO=$(git remote get-url origin | sed -E 's#.*github\.com[:/]([^/]+/[^/.]+).*#\1#')
echo "Released v${NEW_VERSION}"
echo "Watch: https://github.com/${OWNER_REPO}/actions"
```

## Hard rules

- NEVER `--no-verify`, `--no-edit`, `-c commit.gpgsign=false`.
- NEVER `git push --force`.
- NEVER auto-retry a failed push.
- NEVER `git commit --amend` after a hook rejection.
- ALWAYS confirm before commit push AND tag push (2 separate confirms).
- ALWAYS show diff / draft to user before destructive ops.
