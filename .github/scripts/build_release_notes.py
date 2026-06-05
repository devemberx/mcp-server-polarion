#!/usr/bin/env python3
"""Build a GitHub Release body: GitHub's categorized auto-notes (grouped per
`.github/release.yml`), prefixed with a `## Highlights` block built from the
curated bullets carried in the annotated tag's own message body. The deploy
skill writes the bullets into the tag (no heading there — this script adds it),
so nothing accumulates in the repo tree. Reads GITHUB_REPOSITORY /
GITHUB_REF_NAME, prints markdown to stdout.

CI helper (not part of the shipped package), so printing to stdout is the
intended contract — it does not fall under the server's no-print rule.
"""

from __future__ import annotations

import json
import os
import subprocess


def _gh(*args: str) -> str:
    # Capture stdout only; let stderr inherit so gh's error message lands in the
    # CI log when a call fails (check=True still fails the step on non-zero).
    return subprocess.run(
        ["gh", *args], stdout=subprocess.PIPE, text=True, check=True
    ).stdout


def tag_highlights(repo: str, tag: str) -> str:
    """Curated highlight bullets = the annotated tag's message minus its first
    (dated marker) line, without the `## Highlights` heading (the caller adds
    it). Empty for a lightweight or date-only tag."""
    # Singular `git/ref/` returns one object; plural `git/refs/` prefix-matches
    # and yields an array when a sibling tag shares the prefix (e.g. -rc).
    ref = json.loads(_gh("api", f"repos/{repo}/git/ref/tags/{tag}"))
    obj = ref["object"]
    if obj["type"] != "tag":
        return ""
    message = json.loads(_gh("api", f"repos/{repo}/git/tags/{obj['sha']}"))["message"]
    return "\n".join(message.splitlines()[1:]).strip()


def main() -> None:
    repo = os.environ["GITHUB_REPOSITORY"]
    tag = os.environ["GITHUB_REF_NAME"]

    body = json.loads(
        _gh(
            "api",
            f"repos/{repo}/releases/generate-notes",
            "-X",
            "POST",
            "-f",
            f"tag_name={tag}",
        )
    )["body"]

    parts: list[str] = []
    intro = tag_highlights(repo, tag)
    if intro:
        parts.append("## Highlights")
        parts.append(intro)
        parts.append("")
    parts.append(body)
    print("\n".join(parts))


if __name__ == "__main__":
    main()
