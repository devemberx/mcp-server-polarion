"""Unit tests for the Tier-2 case definitions.

Mirrors the Tier-1 invariants at the efficiency threshold: every case must
name a registered check, carry ``min_pass_rate == 0.8``, and keep its name
unique across BOTH tiers (``run.py --case`` selects by name from the
concatenated list).
"""

from __future__ import annotations

import pytest

# The CASES list pulls in ``strands_evals.Case``, only present when the optional
# ``evals`` dependency group is installed; skip on the bare dev install.
pytest.importorskip("strands_evals")

from evals.cases.tier1_prohibitions import CASES as TIER1_CASES
from evals.cases.tier2_efficiency import CASES, _case
from evals.evaluators import checks


class TestCases:
    def test_every_case_check_is_registered(self) -> None:
        registry_keys = set(checks.REGISTRY)
        for case in CASES:
            metadata = case.metadata or {}
            assert metadata["check"] in registry_keys, (
                f"case '{case.name}' references missing check '{metadata['check']}'"
            )

    def test_every_case_uses_efficiency_threshold(self) -> None:
        for case in CASES:
            assert (case.metadata or {}).get("min_pass_rate") == 0.8

    def test_case_names_are_unique_across_tiers(self) -> None:
        names = [c.name for c in [*TIER1_CASES, *CASES]]
        assert len(names) == len(set(names))

    def test_helper_builds_expected_metadata_shape(self) -> None:
        case = _case("T2-X", "do a thing", "direct_read", foo="bar")
        assert case.name == "T2-X"
        assert case.input == "do a thing"
        assert case.metadata == {
            "check": "direct_read",
            "params": {"foo": "bar"},
            "min_pass_rate": 0.8,
        }
