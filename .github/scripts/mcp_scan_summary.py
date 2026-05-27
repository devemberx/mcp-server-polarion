"""Render the mcp-scanner JSON report into Markdown.

Emits scan-summary.md, reused verbatim for both the CI step summary and the
sticky PR comment so the two never drift.
"""

from __future__ import annotations

import collections
import json
from pathlib import Path

RESULT_PATH = Path("scan-result.json")
SUMMARY_PATH = Path("scan-summary.md")
MARKER = "<!-- mcp-scan -->"
SEVERITIES = ("SAFE", "LOW", "MEDIUM", "HIGH", "UNKNOWN")

Finding = tuple[str, str, int, str]
Tally = collections.Counter[str]


def render(results: list[dict[str, object]]) -> str:
    tally: dict[str, Tally] = collections.defaultdict(collections.Counter)
    flagged: dict[str, list[Finding]] = collections.defaultdict(list)
    for result in results:
        name = result.get("tool_name") or result.get("name") or "?"
        findings: dict[str, dict[str, object]] = result.get("findings", {})  # type: ignore[assignment]
        for analyzer, finding in findings.items():
            severity = str(finding.get("severity", "UNKNOWN"))
            tally[analyzer][severity] += 1
            if severity != "SAFE":
                threats = ", ".join(finding.get("threat_names", []))  # type: ignore[arg-type]
                total = int(finding.get("total_findings", 0))  # type: ignore[arg-type]
                flagged[analyzer].append((str(name), severity, total, threats))

    lines = [
        MARKER,
        "## MCP Security Scan",
        "",
        f"Scanned **{len(results)}** tools — analyzer: yara",
        "",
        "| Analyzer | " + " | ".join(s.title() for s in SEVERITIES) + " |",
        "|---|" + "---|" * len(SEVERITIES),
    ]
    for analyzer in sorted(tally):
        counts = tally[analyzer]
        row = " | ".join(str(counts[s]) for s in SEVERITIES)
        lines.append(f"| {analyzer} | {row} |")
    lines.append("")
    for analyzer, items in flagged.items():
        if not items:
            continue
        summary = f"{analyzer}: {len(items)} non-SAFE tool(s)"
        lines.append(f"<details><summary>{summary}</summary>")
        lines.append("")
        lines.append("| Tool | Severity | Findings | Threats |")
        lines.append("|---|---|---|---|")
        for name, sev, total, threats in items:
            lines.append(f"| {name} | {sev} | {total} | {threats} |")
        lines.append("")
        lines.append("</details>")
    return "\n".join(lines) + "\n"


def main() -> None:
    if not RESULT_PATH.is_file() or RESULT_PATH.stat().st_size == 0:
        empty = f"{MARKER}\n## MCP Security Scan\n\nNo scan output produced.\n"
        SUMMARY_PATH.write_text(empty)
        return
    data = json.loads(RESULT_PATH.read_text())
    SUMMARY_PATH.write_text(render(data.get("scan_results", [])))


if __name__ == "__main__":
    main()
