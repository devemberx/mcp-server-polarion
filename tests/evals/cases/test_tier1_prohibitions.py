"""Tier-1 case-definition invariants: every case names a check that exists in
``checks.REGISTRY`` and stays zero-tolerance (``min_pass_rate == 1.0``)."""

from __future__ import annotations

import pytest

# The CASES list pulls in ``strands_evals.Case``, only present when the optional
# ``evals`` dependency group is installed; skip on the bare dev install.
pytest.importorskip("strands_evals")

from evals.cases.tier1_prohibitions import CASES, _case
from evals.evaluators import checks


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

    def test_case_names_are_unique(self) -> None:
        names = [c.name for c in CASES]
        assert len(names) == len(set(names))

    def test_helper_builds_expected_metadata_shape(self) -> None:
        case = _case("T1-X", "do a thing", "readonly", foo="bar")
        assert case.name == "T1-X"
        assert case.input == "do a thing"
        assert case.metadata == {
            "check": "readonly",
            "params": {"foo": "bar"},
            "min_pass_rate": 1.0,
        }
