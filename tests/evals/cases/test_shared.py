"""Shared-factory invariants: ``make_case`` assembles the one metadata shape
every category consumes, threading ``min_pass_rate`` and extra params through
verbatim.
"""

from __future__ import annotations

import pytest

pytest.importorskip("strands_evals")

from evals.cases._shared import make_case


class TestMakeCase:
    def test_builds_expected_metadata_shape(self) -> None:
        case = make_case(
            "X-1",
            "do a thing",
            "readonly",
            intent="stay read-only",
            covers=["get_document"],
            min_pass_rate=1.0,
            foo="bar",
        )
        assert case.name == "X-1"
        assert case.input == "do a thing"
        assert case.metadata == {
            "check": "readonly",
            "params": {"foo": "bar"},
            "min_pass_rate": 1.0,
            "intent": "stay read-only",
            "covers": ["get_document"],
        }

    def test_min_pass_rate_passes_through(self) -> None:
        case = make_case(
            "X-2",
            "p",
            "direct_read",
            intent="i",
            covers=["get_work_item"],
            min_pass_rate=0.8,
        )
        assert (case.metadata or {})["min_pass_rate"] == 0.8

    def test_no_extra_params_yields_empty_params(self) -> None:
        case = make_case(
            "X-3",
            "p",
            "readonly",
            intent="i",
            covers=["get_document"],
            min_pass_rate=1.0,
        )
        assert (case.metadata or {})["params"] == {}
