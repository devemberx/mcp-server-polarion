"""Tool-coverage gate: every registered MCP tool must be exercised by at least
one eval case (declared via each case's ``covers``) or be explicitly deferred
with a reason. Adding a tool without an eval case fails CI unless it is listed
in ``DEFERRED``. ``EXPECTED_TOOL_NAMES`` (the transport test's registration
contract) is the single source of truth for the tool set.
"""

from __future__ import annotations

import pytest

# ``run`` imports ``strands_evals`` at load; skip on the bare dev install.
pytest.importorskip("strands_evals")

from evals.cases.triggers import CASES as TRIGGER_CASES
from evals.run import ALL_CASES
from tests.mcp_server_polarion.test_mcp_transport import EXPECTED_TOOL_NAMES

# Tools deliberately not yet eval-covered, each with a reason. Shrinks over time
# — remove an entry as soon as a case covers the tool (enforced below).
DEFERRED: dict[str, str] = {
    "get_sql_query_recipes": "pointer tool; exercised indirectly by EFF-SQL-NOT-LUCENE",
    "update_work_item_link": "niche edit op; low NL-trigger value, add later",
    "update_work_item_comment": "niche edit op; low NL-trigger value, add later",
}


def _covered() -> set[str]:
    return {t for case in ALL_CASES for t in (case.metadata or {}).get("covers", [])}


def test_every_tool_covered_or_deferred() -> None:
    gap = EXPECTED_TOOL_NAMES - _covered() - set(DEFERRED)
    assert not gap, f"tools with no eval case and not deferred: {sorted(gap)}"


def test_no_stale_deferred_entries() -> None:
    # A tool that is now covered must be removed from DEFERRED.
    stale = set(DEFERRED) & _covered()
    assert not stale, f"remove now-covered tools from DEFERRED: {sorted(stale)}"


def test_covers_only_names_real_tools() -> None:
    assert _covered() <= EXPECTED_TOOL_NAMES


def test_deferred_only_names_real_tools() -> None:
    assert set(DEFERRED) <= EXPECTED_TOOL_NAMES


def test_triggers_cases_cover_exactly_what_they_assert() -> None:
    # ``covers`` is otherwise an unverified claim. A triggers_tool case asserts
    # one tool family fires; its ``covers`` must name exactly that family, so a
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
