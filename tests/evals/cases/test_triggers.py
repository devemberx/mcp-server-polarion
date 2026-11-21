"""Trigger case-definition invariants: every case names a registered check,
stays zero-tolerance (``min_pass_rate == 1.0``), and documents intent + the
tools it covers.
"""

from __future__ import annotations

import pytest

# CASES list pull in ``strands_evals.Case``, only present when optional
# ``evals`` dependency group installed; skip on bare dev install.
pytest.importorskip("strands_evals")

from evals.cases.triggers import CASES, _case
from evals.evaluators import checks
from tests.mcp_server_polarion.test_mcp_transport import EXPECTED_TOOL_NAMES

# Read tools a case may reject: other-resource sibling (misroute) or
# representation choice under test. Same-resource look-up before / after
# expected tool = legal (expect already fail missing route; waste =
# efficiency's job).
_READ_REJECTS: dict[str, set[str]] = {
    "TRIG-DOC-ATTACHMENT-CONTENT": {"get_work_item_attachment_content"},
    "TRIG-WI-ATTACHMENT-CONTENT": {"get_document_attachment_content"},
    "TRIG-TR-ATTACHMENT-CONTENT": {"get_work_item_attachment_content"},
    "TRIG-TEST-RECORD-ATTACHMENTS": {"list_work_item_attachments"},
    "TRIG-LIST-TEST-RUNS": {"list_work_items"},
    "TRIG-READ-NOT-GET": {"get_document", "read_document_parts"},
}


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

    def test_read_rejects_are_declared(self) -> None:
        for case in CASES:
            reject = set((case.metadata or {})["params"].get("reject", []))
            reads = reject - checks.WRITE_TOOLS
            assert reads <= _READ_REJECTS.get(str(case.name), set()), case.name

    def test_read_reject_declarations_not_stale(self) -> None:
        by_name = {str(c.name): c for c in CASES}
        for name, reads in _READ_REJECTS.items():
            reject = set((by_name[name].metadata or {})["params"].get("reject", []))
            assert reads <= reject, name

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
