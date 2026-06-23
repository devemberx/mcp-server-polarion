"""Tests for the enum option model in ``mcp_server_polarion.models.enum``."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from mcp_server_polarion.models import EnumOption


class TestEnumOption:
    def test_valid_full(self):
        opt = EnumOption(
            id="draft",
            name="Draft",
            description="Initial state",
            default=True,
            hidden=False,
            terminal=True,
        )
        assert opt.id == "draft"
        assert opt.name == "Draft"
        assert opt.description == "Initial state"
        assert opt.default is True
        assert opt.terminal is True

    def test_optional_flags_default(self):
        opt = EnumOption(id="open", name="Open")
        assert opt.description == ""
        assert opt.default is False
        assert opt.hidden is False
        assert opt.terminal is False

    def test_missing_id(self):
        with pytest.raises(ValidationError):
            EnumOption(name="No ID")  # type: ignore[call-arg]

    def test_missing_name(self):
        with pytest.raises(ValidationError):
            EnumOption(id="open")  # type: ignore[call-arg]
