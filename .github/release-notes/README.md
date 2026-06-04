# Release highlights

One optional file per release, named after the tag: `v<MAJOR>.<MINOR>.<PATCH>.md`
(e.g. `v1.2.0.md`).

The `deploy` skill drafts this during a release and commits it alongside the
version bump, so the tag points to a commit that contains it. On tag push,
`.github/scripts/build_release_notes.py` reads the matching file and prepends it
to GitHub's categorized auto-notes; if no file exists, the release body is the
categorized list alone (no highlights section).

Write plain, human-readable prose — start with a `## Highlights` heading and
summarize what matters, not a mechanical restatement of PR titles.
