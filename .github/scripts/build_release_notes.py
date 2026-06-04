#!/usr/bin/env python3
"""Build a GitHub Release body: GitHub's categorized auto-notes (grouped per
`.github/release.yml`) with a Highlights list prepended from the Features
section. Reads GITHUB_REPOSITORY / GITHUB_REF_NAME, prints markdown to stdout.

This is a CI helper (not part of the shipped package), so printing to stdout is
the intended contract — it does not fall under the server's no-print rule.
"""

from __future__ import annotations

import json
import os
import re
import subprocess

_PREFIX = re.compile(r"^[a-z]+(\([^)]*\))?!?:\s*")


def highlight(bullet: str) -> str:
    """Turn a '* feat(scope): summary by @user in .../pull/N' line into a
    '- Summary (#N)' Highlights entry."""
    title = re.split(r" by @", bullet[2:])[0]
    summary = _PREFIX.sub("", title).strip()
    summary = summary[:1].upper() + summary[1:] if summary else summary
    match = re.search(r"/pull/(\d+)", bullet)
    return f"- {summary} (#{match.group(1)})" if match else f"- {summary}"


def main() -> None:
    repo = os.environ["GITHUB_REPOSITORY"]
    tag = os.environ["GITHUB_REF_NAME"]

    result = subprocess.run(
        [
            "gh",
            "api",
            f"repos/{repo}/releases/generate-notes",
            "-X",
            "POST",
            "-f",
            f"tag_name={tag}",
        ],
        capture_output=True,
        text=True,
        check=True,
    )
    body = json.loads(result.stdout)["body"]

    current: str | None = None
    features: list[str] = []
    for line in body.splitlines():
        if line.startswith("### "):
            current = line[4:]
        elif line.startswith("* ") and current and "Features" in current:
            features.append(line)

    parts: list[str] = []
    if features:
        parts.append("## Highlights")
        parts.extend(highlight(b) for b in features)
        parts.append("")
    parts.append(body)
    print("\n".join(parts))


if __name__ == "__main__":
    main()
