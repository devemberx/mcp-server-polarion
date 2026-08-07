"""``PolarionConfig`` — env var loading and validation."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from mcp_server_polarion.core.config import (
    MIN_NONZERO_REQUESTS_PER_SECOND,
    PolarionConfig,
)


class TestPolarionConfigLoading:
    """Config values loaded and validated correctly."""

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

    def test_verify_ssl_defaults_to_true(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.delenv("POLARION_VERIFY_SSL", raising=False)
        config = PolarionConfig(
            polarion_url="https://example.com",
            polarion_token="t",
            _env_file=None,  # type: ignore[call-arg]
        )
        assert config.polarion_verify_ssl is True

    def test_verify_ssl_reads_from_env(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setenv("POLARION_URL", "https://example.com")
        monkeypatch.setenv("POLARION_TOKEN", "t")
        monkeypatch.setenv("POLARION_VERIFY_SSL", "false")

        config = PolarionConfig()  # type: ignore[call-arg]
        assert config.polarion_verify_ssl is False


class TestMaxRequestsPerSecond:
    """``polarion_max_requests_per_second`` — default, env override, validation."""

    def test_defaults_to_1(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("POLARION_MAX_REQUESTS_PER_SECOND", raising=False)
        config = PolarionConfig(
            polarion_url="https://example.com",
            polarion_token="t",
            _env_file=None,  # type: ignore[call-arg]
        )
        assert config.polarion_max_requests_per_second == 1.0

    def test_reads_from_env(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("POLARION_URL", "https://example.com")
        monkeypatch.setenv("POLARION_TOKEN", "t")
        # Differ from default — else env-read indistinguishable from fallback.
        monkeypatch.setenv("POLARION_MAX_REQUESTS_PER_SECOND", "5")

        config = PolarionConfig()  # type: ignore[call-arg]
        assert config.polarion_max_requests_per_second == 5.0

    def test_zero_allowed_as_unlimited(self) -> None:
        """0 = no cap sentinel, not a validation error."""
        config = PolarionConfig(
            polarion_url="https://example.com",
            polarion_token="t",
            polarion_max_requests_per_second=0,
            _env_file=None,  # type: ignore[call-arg]
        )
        assert config.polarion_max_requests_per_second == 0.0

    def test_rejects_negative(self) -> None:
        with pytest.raises(ValidationError):
            PolarionConfig(
                polarion_url="https://example.com",
                polarion_token="t",
                polarion_max_requests_per_second=-1,
                _env_file=None,  # type: ignore[call-arg]
            )

    def test_blank_env_value_falls_back_to_default(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """``VAR=`` (compose/env_file idiom) must not abort server startup."""
        monkeypatch.setenv("POLARION_URL", "https://example.com")
        monkeypatch.setenv("POLARION_TOKEN", "t")
        monkeypatch.setenv("POLARION_MAX_REQUESTS_PER_SECOND", "")

        config = PolarionConfig()  # type: ignore[call-arg]
        assert config.polarion_max_requests_per_second == 1.0

    def test_whitespace_env_value_falls_back_to_default(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setenv("POLARION_URL", "https://example.com")
        monkeypatch.setenv("POLARION_TOKEN", "t")
        monkeypatch.setenv("POLARION_MAX_REQUESTS_PER_SECOND", "  ")

        config = PolarionConfig()  # type: ignore[call-arg]
        assert config.polarion_max_requests_per_second == 1.0

    def test_rejects_non_numeric(self) -> None:
        with pytest.raises(ValidationError):
            PolarionConfig(
                polarion_url="https://example.com",
                polarion_token="t",
                polarion_max_requests_per_second="fast",  # type: ignore[arg-type]
                _env_file=None,  # type: ignore[call-arg]
            )

    @pytest.mark.parametrize("value", ["inf", "Infinity", "-inf", "nan", "1e400"])
    def test_rejects_non_finite(
        self,
        value: str,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """``inf`` would divide to a 0 interval — second silent no-cap path."""
        monkeypatch.setenv("POLARION_URL", "https://example.com")
        monkeypatch.setenv("POLARION_TOKEN", "t")
        monkeypatch.setenv("POLARION_MAX_REQUESTS_PER_SECOND", value)

        with pytest.raises(ValidationError):
            PolarionConfig()  # type: ignore[call-arg]

    @pytest.mark.parametrize("value", [0.001, 1e-320])
    def test_rejects_rate_below_floor(self, value: float) -> None:
        """Interval past request timeout = hang; fail at startup instead."""
        with pytest.raises(ValidationError, match="one request per 60 s"):
            PolarionConfig(
                polarion_url="https://example.com",
                polarion_token="t",
                polarion_max_requests_per_second=value,
                _env_file=None,  # type: ignore[call-arg]
            )

    def test_floor_value_accepted(self) -> None:
        config = PolarionConfig(
            polarion_url="https://example.com",
            polarion_token="t",
            polarion_max_requests_per_second=MIN_NONZERO_REQUESTS_PER_SECOND,
            _env_file=None,  # type: ignore[call-arg]
        )
        assert config.polarion_max_requests_per_second == pytest.approx(1 / 60)


class TestBaseApiUrl:
    """``base_api_url`` property construction."""

    def test_base_api_url_normal(self) -> None:
        config = PolarionConfig(
            polarion_url="https://polarion.corp.com",
            polarion_token="t",
        )
        assert config.base_api_url == "https://polarion.corp.com/polarion/rest/v1"

    def test_base_api_url_strips_single_trailing_slash(self) -> None:
        config = PolarionConfig(
            polarion_url="https://polarion.corp.com/",
            polarion_token="t",
        )
        assert config.base_api_url == "https://polarion.corp.com/polarion/rest/v1"

    def test_base_api_url_strips_multiple_trailing_slashes(self) -> None:
        config = PolarionConfig(
            polarion_url="https://polarion.corp.com///",
            polarion_token="t",
        )
        assert config.base_api_url == "https://polarion.corp.com/polarion/rest/v1"
