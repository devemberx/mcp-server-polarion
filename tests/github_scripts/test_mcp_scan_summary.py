"""Unit tests for the mcp-scan summary renderer in `.github/scripts/`.

The script lives outside the package, so it is loaded by path via importlib.
Only the pure ``render`` function is covered; the ``main()`` file-I/O path is
left to the workflow itself.
"""

from __future__ import annotations

from pathlib import Path

from tests.conftest import load_module_from_path

SCRIPT = (
    Path(__file__).resolve().parents[2] / ".github" / "scripts" / "mcp_scan_summary.py"
)

mss = load_module_from_path(SCRIPT, "mcp_scan_summary")


class TestRender:
    def test_empty_results_render_header_and_marker(self) -> None:
        out = mss.render([])
        assert out.startswith(mss.MARKER)
        assert "Scanned **0** tools" in out

    def test_tally_row_counts_severities(self) -> None:
        results = [
            {"tool_name": "get_x", "findings": {"yara": {"severity": "SAFE"}}},
            {"tool_name": "get_y", "findings": {"yara": {"severity": "SAFE"}}},
        ]
        out = mss.render(results)
        assert "Scanned **2** tools" in out
        # SAFE column (first) shows 2 for the yara analyzer.
        assert "| yara | 2 | 0 | 0 | 0 | 0 |" in out

    def test_non_safe_finding_is_detailed(self) -> None:
        results = [
            {
                "name": "danger_tool",
                "findings": {
                    "yara": {
                        "severity": "HIGH",
                        "total_findings": 3,
                        "threat_names": ["prompt_injection", "exfil"],
                    }
                },
            }
        ]
        out = mss.render(results)
        assert "1 non-SAFE tool(s)" in out
        assert "| danger_tool | HIGH | 3 | prompt_injection, exfil |" in out

    def test_missing_name_falls_back_to_question_mark(self) -> None:
        results = [{"findings": {"yara": {"severity": "LOW", "total_findings": 1}}}]
        out = mss.render(results)
        assert "| ? | LOW | 1 |" in out
