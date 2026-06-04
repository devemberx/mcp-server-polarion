#!/usr/bin/env python3
"""Build a GitHub Release body: GitHub's categorized auto-notes (grouped per
`.github/release.yml`), prefixed with curated highlights from
`.github/release-notes/<tag>.md` when the deploy flow committed one. Without
that file the body is the categorized list alone. Reads GITHUB_REPOSITORY /
GITHUB_REF_NAME, prints markdown to stdout.

CI helper (not part of the shipped package), so printing to stdout is the
intended contract — it does not fall under the server's no-print rule.
"""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path


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

    curated = Path(".github/release-notes") / f"{tag}.md"
    intro = curated.read_text().strip() if curated.is_file() else ""

    parts: list[str] = []
    if intro:
        parts.append(intro)
        parts.append("")
    parts.append(body)
    print("\n".join(parts))


if __name__ == "__main__":
    main()
