"""Tool-coverage gate: every registered MCP tool exercised by at least one
eval case (declared via case ``covers``) or explicitly deferred with reason.
New tool without eval case fail CI unless listed in ``DEFERRED``.
``EXPECTED_TOOL_NAMES`` (transport-test registration contract) = single
source of truth for the tool set.
"""

from __future__ import annotations

import pytest

# ``run`` import ``strands_evals`` at load; skip on bare dev install.
pytest.importorskip("strands_evals")

from evals.cases.triggers import CASES as TRIGGER_CASES
from evals.run import ALL_CASES
from tests.mcp_server_polarion.test_mcp_transport import EXPECTED_TOOL_NAMES

# Tools deliberately not yet eval-covered, each with reason. Shrink over time
# — remove entry as soon as a case covers the tool (enforced below).
DEFERRED: dict[str, str] = {}


def _covered() -> set[str]:
    return {t for case in ALL_CASES for t in (case.metadata or {}).get("covers", [])}


def test_every_tool_covered_or_deferred() -> None:
    gap = EXPECTED_TOOL_NAMES - _covered() - set(DEFERRED)
    assert not gap, f"tools with no eval case and not deferred: {sorted(gap)}"


def test_no_stale_deferred_entries() -> None:
    # Tool now covered must leave DEFERRED.
    stale = set(DEFERRED) & _covered()
    assert not stale, f"remove now-covered tools from DEFERRED: {sorted(stale)}"


def test_covers_only_names_real_tools() -> None:
    assert _covered() <= EXPECTED_TOOL_NAMES


def test_deferred_only_names_real_tools() -> None:
    assert set(DEFERRED) <= EXPECTED_TOOL_NAMES


def test_triggers_cases_cover_exactly_what_they_assert() -> None:
    # ``covers`` otherwise = unverified claim. triggers_tool case assert one
    # tool family fire; its ``covers`` must name exactly that family, so a
    # case can't bank coverage for a tool its check never asserts.
    for case in TRIGGER_CASES:
        meta = case.metadata or {}
        if meta.get("check") != "triggers_tool":
            continue
        raw = meta.get("params", {}).get("expect", [])
        expect = {raw} if isinstance(raw, str) else set(raw)
        covers = set(meta.get("covers", []))
        assert covers == expect, (
            f"{case.name}: covers {sorted(covers)} != expected tools {sorted(expect)}"
        )
