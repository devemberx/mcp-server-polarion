"""Tests for ``PolarionConfig`` — environment variable loading and validation."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from mcp_server_polarion.core.config import PolarionConfig


class TestPolarionConfigLoading:
    """Verify that config values are loaded and validated correctly."""

    def test_loads_from_explicit_kwargs(self) -> None:
        config = PolarionConfig(
            polarion_url="https://example.com",
            polarion_token="tok-123",
        )
        assert config.polarion_url == "https://example.com"
        assert config.polarion_token == "tok-123"

    def test_loads_from_env_vars(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("POLARION_URL", "https://env.example.com")
        monkeypatch.setenv("POLARION_TOKEN", "env-token")

        config = PolarionConfig()  # type: ignore[call-arg]
        assert config.polarion_url == "https://env.example.com"
        assert config.polarion_token == "env-token"

    def test_missing_url_raises_validation_error(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.delenv("POLARION_URL", raising=False)
        monkeypatch.delenv("POLARION_TOKEN", raising=False)

        with pytest.raises(ValidationError):
            PolarionConfig(_env_file=None)  # type: ignore[call-arg]

    def test_missing_token_raises_validation_error(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setenv("POLARION_URL", "https://example.com")
        monkeypatch.delenv("POLARION_TOKEN", raising=False)

        with pytest.raises(ValidationError):
            PolarionConfig(_env_file=None)  # type: ignore[call-arg]


class TestBaseApiUrl:
    """Verify ``base_api_url`` property construction."""

    def test_base_api_url_normal(self) -> None:
        config = PolarionConfig(
            polarion_url="https://polarion.corp.com",
            polarion_token="t",
        )
        assert (
            config.base_api_url
            == "https://polarion.corp.com/polarion/rest/v1"
        )

    def test_base_api_url_strips_single_trailing_slash(self) -> None:
        config = PolarionConfig(
            polarion_url="https://polarion.corp.com/",
            polarion_token="t",
        )
        assert (
            config.base_api_url
            == "https://polarion.corp.com/polarion/rest/v1"
        )

    def test_base_api_url_strips_multiple_trailing_slashes(self) -> None:
        config = PolarionConfig(
            polarion_url="https://polarion.corp.com///",
            polarion_token="t",
        )
        assert (
            config.base_api_url
            == "https://polarion.corp.com/polarion/rest/v1"
        )
