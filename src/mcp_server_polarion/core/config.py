"""Polarion configuration from env vars; secrets never hardcoded."""

from __future__ import annotations

from typing import Final

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

_DEFAULT_MAX_REQUESTS_PER_SECOND: Final[float] = 1.0

# Slowest cap still meaning "throttle" — 1 req/60 s. Below it, derived
# client interval outlast request timeout: hang, not pacing.
MIN_NONZERO_REQUESTS_PER_SECOND: Final[float] = 1.0 / 60.0


class PolarionConfig(BaseSettings):
    """Env-based configuration for Polarion MCP server."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        # Shared .env may hold unrelated secrets (eval OPENAI_API_KEY); ignore extras.
        extra="ignore",
    )

    polarion_url: str = Field(
        description=(
            "Polarion instance root URL (e.g. 'https://example.com'), without the "
            "'/polarion' context path; trailing slashes stripped."
        ),
    )
    polarion_token: str = Field(
        description="Personal access token for Polarion REST API.",
    )
    polarion_verify_ssl: bool = Field(
        default=True,
        description=(
            "Verify TLS certs; False only for trusted self-signed internal instances."
        ),
    )
    polarion_max_requests_per_second: float = Field(
        default=_DEFAULT_MAX_REQUESTS_PER_SECOND,
        ge=0,
        allow_inf_nan=False,
        description=(
            "Client-side request rate cap in requests per second; match your "
            "instance's server-side throttle. 0 disables client-side pacing. "
            "Writes keep a fixed post-write delay this cap does not lift."
        ),
    )

    @field_validator("polarion_max_requests_per_second", mode="before")
    @classmethod
    def _blank_rate_means_default(cls, value: object) -> object:
        """Empty env value (``VAR=``) fall back to default, never crash server."""
        if isinstance(value, str) and not value.strip():
            return _DEFAULT_MAX_REQUESTS_PER_SECOND
        return value

    @field_validator("polarion_max_requests_per_second", mode="after")
    @classmethod
    def _rate_not_below_floor(cls, value: float) -> float:
        """Reject rate so small its interval read as hang; 0 stay no-cap sentinel."""
        if value and value < MIN_NONZERO_REQUESTS_PER_SECOND:
            msg = (
                f"must be 0 (no cap) or at least "
                f"{MIN_NONZERO_REQUESTS_PER_SECOND:.4f} "
                f"(one request per 60 s); got {value!r}"
            )
            raise ValueError(msg)
        return value

    @property
    def base_api_url(self) -> str:
        """Full REST API v1 base URL."""
        return f"{self.polarion_url.rstrip('/')}/polarion/rest/v1"
