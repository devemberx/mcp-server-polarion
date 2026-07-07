"""Polarion configuration from env vars; secrets never hardcoded."""

from __future__ import annotations

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


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

    @property
    def base_api_url(self) -> str:
        """Full REST API v1 base URL."""
        return f"{self.polarion_url.rstrip('/')}/polarion/rest/v1"
