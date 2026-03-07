"""Async HTTP client for the Polarion REST API v1.

``PolarionClient`` wraps :class:`httpx.AsyncClient` with:

* **Bearer-token authentication** via default headers.
* **Automatic error mapping** — HTTP 401/403 → ``PolarionAuthError``,
  404 → ``PolarionNotFoundError``, others → ``PolarionError``.
* **Exponential-backoff retry** for transient failures (HTTP 429, 5xx)
  with a maximum of 2 retries.
* **Write-operation delay** — a configurable pause between sequential
  writes to account for Polarion cluster propagation (~3 s).
* **Pagination helper** — ``get_all_pages()`` transparently iterates all
  pages of a list endpoint.
"""

from __future__ import annotations

import asyncio
import logging
import re
import types
from typing import Final

import httpx

from mcp_server_polarion.core.config import PolarionConfig
from mcp_server_polarion.core.exceptions import (
    PolarionAuthError,
    PolarionError,
    PolarionNotFoundError,
)

logger: Final = logging.getLogger("mcp_server_polarion.core.client")

# Retry configuration -------------------------------------------------------
_MAX_RETRIES: Final[int] = 2
_INITIAL_BACKOFF_SECONDS: Final[float] = 1.0
_BACKOFF_MULTIPLIER: Final[float] = 2.0
_RETRYABLE_STATUS_CODES: Final[frozenset[int]] = frozenset({429, 500, 502, 503, 504})

# Write-delay configuration -------------------------------------------------
_WRITE_DELAY_SECONDS: Final[float] = 1.5

# Timeout configuration ------------------------------------------------------
_DEFAULT_TIMEOUT_SECONDS: Final[float] = 30.0

# HTTP status codes ----------------------------------------------------------
_HTTP_NO_CONTENT: Final[int] = 204
_HTTP_UNAUTHORIZED: Final[int] = 401
_HTTP_FORBIDDEN: Final[int] = 403
_HTTP_NOT_FOUND: Final[int] = 404

# Error detail ---------------------------------------------------------------
_MAX_ERROR_DETAIL_LEN: Final[int] = 200


def _extract_json_api_detail(body: object) -> str:
    """Extract a concise detail string from a JSON:API response body.

    Prefers ``errors[*].detail`` (or ``errors[*].title``) from the
    JSON:API error object array.  Falls back to a truncated string
    representation of the full body.

    Args:
        body: Decoded JSON response body.

    Returns:
        A human-readable error detail string, at most
        ``_MAX_ERROR_DETAIL_LEN`` characters.
    """
    if not isinstance(body, dict):
        return str(body)[:_MAX_ERROR_DETAIL_LEN]
    errors = body.get("errors")
    if isinstance(errors, list) and errors:
        details = [
            str(e.get("detail") or e.get("title") or "")
            for e in errors
            if isinstance(e, dict)
        ]
        text = "; ".join(d for d in details if d)
        if text:
            return text[:_MAX_ERROR_DETAIL_LEN]
    return str(body)[:_MAX_ERROR_DETAIL_LEN]


def _sanitize_error_text(raw: str) -> str:
    """Strip HTML tags and truncate raw error text for safe display.

    Args:
        raw: Raw response body text (may contain HTML).

    Returns:
        Plain text with HTML tags removed, at most
        ``_MAX_ERROR_DETAIL_LEN`` characters.  A trailing ellipsis (…)
        is appended when the text is truncated.
    """
    clean = re.sub(r"<[^>]+>", " ", raw)
    clean = " ".join(clean.split())
    if len(clean) > _MAX_ERROR_DETAIL_LEN:
        return clean[:_MAX_ERROR_DETAIL_LEN] + "\u2026"
    return clean


class PolarionClient:
    """Async HTTP client for the Polarion REST API.

    The client is designed to be created once and reused for the lifetime
    of the MCP server (managed via the ``lifespan`` context in
    ``server.py``).

    Usage::

        config = PolarionConfig()
        async with PolarionClient(config) as client:
            data = await client.get("/projects")

    Args:
        config: A ``PolarionConfig`` instance supplying URL and token.
        write_delay: Seconds to wait after each write operation
            (default 1.5 s).

    Attributes:
        base_url: The resolved REST API v1 base URL.
    """

    def __init__(
        self,
        config: PolarionConfig,
        *,
        write_delay: float = _WRITE_DELAY_SECONDS,
    ) -> None:
        self.base_url: str = config.base_api_url
        self._write_delay = write_delay
        self._client = httpx.AsyncClient(
            base_url=self.base_url,
            headers={
                "Authorization": f"Bearer {config.polarion_token}",
                "Content-Type": "application/json",
                "Accept": "application/json",
            },
            timeout=httpx.Timeout(_DEFAULT_TIMEOUT_SECONDS),
        )

    # -- Context-manager interface -------------------------------------------

    async def __aenter__(self) -> PolarionClient:
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc_val: BaseException | None,
        exc_tb: types.TracebackType | None,
    ) -> None:
        await self.close()

    async def close(self) -> None:
        """Close the underlying ``httpx.AsyncClient``."""
        await self._client.aclose()

    # -- Public HTTP helpers -------------------------------------------------

    async def get(
        self,
        path: str,
        *,
        params: dict[str, str | int] | None = None,
    ) -> dict[str, object]:
        """Send a ``GET`` request.

        Args:
            path: URL path relative to the base API URL (e.g. ``/projects``).
            params: Optional query parameters.

        Returns:
            Decoded JSON response body.

        Raises:
            PolarionAuthError: On HTTP 401/403.
            PolarionNotFoundError: On HTTP 404.
            PolarionError: On other non-2xx responses.
        """
        return await self._request("GET", path, params=params)

    async def post(
        self,
        path: str,
        *,
        json: dict[str, object] | None = None,
    ) -> dict[str, object]:
        """Send a ``POST`` request (write operation).

        A short delay is applied **after** the request succeeds to account
        for Polarion cluster propagation.

        Args:
            path: URL path relative to the base API URL.
            json: JSON request body.

        Returns:
            Decoded JSON response body.
        """
        result = await self._request("POST", path, json=json)
        await asyncio.sleep(self._write_delay)
        return result

    async def patch(
        self,
        path: str,
        *,
        json: dict[str, object] | None = None,
    ) -> dict[str, object]:
        """Send a ``PATCH`` request (write operation).

        A short delay is applied **after** the request succeeds.

        Args:
            path: URL path relative to the base API URL.
            json: JSON request body.

        Returns:
            Decoded JSON response body.
        """
        result = await self._request("PATCH", path, json=json)
        await asyncio.sleep(self._write_delay)
        return result

    # -- Pagination helper ---------------------------------------------------

    async def get_all_pages(
        self,
        path: str,
        *,
        params: dict[str, str | int] | None = None,
        page_size: int = 100,
    ) -> list[dict[str, object]]:
        """Fetch **all** pages of a paginated list endpoint.

        Iterates through pages until the returned ``data`` array is
        shorter than ``page_size`` or a ``links.next`` key is absent.

        Args:
            path: URL path relative to the base API URL.
            params: Additional query parameters (merged with pagination
                params on each request).
            page_size: Number of items per page (max 100).

        Returns:
            A flat list of all ``data`` items across every page.
        """
        all_items: list[dict[str, object]] = []
        page_number = 1
        merged_params: dict[str, str | int] = dict(params) if params else {}

        while True:
            merged_params["page[size]"] = page_size
            merged_params["page[number]"] = page_number

            response = await self.get(path, params=merged_params)

            data = response.get("data")
            if not isinstance(data, list):
                break
            all_items.extend(data)

            # Prefer ``links.next`` as the authoritative stop signal.  Only
            # fall back to the partial-page heuristic when the server omits
            # the ``links`` object entirely, because the server may cap
            # page sizes below the caller-requested value.
            links = response.get("links")
            if isinstance(links, dict):
                if "next" not in links:
                    break
            elif len(data) < page_size:
                break

            page_number += 1

        return all_items

    # -- Internal request engine ---------------------------------------------

    async def _request(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, str | int] | None = None,
        json: dict[str, object] | None = None,
    ) -> dict[str, object]:
        """Execute an HTTP request with retry and error mapping.

        Retries up to ``_MAX_RETRIES`` times on transient errors (429,
        5xx) using exponential backoff.  Non-retryable errors are raised
        immediately.

        Args:
            method: HTTP method (GET, POST, PATCH).
            path: URL path relative to the base API URL.
            params: Optional query parameters.
            json: Optional JSON body.

        Returns:
            Decoded JSON response body.

        Raises:
            PolarionAuthError: On HTTP 401/403.
            PolarionNotFoundError: On HTTP 404.
            PolarionError: On other non-2xx responses after all retries
                are exhausted.
        """
        last_exception: PolarionError | None = None
        backoff = _INITIAL_BACKOFF_SECONDS

        for attempt in range(_MAX_RETRIES + 1):
            try:
                response = await self._client.request(
                    method,
                    path,
                    params=params,
                    json=json,
                )
            except httpx.HTTPError as exc:
                raise PolarionError(
                    f"HTTP transport error: {exc}",
                    status_code=0,
                ) from exc

            if response.is_success:
                # Some responses (e.g. 204 No Content) have empty bodies.
                if response.status_code == _HTTP_NO_CONTENT or not response.content:
                    return {}
                body: object = response.json()
                if not isinstance(body, dict):
                    return {"data": body}
                return body

            # Map status codes to domain exceptions.
            error = self._map_status_to_error(response)

            # Retry only on transient errors.
            is_retryable = response.status_code in _RETRYABLE_STATUS_CODES
            if is_retryable and attempt < _MAX_RETRIES:
                logger.warning(
                    "Retryable error %d on %s %s (attempt %d/%d). Backing off %.1f s.",
                    response.status_code,
                    method,
                    path,
                    attempt + 1,
                    _MAX_RETRIES + 1,
                    backoff,
                )
                last_exception = error
                await asyncio.sleep(backoff)
                backoff *= _BACKOFF_MULTIPLIER
                continue

            raise error

        # All retries exhausted — raise the most recent error.
        if last_exception is not None:
            raise last_exception

        # Defensive: should never be reached.
        raise PolarionError(  # pragma: no cover
            "Unexpected retry loop exit",
            status_code=0,
        )

    # -- Error mapping -------------------------------------------------------

    @staticmethod
    def _map_status_to_error(response: httpx.Response) -> PolarionError:
        """Map an unsuccessful HTTP response to a domain exception.

        Args:
            response: The ``httpx.Response`` with non-2xx status.

        Returns:
            A ``PolarionError`` subclass matching the status code.
        """
        status = response.status_code
        try:
            detail: str = _extract_json_api_detail(response.json())
        except (ValueError, UnicodeDecodeError):
            detail = _sanitize_error_text(response.text)

        message = f"Polarion API error {status} {response.reason_phrase}: {detail}"

        if status in {_HTTP_UNAUTHORIZED, _HTTP_FORBIDDEN}:
            return PolarionAuthError(message, status_code=status)
        if status == _HTTP_NOT_FOUND:
            return PolarionNotFoundError(message, status_code=status)
        return PolarionError(message, status_code=status)
