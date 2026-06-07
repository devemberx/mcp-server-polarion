"""Tests for project models in ``mcp_server_polarion.models.projects``."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from mcp_server_polarion.models import ProjectSummary


class TestProjectSummary:
    def test_valid(self):
        p = ProjectSummary(id="myproject", name="My Project")
        assert p.id == "myproject"
        assert p.name == "My Project"

    def test_active_defaults_true(self):
        p = ProjectSummary(id="myproject", name="My Project")
        assert p.active is True

    def test_active_explicit_false(self):
        p = ProjectSummary(id="archived", name="Old Project", active=False)
        assert p.active is False

    def test_missing_id(self):
        with pytest.raises(ValidationError):
            ProjectSummary(name="No ID")  # type: ignore[call-arg]

    def test_missing_name(self):
        with pytest.raises(ValidationError):
            ProjectSummary(id="proj")  # type: ignore[call-arg]
