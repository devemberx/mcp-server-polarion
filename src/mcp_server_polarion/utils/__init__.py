"""Pure utility functions — HTML conversion and sanitization."""

from __future__ import annotations

from mcp_server_polarion.utils.html import (
    first_anchorless_block,
    html_to_markdown,
    markdown_to_html,
    sanitize_html,
    stamp_block_ids,
)

__all__: list[str] = [
    "first_anchorless_block",
    "html_to_markdown",
    "markdown_to_html",
    "sanitize_html",
    "stamp_block_ids",
]
