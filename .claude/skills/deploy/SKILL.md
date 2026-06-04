---
name: deploy
description: Deploy (release/version bump/tag) automation for mcp-server-polarion. Bumps version in pyproject.toml + uv.lock, commits with conventional message, creates a date-only annotated tag, and pushes it to trigger the PyPI publish workflow, which then auto-publishes a categorized GitHub Release. Triggers on "/deploy", or any release/version bump/tag request.
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

## Step 6 — Create date-only tag locally

The tag annotation carries only the release date; all the human-readable detail lives in the GitHub Release (Step 7). Keeping the tag minimal means the published-version marker never drifts from the curated release notes.

```bash
RELEASE_DATE=$(date +%F)  # UTC release date, YYYY-MM-DD
git tag -a "v${NEW_VERSION}" -m "Release v${NEW_VERSION} (${RELEASE_DATE})"
git show "v${NEW_VERSION}" --no-patch  # verify the one-line annotation
```

No temp file, no interactive draft — the message is a single deterministic line.

## Step 7 — Push tag (confirm #2, with irreversibility warning)

AskUserQuestion with this exact warning text: **"Pushing tag v${NEW_VERSION} triggers `.github/workflows/publish.yml` → evals → TestPyPI → PyPI → GitHub Release. IRREVERSIBLE (PyPI blocks re-uploading the same version). Continue?"** Options: `push` / `cancel`.

```bash
git push origin "v${NEW_VERSION}"
```

On failure: local tag still exists; user can retry with `git push origin v${NEW_VERSION}` or delete locally via `git tag -d v${NEW_VERSION}`.

The GitHub Release is created automatically by the `release` job in `publish.yml` after PyPI succeeds — `gh release create --generate-notes` builds categorized notes from the PRs merged since the previous tag, grouped per `.github/release.yml`. Do NOT create a release by hand here; a manual `gh release create` would collide with the workflow (`release already exists`). Categorization quality depends on each merged PR carrying the right label, which `.github/workflows/label-pr.yml` stamps from the PR's conventional-commit title.

## Step 8 — Post-push summary

```bash
OWNER_REPO=$(git remote get-url origin | sed -E 's#.*github\.com[:/]([^/]+/[^/.]+).*#\1#')
echo "Released v${NEW_VERSION}"
echo "Release notes (auto-published after PyPI): https://github.com/${OWNER_REPO}/releases/tag/v${NEW_VERSION}"
echo "Watch: https://github.com/${OWNER_REPO}/actions"
```

## Hard rules

- NEVER `--no-verify`, `--no-edit`, `-c commit.gpgsign=false`.
- NEVER `git push --force`.
- NEVER auto-retry a failed push.
- NEVER `git commit --amend` after a hook rejection.
- ALWAYS confirm before commit push AND tag push (2 separate confirms).
- ALWAYS show diff / draft to user before destructive ops.
- NEVER put categorized release notes in the tag — the tag carries only the release date; notes are auto-generated into the GitHub Release by `publish.yml`.
- NEVER run `gh release create` by hand during deploy — the workflow owns it; a manual release collides with the workflow's `release` job.
