"""Polarion configuration loaded from environment variables.

All secrets live in ``.env`` (local dev) or real environment variables
(CI / production).  Never hardcode credentials.
"""

from __future__ import annotations

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class PolarionConfig(BaseSettings):
    """Environment-based configuration for the Polarion MCP server.

    Attributes:
        polarion_url: Base URL of the Polarion instance.  Trailing slashes
            are accepted and will be stripped automatically.
        polarion_token: Personal access token for Bearer authentication.
    """

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
    )

    polarion_url: str = Field(
        description=(
            "Base URL of the Polarion instance. "
            "Trailing slashes are accepted and will be stripped automatically."
        ),
    )
    polarion_token: str = Field(
        description="Personal access token for Polarion REST API.",
    )

    @property
    def base_api_url(self) -> str:
        """Return the full REST API v1 base URL."""
        return f"{self.polarion_url.rstrip('/')}/polarion/rest/v1"
