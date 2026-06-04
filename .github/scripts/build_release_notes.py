#!/usr/bin/env python3
"""Build a GitHub Release body: GitHub's categorized auto-notes (grouped per
`.github/release.yml`), prefixed with the curated highlights carried in the
annotated tag's own message body. The deploy skill writes the highlights into
the tag, so nothing accumulates in the repo tree. Reads GITHUB_REPOSITORY /
GITHUB_REF_NAME, prints markdown to stdout.

CI helper (not part of the shipped package), so printing to stdout is the
intended contract — it does not fall under the server's no-print rule.
"""

from __future__ import annotations

import json
import os
import subprocess


def _gh(*args: str) -> str:
    return subprocess.run(
        ["gh", *args], capture_output=True, text=True, check=True
    ).stdout


def tag_highlights(repo: str, tag: str) -> str:
    """Curated highlights = the annotated tag's message minus its first
    (dated marker) line. Empty for a lightweight or date-only tag."""
    ref = json.loads(_gh("api", f"repos/{repo}/git/refs/tags/{tag}"))
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
        parts.append(intro)
        parts.append("")
    parts.append(body)
    print("\n".join(parts))


if __name__ == "__main__":
    main()
