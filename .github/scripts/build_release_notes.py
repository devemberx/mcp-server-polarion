#!/usr/bin/env python3
"""Build a GitHub Release body for the CI publish workflow.

Combines two sections:
  1. ## Highlights — curated bullets from the annotated tag's message body
     (the deploy skill writes them into the tag; this script adds the heading).
  2. Auto-generated change notes — GitHub's categorized list per .github/release.yml.

Reads GITHUB_REPOSITORY and GITHUB_REF_NAME; prints markdown to stdout.
CI helper — printing to stdout is the intended contract here.
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
