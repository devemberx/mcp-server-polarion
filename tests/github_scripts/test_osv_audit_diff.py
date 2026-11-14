"""osv audit diff tests, loaded by path via importlib."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from tests.conftest import load_module_from_path

SCRIPT = (
    Path(__file__).resolve().parents[2] / ".github" / "scripts" / "osv_audit_diff.py"
)

oad = load_module_from_path(SCRIPT, "osv_audit_diff")

DNSPYTHON = ("dnspython", "2.9.0", ["GHSA-vvf4-9x63-8xvv"], "7.5")
OTEL = ("opentelemetry-api", "1.45.1", ["GHSA-mf5v-w3w3-f6cg"], "5.3")
MCP = ("mcp", "2.1.1", ["GHSA-84m7-p3x7-pcfv", "PYSEC-2026-1"], "7.5")


def report(*packages: tuple[str, str, list[str], str]) -> dict[str, object]:
    """Build osv-scanner JSON shape: results[].packages[].groups[]."""
    return {
        "results": [
            {
                "source": {"path": "runtime-requirements.txt", "type": "lockfile"},
                "packages": [
                    {
                        "package": {
                            "name": name,
                            "version": version,
                            "ecosystem": "PyPI",
                        },
                        "vulnerabilities": [{"id": i} for i in ids],
                        "groups": [{"ids": ids, "max_severity": severity}],
                    }
                    for name, version, ids, severity in packages
                ],
            }
        ]
    }


class TestParseReport:
    def test_one_finding_per_group(self) -> None:
        findings = oad.parse_report(report(MCP))
        assert findings == [
            oad.Finding(
                package="mcp",
                version="2.1.1",
                ids=("GHSA-84m7-p3x7-pcfv", "PYSEC-2026-1"),
                severity="7.5",
            )
        ]

    @pytest.mark.parametrize("data", [{}, {"results": None}, {"results": []}])
    def test_clean_scan_yields_nothing(self, data: dict[str, object]) -> None:
        assert oad.parse_report(data) == []

    def test_non_object_report_rejected(self) -> None:
        with pytest.raises(ValueError, match="JSON object"):
            oad.parse_report(["not", "a", "report"])

    def test_vulns_without_groups_still_reported(self) -> None:
        # Format drift must not silently pass: fall back to raw vulnerabilities.
        data = {
            "results": [
                {
                    "packages": [
                        {
                            "package": {"name": "dnspython", "version": "2.9.0"},
                            "vulnerabilities": [
                                {"id": "GHSA-vvf4-9x63-8xvv", "aliases": ["CVE-1"]}
                            ],
                        }
                    ]
                }
            ]
        }
        [finding] = oad.parse_report(data)
        assert finding.ids == ("GHSA-vvf4-9x63-8xvv", "CVE-1")
        assert finding.severity == "?"


class TestSplit:
    def test_no_base_means_every_finding_is_new(self) -> None:
        head = oad.parse_report(report(DNSPYTHON))
        new, existing = oad.split(head, None)
        assert new == head
        assert existing == []

    def test_cve_fix_pr_passes_despite_other_base_cve(self) -> None:
        # Deadlock replay: base carries both CVEs; PR fixes dnspython only.
        base = oad.parse_report(report(DNSPYTHON, OTEL))
        head = oad.parse_report(report(OTEL))
        new, existing = oad.split(head, base)
        assert new == []
        assert [f.package for f in existing] == ["opentelemetry-api"]

    def test_downgrade_reintroducing_cves_is_new(self) -> None:
        # litellm bump that drags mcp back onto a vulnerable release.
        base = oad.parse_report(report(DNSPYTHON))
        head = oad.parse_report(report(DNSPYTHON, MCP))
        new, existing = oad.split(head, base)
        assert [f.package for f in new] == ["mcp"]
        assert [f.package for f in existing] == ["dnspython"]

    def test_version_change_with_same_vuln_is_existing(self) -> None:
        base = oad.parse_report(report(DNSPYTHON))
        head = oad.parse_report(report(("dnspython", "2.9.1", *DNSPYTHON[2:])))
        new, _ = oad.split(head, base)
        assert new == []

    def test_alias_overlap_counts_as_existing(self) -> None:
        base = oad.parse_report(report(("mcp", "2.1.1", ["PYSEC-2026-1"], "7.5")))
        head = oad.parse_report(report(MCP))
        new, _ = oad.split(head, base)
        assert new == []

    def test_package_names_compare_normalized(self) -> None:
        base = oad.parse_report(report(("Zope_Interface", "1", ["GHSA-x"], "5")))
        head = oad.parse_report(report(("zope.interface", "1", ["GHSA-x"], "5")))
        new, _ = oad.split(head, base)
        assert new == []

    def test_same_id_on_other_package_is_new(self) -> None:
        base = oad.parse_report(report(("fastmcp", "4.1.1", ["GHSA-x"], "6.8")))
        head = oad.parse_report(report(("fastmcp-slim", "4.1.1", ["GHSA-x"], "6.8")))
        new, _ = oad.split(head, base)
        assert [f.package for f in new] == ["fastmcp-slim"]


class TestRender:
    def test_clean_report(self) -> None:
        out = oad.render([], [], baseline=True)
        assert "No known vulnerabilities" in out

    def test_new_and_existing_sections(self) -> None:
        new = oad.parse_report(report(MCP))
        existing = oad.parse_report(report(OTEL))
        out = oad.render(new, existing, baseline=True)
        assert "**1 new** (blocking) · 1 pre-existing on base" in out
        assert (
            "| mcp | 2.1.1 | [GHSA-84m7-p3x7-pcfv](https://osv.dev/GHSA-84m7-p3x7-pcfv)"
            " | 7.5 |" in out
        )
        assert "| opentelemetry-api | 1.45.1 |" in out

    def test_no_baseline_wording(self) -> None:
        out = oad.render(oad.parse_report(report(DNSPYTHON)), [], baseline=False)
        assert "Full audit" in out


class TestMain:
    def write(self, path: Path, data: object) -> Path:
        path.write_text(json.dumps(data))
        return path

    def test_new_vuln_exits_1_with_error_annotation(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        head = self.write(tmp_path / "head.json", report(DNSPYTHON, MCP))
        base = self.write(tmp_path / "base.json", report(DNSPYTHON))
        summary = tmp_path / "summary.md"
        rc = oad.main([str(head), "--base", str(base), "--summary", str(summary)])
        out = capsys.readouterr().out
        assert rc == 1
        assert "::error title=New vulnerability::mcp 2.1.1" in out
        assert "::warning title=Pre-existing vulnerability::dnspython 2.9.0" in out
        assert "GHSA-84m7-p3x7-pcfv" in summary.read_text()

    def test_only_existing_vulns_exits_0(self, tmp_path: Path) -> None:
        head = self.write(tmp_path / "head.json", report(OTEL))
        base = self.write(tmp_path / "base.json", report(DNSPYTHON, OTEL))
        summary = tmp_path / "summary.md"
        rc = oad.main([str(head), "--base", str(base), "--summary", str(summary)])
        assert rc == 0

    def test_without_base_any_vuln_exits_1(self, tmp_path: Path) -> None:
        head = self.write(tmp_path / "head.json", report(OTEL))
        rc = oad.main([str(head), "--summary", str(tmp_path / "s.md")])
        assert rc == 1

    @pytest.mark.parametrize("content", [None, "", "{not json"])
    def test_unreadable_report_fails_closed(
        self,
        tmp_path: Path,
        capsys: pytest.CaptureFixture[str],
        content: str | None,
    ) -> None:
        head = tmp_path / "head.json"
        if content is not None:
            head.write_text(content)
        rc = oad.main([str(head), "--summary", str(tmp_path / "s.md")])
        assert rc == 2
        assert "::error title=osv audit::" in capsys.readouterr().out

    def test_unreadable_base_fails_closed(self, tmp_path: Path) -> None:
        head = self.write(tmp_path / "head.json", report())
        base = tmp_path / "base.json"
        base.write_text("{not json")
        rc = oad.main(
            [str(head), "--base", str(base), "--summary", str(tmp_path / "s.md")]
        )
        assert rc == 2
