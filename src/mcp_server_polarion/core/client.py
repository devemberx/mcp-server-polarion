"""Async HTTP client for the Polarion REST API v1.

``PolarionClient`` wraps :class:`httpx.AsyncClient` with bearer auth,
JSON:API error mapping (401/403 → ``PolarionAuthError``, 404 →
``PolarionNotFoundError``, else ``PolarionError``), 429/5xx
exponential-backoff retry, and a post-mutation delay.
"""

from __future__ import annotations

import asyncio
import getpass
import logging
import re
import tempfile
import types
from pathlib import Path
from typing import Final

import httpx

from mcp_server_polarion.core.config import PolarionConfig
from mcp_server_polarion.core.exceptions import (
    PolarionAuthError,
    PolarionError,
    PolarionNotFoundError,
)
from mcp_server_polarion.core.global_pace import GlobalPacer

logger: Final = logging.getLogger("mcp_server_polarion.core.client")


def _default_pace_lock_path() -> str:
    """Host-shared lock path so every local server process paces together.

    Shared ``gettempdir()`` + username scope it per user on shared hosts.
    """
    try:
        user = getpass.getuser()
    except OSError:
        user = "default"
    return str(Path(tempfile.gettempdir()) / f"mcp-server-polarion-pace-{user}.lock")


_DEFAULT_PACE_LOCK_PATH: Final[str] = _default_pace_lock_path()

_MAX_RETRIES: Final[int] = 2
_INITIAL_BACKOFF_SECONDS: Final[float] = 1.0
_BACKOFF_MULTIPLIER: Final[float] = 2.0
_RETRYABLE_STATUS_CODES: Final[frozenset[int]] = frozenset({429, 500, 502, 503, 504})

# Pause after each mutation (Polarion forbids concurrent writes).
_WRITE_DELAY_SECONDS: Final[float] = 1.5
# Min gap between request starts → ≤3 req/s; start-based, so slow
# requests add no extra wait.
_MIN_REQUEST_INTERVAL_SECONDS: Final[float] = 1.0 / 3.0
_DEFAULT_TIMEOUT_SECONDS: Final[float] = 30.0

_HTTP_NO_CONTENT: Final[int] = 204
_HTTP_UNAUTHORIZED: Final[int] = 401
_HTTP_FORBIDDEN: Final[int] = 403
_HTTP_NOT_FOUND: Final[int] = 404

_MAX_ERROR_DETAIL_LEN: Final[int] = 200


def _extract_json_api_detail(body: object) -> str:
    """Concise detail from a JSON:API body: ``errors[*].detail``/``title``,
    else truncated body.
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
    """Strip HTML tags and truncate raw error text for safe display."""
    clean = re.sub(r"<[^>]+>", " ", raw)
    clean = " ".join(clean.split())
    if len(clean) > _MAX_ERROR_DETAIL_LEN:
        return clean[:_MAX_ERROR_DETAIL_LEN] + "\u2026"
    return clean


class PolarionClient:
    """Async HTTP client for the Polarion REST API; created once and reused
    for the MCP server lifetime (``lifespan`` context in ``server.py``).
    """

    def __init__(
        self,
        config: PolarionConfig,
        *,
        write_delay: float = _WRITE_DELAY_SECONDS,
        min_interval: float = _MIN_REQUEST_INTERVAL_SECONDS,
        pace_lock_path: str | None = _DEFAULT_PACE_LOCK_PATH,
    ) -> None:
        self.base_url: str = config.base_api_url
        self._write_delay = write_delay
        self._min_interval = min_interval
        # None (or min_interval=0) keeps host-global pacing a no-op for tests.
        self._global_pacer = GlobalPacer(pace_lock_path, min_interval)
        # -inf: first request never waits, whatever the clock epoch.
        self._last_request_monotonic: float = float("-inf")
        self._client = httpx.AsyncClient(
            base_url=self.base_url,
            headers={
                "Authorization": f"Bearer {config.polarion_token}",
                "Content-Type": "application/json",
                "Accept": "application/json",
            },
            timeout=httpx.Timeout(_DEFAULT_TIMEOUT_SECONDS),
            verify=config.polarion_verify_ssl,
        )
        # Lazily bound to running loop; serializes all calls
        # (no concurrency); not reentrant.
        self._request_lock: asyncio.Lock | None = None

    def _get_request_lock(self) -> asyncio.Lock:
        if self._request_lock is None:
            self._request_lock = asyncio.Lock()
        return self._request_lock

    async def _pace(self) -> None:
        """Block until ``_min_interval`` since the previous request issued. Caller
        holds the lock; :meth:`_request` stamps each attempt's issue time.
        """
        loop = asyncio.get_running_loop()
        wait = self._min_interval - (loop.time() - self._last_request_monotonic)
        if wait > 0:
            await asyncio.sleep(wait)

    async def __aenter__(self) -> PolarionClient:
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc_val: BaseException | None,
        exc_tb: types.TracebackType | None,
    ) -> None:
        await self.close()

    @property
    def is_closed(self) -> bool:
        """Whether the underlying HTTP transport has been closed."""
        return self._client.is_closed

    async def close(self) -> None:
        """Close the underlying ``httpx.AsyncClient``."""
        await self._client.aclose()

    async def get(
        self,
        path: str,
        *,
        params: dict[str, str | int] | None = None,
    ) -> dict[str, object]:
        """``GET``; raises ``PolarionAuthError`` (401/403),
        ``PolarionNotFoundError`` (404), ``PolarionError`` (other non-2xx).
        """
        async with self._get_request_lock(), self._global_pacer.hold():
            return await self._request("GET", path, params=params)

    async def post(
        self,
        path: str,
        *,
        json: dict[str, object] | None = None,
    ) -> dict[str, object]:
        """``POST``; sleeps ``_write_delay`` after success, inside the lock,
        for cluster propagation before the next call.
        """
        async with self._get_request_lock(), self._global_pacer.hold():
            result = await self._request("POST", path, json=json)
            await asyncio.sleep(self._write_delay)
            return result

    async def patch(
        self,
        path: str,
        *,
        json: dict[str, object] | None = None,
    ) -> dict[str, object]:
        """``PATCH``; same post-success delay contract as :meth:`post`."""
        async with self._get_request_lock(), self._global_pacer.hold():
            result = await self._request("PATCH", path, json=json)
            await asyncio.sleep(self._write_delay)
            return result

    async def delete(
        self,
        path: str,
        *,
        json: dict[str, object] | None = None,
    ) -> dict[str, object]:
        """``DELETE``; same delay contract as :meth:`post`. ``json`` carries
        bulk-delete ids — non-standard for DELETE, but httpx and Polarion's
        gateway both accept it. ``{}`` for 204 No Content.
        """
        async with self._get_request_lock(), self._global_pacer.hold():
            result = await self._request("DELETE", path, json=json)
            await asyncio.sleep(self._write_delay)
            return result

    async def _request(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, str | int] | None = None,
        json: dict[str, object] | None = None,
    ) -> dict[str, object]:
        """Execute with error mapping; retries 429/5xx up to ``_MAX_RETRIES``
        with exponential backoff, other errors raise immediately.
        """
        # Lock held across retries — releasing mid-backoff would let another caller
        # slip in and hit the same 429. Pace before first attempt; backoffs widen gap.
        await self._pace()
        last_exception: PolarionError | None = None
        backoff = _INITIAL_BACKOFF_SECONDS
        loop = asyncio.get_running_loop()

        for attempt in range(_MAX_RETRIES + 1):
            # Stamp per attempt so the next request paces from the last one
            # sent, not the stale first.
            self._last_request_monotonic = loop.time()
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
                if response.status_code == _HTTP_NO_CONTENT or not response.content:
                    return {}
                body: object = response.json()
                if not isinstance(body, dict):
                    return {"data": body}
                return body

            error = self._map_status_to_error(response)

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

        if last_exception is not None:
            raise last_exception

        raise PolarionError(  # pragma: no cover
            "Unexpected retry loop exit",
            status_code=0,
        )

    @staticmethod
    def _map_status_to_error(response: httpx.Response) -> PolarionError:
        """Map a non-2xx response to the matching ``PolarionError`` subclass."""
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
