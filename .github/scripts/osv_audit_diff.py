"""Gate osv-scanner findings on what PR introduce over its base.

Absolute gate (any CVE in head = red) deadlock when two CVEs get fixed in
separate PRs: each stay red on the other's CVE. PR run pass `--base` -> only
head-only vulns fail, pre-existing ones warn. No `--base` (push / schedule)
= full audit, every finding fail, so main's own CVEs stay visible.

Exit 0 = pass, 1 = blocking vulns, 2 = unreadable report (fail closed).
"""

from __future__ import annotations

import argparse
import json
import re
from pathlib import Path
from typing import NamedTuple


class Finding(NamedTuple):
    package: str
    version: str
    ids: tuple[str, ...]
    severity: str


def _norm(name: str) -> str:
    """PEP 503 name normalization."""
    return re.sub(r"[-_.]+", "-", name).lower()


def _as_list(value: object) -> list[dict[str, object]]:
    return [v for v in value if isinstance(v, dict)] if isinstance(value, list) else []


def _strs(value: object) -> list[str]:
    return [str(v) for v in value] if isinstance(value, list) else []


def parse_report(data: object) -> list[Finding]:
    """One finding per alias group (`groups[]` collapse GHSA/PYSEC/CVE twins)."""
    if not isinstance(data, dict):
        raise ValueError("scan report is not a JSON object")
    findings: list[Finding] = []
    for result in _as_list(data.get("results")):
        for pkg in _as_list(result.get("packages")):
            info = pkg.get("package")
            info = info if isinstance(info, dict) else {}
            name = str(info.get("name", "?"))
            version = str(info.get("version", "?"))
            groups = _as_list(pkg.get("groups"))
            if groups:
                for group in groups:
                    ids = tuple(_strs(group.get("ids")))
                    severity = str(group.get("max_severity") or "?")
                    findings.append(Finding(name, version, ids, severity))
                continue
            # Groups missing = format drift; raw vulns keep gate fail-closed.
            for vuln in _as_list(pkg.get("vulnerabilities")):
                ids = (str(vuln.get("id", "?")), *_strs(vuln.get("aliases")))
                findings.append(Finding(name, version, ids, "?"))
    return findings


def split(
    head: list[Finding], base: list[Finding] | None
) -> tuple[list[Finding], list[Finding]]:
    """(new, existing). Match on package + any shared id; version ignored —
    bump still hit by same advisory is not PR-introduced.
    """
    if base is None:
        return head, []
    seen = {(_norm(f.package), i) for f in base for i in f.ids}
    new: list[Finding] = []
    existing: list[Finding] = []
    for f in head:
        hit = any((_norm(f.package), i) in seen for i in f.ids)
        (existing if hit else new).append(f)
    return new, existing


def _table(findings: list[Finding]) -> list[str]:
    rows = ["| Package | Version | Vulnerability | Severity |", "|---|---|---|---|"]
    for f in findings:
        vid = f.ids[0] if f.ids else "?"
        link = f"[{vid}](https://osv.dev/{vid})"
        rows.append(f"| {f.package} | {f.version} | {link} | {f.severity} |")
    return rows


def render(new: list[Finding], existing: list[Finding], *, baseline: bool) -> str:
    lines = ["## Dependency Audit (osv-scanner)", ""]
    if baseline:
        lines.append(
            "Gate: only vulnerabilities this PR introduces over its base fail."
        )
    else:
        lines.append("Full audit (no PR base): every finding fails.")
    lines.append("")
    if not new and not existing:
        lines.append("No known vulnerabilities.")
        return "\n".join(lines) + "\n"
    lines.append(
        f"**{len(new)} new** (blocking) · {len(existing)} pre-existing on base"
    )
    for title, items in (("New (blocking)", new), ("Pre-existing on base", existing)):
        if items:
            lines += ["", f"### {title}", "", *_table(items)]
    return "\n".join(lines) + "\n"


def _describe(f: Finding) -> str:
    return f"{f.package} {f.version}: {', '.join(f.ids)} (severity {f.severity})"


def _load(path: Path) -> list[Finding]:
    return parse_report(json.loads(path.read_text()))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Gate osv-scanner findings.")
    parser.add_argument("head", type=Path, help="osv-scanner JSON for PR head")
    parser.add_argument("--base", type=Path, help="osv-scanner JSON for PR base")
    parser.add_argument("--summary", type=Path, default=Path("osv-summary.md"))
    args = parser.parse_args(argv)
    try:
        head = _load(args.head)
        base = _load(args.base) if args.base else None
    except (OSError, ValueError) as exc:  # JSONDecodeError subclass ValueError
        print(f"::error title=osv audit::unreadable scan report: {exc}")
        return 2
    new, existing = split(head, base)
    args.summary.write_text(render(new, existing, baseline=base is not None))
    for f in new:
        print(f"::error title=New vulnerability::{_describe(f)}")
    for f in existing:
        print(f"::warning title=Pre-existing vulnerability::{_describe(f)}")
    return 1 if new else 0


if __name__ == "__main__":
    raise SystemExit(main())
