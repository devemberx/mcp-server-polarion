"""Gate osv-scanner findings on what PR introduce over its base.

Absolute gate (any CVE in head = red) deadlock when two CVEs get fixed in
separate PRs: each stay red on the other's CVE. PR run pass `--base` -> only
head-only vulns fail, pre-existing ones warn. No `--base` (push / schedule)
= full audit, every finding fail, so main's own CVEs stay visible.

Exit 0 = pass, 1 = blocking vulns, 2 = unreadable or drifted report (fail closed).
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
    results = data.get("results")
    # Clean scan still emit `results: []`; absent/non-list = schema drift.
    if not isinstance(results, list):
        raise ValueError("scan report has no `results` list")
    findings: list[Finding] = []
    for result in _as_list(results):
        for pkg in _as_list(result.get("packages")):
            info = pkg.get("package")
            info = info if isinstance(info, dict) else {}
            name = str(info.get("name", "?"))
            version = str(info.get("version", "?"))
            groups = _as_list(pkg.get("groups"))
            if groups:
                for group in groups:
                    # Shared CVE id of twin advisories live in `aliases`, not `ids`.
                    raw = _strs(group.get("ids")) + _strs(group.get("aliases"))
                    ids = tuple(dict.fromkeys(raw))
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
    sections: tuple[tuple[str, list[Finding]], ...]
    if baseline:
        lines.append(
            f"**{len(new)} new** (blocking) · {len(existing)} pre-existing on base"
        )
        sections = (("New (blocking)", new), ("Pre-existing on base", existing))
    else:
        # No base scanned: "new" would mislabel main's own CVEs as introduced.
        lines.append(f"**{len(new)} found** (blocking)")
        sections = (("Found (blocking)", new),)
    for title, items in sections:
        if items:
            lines += ["", f"### {title}", "", *_table(items)]
    return "\n".join(lines) + "\n"


def render_error(exc: Exception) -> str:
    return f"## Dependency Audit (osv-scanner)\n\nUnreadable scan report: {exc}\n"


def _describe(f: Finding) -> str:
    return f"{f.package} {f.version}: {', '.join(f.ids)} (severity {f.severity})"


def _load(path: Path, rc: int | None) -> list[Finding]:
    findings = parse_report(json.loads(path.read_text()))
    # Scanner saw vulns, parser none = drift inside `results`; key check miss it.
    if rc == 1 and not findings:
        raise ValueError(f"{path}: osv-scanner exit 1 but no findings parsed")
    return findings


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Gate osv-scanner findings.")
    parser.add_argument("head", type=Path, help="osv-scanner JSON for PR head")
    parser.add_argument("--base", type=Path, help="osv-scanner JSON for PR base")
    parser.add_argument("--head-rc", type=int, help="osv-scanner exit code, head")
    parser.add_argument("--base-rc", type=int, help="osv-scanner exit code, base")
    parser.add_argument("--summary", type=Path, default=Path("osv-summary.md"))
    args = parser.parse_args(argv)
    try:
        head = _load(args.head, args.head_rc)
        base = _load(args.base, args.base_rc) if args.base else None
    except (OSError, ValueError) as exc:  # JSONDecodeError subclass ValueError
        print(f"::error title=osv audit::unreadable scan report: {exc}")
        args.summary.write_text(render_error(exc))
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
