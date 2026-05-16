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
        polarion_url: Base URL of the Polarion instance root (without the
            '/polarion' context path).  Trailing slashes are accepted and
            will be stripped automatically.
        polarion_token: Personal access token for Bearer authentication.
        polarion_verify_ssl: Whether to verify TLS certificates.  Default
            ``True``.  Set ``False`` only for trusted internal instances
            using self-signed certificates.
    """

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
    )

    polarion_url: str = Field(
        description=(
            "Base URL of the Polarion instance root (e.g. 'https://example.com'), "
            "without the '/polarion' application context path. Trailing slashes "
            "are accepted and will be stripped automatically."
        ),
    )
    polarion_token: str = Field(
        description="Personal access token for Polarion REST API.",
    )
    polarion_verify_ssl: bool = Field(
        default=True,
        description=(
            "Verify TLS certificates for HTTPS connections. Set to False only "
            "for trusted internal Polarion instances using self-signed certificates."
        ),
    )

    @property
    def base_api_url(self) -> str:
        """Return the full REST API v1 base URL."""
        return f"{self.polarion_url.rstrip('/')}/polarion/rest/v1"
