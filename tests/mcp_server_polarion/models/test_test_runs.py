"""Tests for test run models in ``mcp_server_polarion.models.test_runs``."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from mcp_server_polarion.models import TestRunCreateSpec


class TestTestRunCreateSpec:
    def test_minimal_spec(self):
        spec = TestRunCreateSpec(id="RUN-1")
        assert spec.id == "RUN-1"
        assert spec.title is None
        assert spec.custom_fields is None

    def test_typo_key_rejected(self):
        # extra='forbid': a typo key must error, not silently drop the field.
        with pytest.raises(ValidationError, match="titel"):
            TestRunCreateSpec.model_validate({"id": "RUN-1", "titel": "oops"})
