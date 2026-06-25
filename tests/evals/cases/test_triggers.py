"""Trigger case-definition invariants: every case names a registered check,
stays zero-tolerance (``min_pass_rate == 1.0``), and documents intent + the
tools it covers.
"""

from __future__ import annotations

import pytest

# The CASES list pulls in ``strands_evals.Case``, only present when the optional
# ``evals`` dependency group is installed; skip on the bare dev install.
pytest.importorskip("strands_evals")

from evals.cases.triggers import CASES, _case
from evals.evaluators import checks
from tests.mcp_server_polarion.test_mcp_transport import EXPECTED_TOOL_NAMES


class TestCases:
    def test_every_case_check_is_registered(self) -> None:
        registry_keys = set(checks.REGISTRY)
        for case in CASES:
            metadata = case.metadata or {}
            assert metadata["check"] in registry_keys, (
                f"case '{case.name}' references missing check '{metadata['check']}'"
            )

    def test_every_case_is_zero_tolerance(self) -> None:
        for case in CASES:
            assert (case.metadata or {}).get("min_pass_rate") == 1.0

    def test_every_case_documents_intent(self) -> None:
        for case in CASES:
            intent = (case.metadata or {}).get("intent")
            assert isinstance(intent, str) and intent.strip(), case.name

    def test_every_case_declares_real_covers(self) -> None:
        for case in CASES:
            covers = (case.metadata or {}).get("covers")
            assert isinstance(covers, list) and covers, case.name
            assert set(covers) <= EXPECTED_TOOL_NAMES, case.name

    def test_helper_builds_expected_metadata_shape(self) -> None:
        case = _case(
            "TRIG-X",
            "do a thing",
            "triggers_tool",
            intent="x must y",
            covers=["create_work_items"],
            foo="bar",
        )
        assert case.name == "TRIG-X"
        assert case.input == "do a thing"
        assert case.metadata == {
            "check": "triggers_tool",
            "params": {"foo": "bar"},
            "min_pass_rate": 1.0,
            "intent": "x must y",
            "covers": ["create_work_items"],
        }
