"""Async HTTP client for Polarion REST API v1.

``PolarionClient`` wrap :class:`httpx.AsyncClient`: bearer auth, JSON:API
error mapping (401/403 → ``PolarionAuthError``, 404 →
``PolarionNotFoundError``, else ``PolarionError``), 429/5xx
exponential-backoff retry, post-mutation delay.
"""

from __future__ import annotations

import asyncio
import logging
import re
import types
import uuid
from typing import Final

import httpx

from mcp_server_polarion.core.config import PolarionConfig
from mcp_server_polarion.core.exceptions import (
    PolarionAuthError,
    PolarionError,
    PolarionNotFoundError,
    PolarionResponseTooLargeError,
)

logger: Final = logging.getLogger("mcp_server_polarion.core.client")

_MAX_RETRIES: Final[int] = 2
_INITIAL_BACKOFF_SECONDS: Final[float] = 1.0
_BACKOFF_MULTIPLIER: Final[float] = 2.0
_RETRYABLE_STATUS_CODES: Final[frozenset[int]] = frozenset({429, 500, 502, 503, 504})

# Pause after each mutation (Polarion forbid concurrent writes).
_WRITE_DELAY_SECONDS: Final[float] = 1.5
# Start-based min gap → ≤3 req/s; slow request add no extra wait.
_MIN_REQUEST_INTERVAL_SECONDS: Final[float] = 1.0 / 3.0
_DEFAULT_TIMEOUT_SECONDS: Final[float] = 30.0

_HTTP_NO_CONTENT: Final[int] = 204
_HTTP_UNAUTHORIZED: Final[int] = 401
_HTTP_FORBIDDEN: Final[int] = 403
_HTTP_NOT_FOUND: Final[int] = 404

_MAX_ERROR_DETAIL_LEN: Final[int] = 200

# Vendor content endpoint 406 on client-wide JSON-only Accept default.
_BYTES_ACCEPT_HEADER: Final[str] = "application/octet-stream, application/json"


def _extract_json_api_detail(body: object) -> str:
    """Detail from JSON:API body: ``errors[*].detail``/``title``, else truncated."""
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
    """Strip HTML tags + truncate for safe display."""
    clean = re.sub(r"<[^>]+>", " ", raw)
    clean = " ".join(clean.split())
    if len(clean) > _MAX_ERROR_DETAIL_LEN:
        return clean[:_MAX_ERROR_DETAIL_LEN] + "\u2026"
    return clean


class PolarionClient:
    """Async Polarion REST client; one instance per server ``lifespan``."""

    def __init__(
        self,
        config: PolarionConfig,
        *,
        write_delay: float = _WRITE_DELAY_SECONDS,
        min_interval: float = _MIN_REQUEST_INTERVAL_SECONDS,
    ) -> None:
        self.base_url: str = config.base_api_url
        self._write_delay = write_delay
        self._min_interval = min_interval
        # -inf: first request never wait, any clock epoch.
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
        # Lazy-bound to running loop; serialize all calls; not reentrant.
        self._request_lock: asyncio.Lock | None = None

    def _get_request_lock(self) -> asyncio.Lock:
        if self._request_lock is None:
            self._request_lock = asyncio.Lock()
        return self._request_lock

    async def _pace(self) -> None:
        """Block until ``_min_interval`` since previous request (caller hold lock)."""
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
        """Whether underlying HTTP transport closed."""
        return self._client.is_closed

    async def close(self) -> None:
        """Close underlying ``httpx.AsyncClient``."""
        await self._client.aclose()

    async def get(
        self,
        path: str,
        *,
        params: dict[str, str | int] | None = None,
    ) -> dict[str, object]:
        """``GET``; raise ``PolarionAuthError`` (401/403),
        ``PolarionNotFoundError`` (404), ``PolarionError`` (other non-2xx).
        """
        async with self._get_request_lock():
            return await self._request("GET", path, params=params)

    async def get_bytes(self, path: str, *, max_bytes: int) -> bytes:
        """Streamed ``GET``; abort + raise ``PolarionResponseTooLargeError``
        once body cross ``max_bytes`` (client-side cap, never retried). Same
        error mapping/retry as :meth:`get` otherwise.
        """
        async with self._get_request_lock():
            return await self._request_bytes(path, max_bytes=max_bytes)

    async def post(
        self,
        path: str,
        *,
        json: dict[str, object] | None = None,
    ) -> dict[str, object]:
        """``POST``; sleep ``_write_delay`` in-lock after success —
        cluster propagation before next call.
        """
        async with self._get_request_lock():
            result = await self._request("POST", path, json=json)
            await asyncio.sleep(self._write_delay)
            return result

    async def post_multipart(
        self,
        path: str,
        *,
        data: dict[str, str],
        files: list[tuple[str, tuple[str, bytes, str]]],
    ) -> dict[str, object]:
        """``POST`` ``multipart/form-data``; same delay contract as :meth:`post`.
        ``files`` order-matched to ``data`` JSON — caller build the pairing.
        """
        async with self._get_request_lock():
            result = await self._request("POST", path, data=data, files=files)
            await asyncio.sleep(self._write_delay)
            return result

    async def patch(
        self,
        path: str,
        *,
        json: dict[str, object] | None = None,
    ) -> dict[str, object]:
        """``PATCH``; same post-success delay contract as :meth:`post`."""
        async with self._get_request_lock():
            result = await self._request("PATCH", path, json=json)
            await asyncio.sleep(self._write_delay)
            return result

    async def delete(
        self,
        path: str,
        *,
        json: dict[str, object] | None = None,
    ) -> dict[str, object]:
        """``DELETE``; same delay contract as :meth:`post`. ``json`` carry
        bulk-delete ids — non-standard for DELETE, httpx + Polarion gateway
        both accept. ``{}`` for 204 No Content.
        """
        async with self._get_request_lock():
            result = await self._request("DELETE", path, json=json)
            await asyncio.sleep(self._write_delay)
            return result

    async def _request(  # noqa: PLR0913
        self,
        method: str,
        path: str,
        *,
        params: dict[str, str | int] | None = None,
        json: dict[str, object] | None = None,
        data: dict[str, str] | None = None,
        files: list[tuple[str, tuple[str, bytes, str]]] | None = None,
    ) -> dict[str, object]:
        """Execute with error mapping; retry 429/5xx up to ``_MAX_RETRIES``
        with exponential backoff, other errors raise immediately. ``files``
        set → multipart body, ``data`` = its plain form fields.
        """
        # Lock held across retries — release mid-backoff = other caller hit same 429.
        # Pace before first attempt; backoffs widen gap.
        await self._pace()
        last_exception: PolarionError | None = None
        backoff = _INITIAL_BACKOFF_SECONDS
        loop = asyncio.get_running_loop()

        # Client-wide Content-Type: application/json stick on multipart (httpx
        # setdefault skip present key) — override per-request, own boundary.
        multipart_headers: dict[str, str] | None = None
        if files is not None:
            boundary = uuid.uuid4().hex
            multipart_headers = {
                "Content-Type": f"multipart/form-data; boundary={boundary}",
            }

        for attempt in range(_MAX_RETRIES + 1):
            # Stamp per attempt — next request pace from last sent, not stale first.
            self._last_request_monotonic = loop.time()
            try:
                response = await self._client.request(
                    method,
                    path,
                    params=params,
                    json=json,
                    data=data,
                    files=files,
                    headers=multipart_headers,
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

    async def _request_bytes(self, path: str, *, max_bytes: int) -> bytes:
        """Stream GET behind :meth:`get_bytes`; retry 429/5xx like
        :meth:`_request`.
        """
        # _request duplicate: body arrive streamed, cap check mid-accumulation.
        await self._pace()
        last_exception: PolarionError | None = None
        backoff = _INITIAL_BACKOFF_SECONDS
        loop = asyncio.get_running_loop()

        for attempt in range(_MAX_RETRIES + 1):
            self._last_request_monotonic = loop.time()
            try:
                async with self._client.stream(
                    "GET",
                    path,
                    headers={"Accept": _BYTES_ACCEPT_HEADER},
                ) as response:
                    if not response.is_success:
                        # Error body needed for _map_status_to_error below.
                        await response.aread()
                    else:
                        chunks: list[bytes] = []
                        total = 0
                        async for chunk in response.aiter_bytes():
                            total += len(chunk)
                            if total > max_bytes:
                                raise PolarionResponseTooLargeError(
                                    f"Response from {path} exceeded "
                                    f"{max_bytes} byte cap before "
                                    "completing.",
                                    limit=max_bytes,
                                )
                            chunks.append(chunk)
                        return b"".join(chunks)
            except httpx.HTTPError as exc:
                raise PolarionError(
                    f"HTTP transport error: {exc}",
                    status_code=0,
                ) from exc

            error = self._map_status_to_error(response)

            is_retryable = response.status_code in _RETRYABLE_STATUS_CODES
            if is_retryable and attempt < _MAX_RETRIES:
                logger.warning(
                    "Retryable error %d on GET %s (attempt %d/%d). Backing off %.1f s.",
                    response.status_code,
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
        """Map non-2xx response to matching ``PolarionError`` subclass."""
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
