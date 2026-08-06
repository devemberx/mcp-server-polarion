"""``PolarionClient`` tests via ``respx``; ``write_delay=0`` avoids sleeping."""

from __future__ import annotations

import asyncio
import json
from unittest.mock import patch

import httpx
import pytest
import respx

from mcp_server_polarion.core.client import (
    _MAX_ERROR_DETAIL_LEN,
    PolarionClient,
)
from mcp_server_polarion.core.config import PolarionConfig
from mcp_server_polarion.core.exceptions import (
    PolarionAuthError,
    PolarionError,
    PolarionNotFoundError,
    PolarionResponseTooLargeError,
)

BASE = "https://polarion.example.com/polarion/rest/v1"


def _config() -> PolarionConfig:
    return PolarionConfig(
        polarion_url="https://polarion.example.com",
        polarion_token="test-token",
    )


class TestAuthentication:
    """Client send correct ``Authorization`` header."""

    async def test_bearer_token_sent(self) -> None:
        """GET includes ``Authorization: Bearer <token>``."""
        with respx.mock(base_url=BASE) as mock:
            route = mock.get("/projects").mock(
                return_value=httpx.Response(200, json={"data": []}),
            )

            async with PolarionClient(
                _config(), write_delay=0, min_interval=0
            ) as client:
                await client.get("/projects")

            assert route.called
            request = route.calls.last.request
            assert request.headers["authorization"] == "Bearer test-token"

    async def test_content_type_json(self) -> None:
        """Requests set ``Content-Type: application/json``."""
        with respx.mock(base_url=BASE) as mock:
            route = mock.get("/projects").mock(
                return_value=httpx.Response(200, json={"data": []}),
            )

            async with PolarionClient(
                _config(), write_delay=0, min_interval=0
            ) as client:
                await client.get("/projects")

            request = route.calls.last.request
            assert request.headers["content-type"] == "application/json"


class TestSuccessfulResponses:
    """Parsing of 2xx responses."""

    async def test_get_returns_json_dict(self) -> None:
        with respx.mock(base_url=BASE) as mock:
            mock.get("/projects").mock(
                return_value=httpx.Response(
                    200,
                    json={
                        "data": [{"id": "proj1"}],
                        "meta": {"totalCount": 1},
                    },
                ),
            )

            async with PolarionClient(
                _config(), write_delay=0, min_interval=0
            ) as client:
                result = await client.get("/projects")

            assert result["data"] == [{"id": "proj1"}]

    async def test_get_with_query_params(self) -> None:
        with respx.mock(base_url=BASE) as mock:
            route = mock.get("/projects/P1/workitems").mock(
                return_value=httpx.Response(200, json={"data": []}),
            )

            async with PolarionClient(
                _config(), write_delay=0, min_interval=0
            ) as client:
                await client.get(
                    "/projects/P1/workitems",
                    params={"fields[workitems]": "title", "page[size]": 10},
                )

            request = route.calls.last.request
            assert "fields%5Bworkitems%5D=title" in str(request.url)

    async def test_post_returns_json(self) -> None:
        with respx.mock(base_url=BASE) as mock:
            mock.post("/projects/P1/workitems").mock(
                return_value=httpx.Response(
                    201,
                    json={"data": [{"id": "P1/WI-001"}]},
                ),
            )

            async with PolarionClient(
                _config(), write_delay=0, min_interval=0
            ) as client:
                result = await client.post(
                    "/projects/P1/workitems",
                    json={
                        "data": {
                            "type": "workitems",
                            "attributes": {"title": "T"},
                        },
                    },
                )

            assert result["data"] == [{"id": "P1/WI-001"}]

    async def test_patch_returns_empty_on_204(self) -> None:
        with respx.mock(base_url=BASE) as mock:
            mock.patch("/projects/P1/workitems/WI-001").mock(
                return_value=httpx.Response(204),
            )

            async with PolarionClient(
                _config(), write_delay=0, min_interval=0
            ) as client:
                result = await client.patch(
                    "/projects/P1/workitems/WI-001",
                    json={"data": {"attributes": {"title": "Updated"}}},
                )

            assert result == {}

    async def test_delete_returns_empty_on_204(self) -> None:
        with respx.mock(base_url=BASE) as mock:
            mock.delete("/projects/P1/workitems/WI-001/linkedworkitems").mock(
                return_value=httpx.Response(204),
            )

            async with PolarionClient(
                _config(), write_delay=0, min_interval=0
            ) as client:
                result = await client.delete(
                    "/projects/P1/workitems/WI-001/linkedworkitems",
                    json={
                        "data": [
                            {
                                "type": "linkedworkitems",
                                "id": "P1/WI-001/parent/P1/WI-002",
                            }
                        ]
                    },
                )

            assert result == {}

    async def test_delete_sends_json_body(self) -> None:
        """DELETE-with-body: JSON payload must reach wire."""
        with respx.mock(base_url=BASE) as mock:
            route = mock.delete("/projects/P1/workitems/WI-001/linkedworkitems").mock(
                return_value=httpx.Response(204)
            )

            async with PolarionClient(
                _config(), write_delay=0, min_interval=0
            ) as client:
                await client.delete(
                    "/projects/P1/workitems/WI-001/linkedworkitems",
                    json={
                        "data": [
                            {
                                "type": "linkedworkitems",
                                "id": "P1/WI-001/parent/P1/WI-002",
                            }
                        ]
                    },
                )

            request = route.calls.last.request
            sent = json.loads(request.content)
            assert sent == {
                "data": [
                    {
                        "type": "linkedworkitems",
                        "id": "P1/WI-001/parent/P1/WI-002",
                    }
                ]
            }

    async def test_non_dict_body_wrapped(self) -> None:
        """Response body list → wrap in ``{"data": ...}``."""
        with respx.mock(base_url=BASE) as mock:
            mock.get("/some/list").mock(
                return_value=httpx.Response(200, json=[1, 2, 3]),
            )

            async with PolarionClient(
                _config(), write_delay=0, min_interval=0
            ) as client:
                result = await client.get("/some/list")

            assert result == {"data": [1, 2, 3]}


class TestErrorMapping:
    """HTTP status → domain exception mapping."""

    async def test_401_raises_auth_error(self) -> None:
        with respx.mock(base_url=BASE) as mock:
            mock.get("/projects").mock(
                return_value=httpx.Response(
                    401,
                    json={"error": "Unauthorized"},
                ),
            )

            async with PolarionClient(
                _config(), write_delay=0, min_interval=0
            ) as client:
                with pytest.raises(PolarionAuthError) as exc_info:
                    await client.get("/projects")

            assert exc_info.value.status_code == 401

    async def test_403_raises_auth_error(self) -> None:
        with respx.mock(base_url=BASE) as mock:
            mock.get("/projects").mock(
                return_value=httpx.Response(
                    403,
                    json={"error": "Forbidden"},
                ),
            )

            async with PolarionClient(
                _config(), write_delay=0, min_interval=0
            ) as client:
                with pytest.raises(PolarionAuthError) as exc_info:
                    await client.get("/projects")

            assert exc_info.value.status_code == 403

    async def test_404_raises_not_found_error(self) -> None:
        with respx.mock(base_url=BASE) as mock:
            mock.get("/projects/MISSING/workitems/WI-999").mock(
                return_value=httpx.Response(
                    404,
                    json={"error": "Not Found"},
                ),
            )

            async with PolarionClient(
                _config(), write_delay=0, min_interval=0
            ) as client:
                with pytest.raises(PolarionNotFoundError) as exc_info:
                    await client.get("/projects/MISSING/workitems/WI-999")

            assert exc_info.value.status_code == 404

    async def test_delete_404_raises_not_found_error(self) -> None:
        with respx.mock(base_url=BASE) as mock:
            mock.delete("/projects/P1/workitems/WI-001/linkedworkitems").mock(
                return_value=httpx.Response(
                    404,
                    json={"error": "Not Found"},
                ),
            )

            async with PolarionClient(
                _config(), write_delay=0, min_interval=0
            ) as client:
                with pytest.raises(PolarionNotFoundError) as exc_info:
                    await client.delete(
                        "/projects/P1/workitems/WI-001/linkedworkitems",
                        json={
                            "data": [
                                {
                                    "type": "linkedworkitems",
                                    "id": "P1/WI-001/parent/P1/WI-999",
                                }
                            ]
                        },
                    )

            assert exc_info.value.status_code == 404

    async def test_400_raises_generic_error(self) -> None:
        with respx.mock(base_url=BASE) as mock:
            mock.post("/projects/P1/workitems").mock(
                return_value=httpx.Response(
                    400,
                    json={"error": "Bad Request"},
                ),
            )

            async with PolarionClient(
                _config(), write_delay=0, min_interval=0
            ) as client:
                with pytest.raises(PolarionError) as exc_info:
                    await client.post(
                        "/projects/P1/workitems",
                        json={"data": {}},
                    )

            assert exc_info.value.status_code == 400
            # Must NOT be a subclass like AuthError / NotFoundError.
            assert type(exc_info.value) is PolarionError

    async def test_error_message_includes_detail(self) -> None:
        with respx.mock(base_url=BASE) as mock:
            mock.get("/projects").mock(
                return_value=httpx.Response(
                    500,
                    json={"errors": [{"detail": "Internal"}]},
                ),
            )

            async with PolarionClient(
                _config(), write_delay=0, min_interval=0
            ) as client:
                with pytest.raises(PolarionError, match="Internal"):
                    await client.get("/projects")

    async def test_error_with_non_json_body(self) -> None:
        """Non-JSON error bodies still produce error message."""
        with respx.mock(base_url=BASE) as mock:
            mock.get("/projects").mock(
                return_value=httpx.Response(
                    401,
                    text="<html>Unauthorized</html>",
                ),
            )

            async with PolarionClient(
                _config(), write_delay=0, min_interval=0
            ) as client:
                with pytest.raises(PolarionAuthError, match="Unauthorized"):
                    await client.get("/projects")

    async def test_html_tags_stripped_from_error_message(self) -> None:
        """HTML in non-JSON error body stripped, not exposed raw."""
        with respx.mock(base_url=BASE) as mock:
            mock.get("/projects").mock(
                return_value=httpx.Response(
                    503,
                    text="<html><body><h1>Service Unavailable</h1></body></html>",
                ),
            )

            async with PolarionClient(
                _config(), write_delay=0, min_interval=0
            ) as client:
                with pytest.raises(PolarionError) as exc_info:
                    await client.get("/projects")

        message = str(exc_info.value)
        assert "<html>" not in message
        assert "Service Unavailable" in message

    async def test_long_error_body_is_truncated(self) -> None:
        """Long error detail capped at _MAX_ERROR_DETAIL_LEN."""
        long_text = "x" * 500
        with respx.mock(base_url=BASE) as mock:
            mock.get("/projects").mock(
                return_value=httpx.Response(
                    400,
                    json={"message": long_text},
                ),
            )

            async with PolarionClient(
                _config(), write_delay=0, min_interval=0
            ) as client:
                with pytest.raises(PolarionError) as exc_info:
                    await client.get("/projects")

        status_prefix = "Polarion API error 400 Bad Request: "
        detail_part = str(exc_info.value)[len(status_prefix) :]
        assert len(detail_part) <= _MAX_ERROR_DETAIL_LEN


class TestTransportErrors:
    """httpx transport errors wrapped in PolarionError."""

    async def test_connection_error_becomes_polarion_error(self) -> None:
        with respx.mock(base_url=BASE) as mock:
            mock.get("/projects").mock(
                side_effect=httpx.ConnectError("refused"),
            )

            async with PolarionClient(
                _config(), write_delay=0, min_interval=0
            ) as client:
                with pytest.raises(PolarionError, match="transport error"):
                    await client.get("/projects")

    async def test_timeout_error_becomes_polarion_error(self) -> None:
        with respx.mock(base_url=BASE) as mock:
            mock.get("/projects").mock(
                side_effect=httpx.ReadTimeout("timed out"),
            )

            async with PolarionClient(
                _config(), write_delay=0, min_interval=0
            ) as client:
                with pytest.raises(PolarionError, match="transport error"):
                    await client.get("/projects")


class TestRetry:
    """Retry behaviour on 429 and 5xx."""

    async def test_retries_on_429_then_succeeds(self) -> None:
        """First request → 429, second → 200."""
        with respx.mock(base_url=BASE) as mock:
            route = mock.get("/projects").mock(
                side_effect=[
                    httpx.Response(429, json={"error": "Too Many Requests"}),
                    httpx.Response(200, json={"data": []}),
                ],
            )

            async with PolarionClient(
                _config(), write_delay=0, min_interval=0
            ) as client:
                result = await client.get("/projects")

            assert result == {"data": []}
            assert route.call_count == 2

    async def test_retries_on_503_then_succeeds(self) -> None:
        with respx.mock(base_url=BASE) as mock:
            route = mock.get("/projects").mock(
                side_effect=[
                    httpx.Response(503, json={"error": "Unavailable"}),
                    httpx.Response(200, json={"data": ["ok"]}),
                ],
            )

            async with PolarionClient(
                _config(), write_delay=0, min_interval=0
            ) as client:
                result = await client.get("/projects")

            assert result == {"data": ["ok"]}
            assert route.call_count == 2

    async def test_max_retries_then_raises(self) -> None:
        """3 consecutive 500s → PolarionError after 2 retries."""
        with respx.mock(base_url=BASE) as mock:
            route = mock.get("/projects").mock(
                side_effect=[
                    httpx.Response(500, json={"error": "fail"}),
                    httpx.Response(500, json={"error": "fail"}),
                    httpx.Response(500, json={"error": "fail"}),
                ],
            )

            async with PolarionClient(
                _config(), write_delay=0, min_interval=0
            ) as client:
                with pytest.raises(PolarionError) as exc_info:
                    await client.get("/projects")

            assert exc_info.value.status_code == 500
            assert route.call_count == 3  # initial + 2 retries

    async def test_no_retry_on_4xx(self) -> None:
        """Non-retryable 4xx must fail immediately (no retry)."""
        with respx.mock(base_url=BASE) as mock:
            route = mock.get("/projects").mock(
                return_value=httpx.Response(
                    400,
                    json={"error": "Bad Request"},
                ),
            )

            async with PolarionClient(
                _config(), write_delay=0, min_interval=0
            ) as client:
                with pytest.raises(PolarionError):
                    await client.get("/projects")

            assert route.call_count == 1

    async def test_no_retry_on_401(self) -> None:
        """401 is not retried — it fails immediately."""
        with respx.mock(base_url=BASE) as mock:
            route = mock.get("/projects").mock(
                return_value=httpx.Response(
                    401,
                    json={"error": "Unauthorized"},
                ),
            )

            async with PolarionClient(
                _config(), write_delay=0, min_interval=0
            ) as client:
                with pytest.raises(PolarionAuthError):
                    await client.get("/projects")

            assert route.call_count == 1

    async def test_retry_two_failures_then_success(self) -> None:
        """Two 502 failures → then 200 on the third attempt."""
        with respx.mock(base_url=BASE) as mock:
            route = mock.get("/projects").mock(
                side_effect=[
                    httpx.Response(502, json={"error": "Bad Gateway"}),
                    httpx.Response(502, json={"error": "Bad Gateway"}),
                    httpx.Response(200, json={"data": "ok"}),
                ],
            )

            async with PolarionClient(
                _config(), write_delay=0, min_interval=0
            ) as client:
                result = await client.get("/projects")

            assert result == {"data": "ok"}
            assert route.call_count == 3


class TestMinIntervalWiring:
    """Pacing interval derive from config rate cap unless explicitly overridden."""

    async def test_min_interval_derived_from_config(self) -> None:
        config = PolarionConfig(
            polarion_url="https://polarion.example.com",
            polarion_token="t",
            polarion_max_requests_per_second=5.0,
        )
        async with PolarionClient(config, write_delay=0) as client:
            assert client._min_interval == pytest.approx(0.2)

    async def test_min_interval_defaults_to_one_second(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Config default 1 req/s pace 1 s apart."""
        # Cap leak two ways: shell export (delenv) + repo-root .env
        # (_env_file=None) — both must die for default-path assert.
        monkeypatch.delenv("POLARION_MAX_REQUESTS_PER_SECOND", raising=False)
        config = PolarionConfig(
            polarion_url="https://polarion.example.com",
            polarion_token="test-token",
            _env_file=None,  # type: ignore[call-arg]
        )
        async with PolarionClient(config, write_delay=0) as client:
            assert client._min_interval == pytest.approx(1.0)

    async def test_zero_rate_disables_pacing(self) -> None:
        """Rate 0 = no cap — reciprocal would ZeroDivisionError."""
        config = PolarionConfig(
            polarion_url="https://polarion.example.com",
            polarion_token="t",
            polarion_max_requests_per_second=0,
        )
        async with PolarionClient(config, write_delay=0) as client:
            assert client._min_interval == 0.0

    async def test_explicit_min_interval_wins_over_config(self) -> None:
        config = PolarionConfig(
            polarion_url="https://polarion.example.com",
            polarion_token="t",
            polarion_max_requests_per_second=1.0,
        )
        async with PolarionClient(config, write_delay=0, min_interval=0) as client:
            assert client._min_interval == 0


class TestSerialization:
    """Concurrent callers serialise through PolarionClient lock."""

    async def test_concurrent_requests_run_sequentially(self) -> None:
        """Two ``client.get`` calls dispatched together must not overlap."""
        in_flight = 0
        max_in_flight = 0

        async def _record(request: httpx.Request) -> httpx.Response:
            nonlocal in_flight, max_in_flight
            in_flight += 1
            max_in_flight = max(max_in_flight, in_flight)
            # Yield to other coroutine; without lock max_in_flight hit 2.
            await asyncio.sleep(0)
            in_flight -= 1
            return httpx.Response(200, json={"data": []})

        with respx.mock(base_url=BASE) as mock:
            route = mock.get("/projects").mock(side_effect=_record)

            async with PolarionClient(
                _config(), write_delay=0, min_interval=0
            ) as client:
                await asyncio.gather(
                    client.get("/projects"),
                    client.get("/projects"),
                )

            assert route.call_count == 2
            assert max_in_flight == 1

    async def test_write_delay_keeps_lock_held(self) -> None:
        """GET during POST write_delay must wait, not overlap — sleep run
        inside the request lock.
        """
        post_start: list[float] = []
        get_start: list[float] = []

        async def _on_post(request: httpx.Request) -> httpx.Response:
            post_start.append(asyncio.get_running_loop().time())
            return httpx.Response(201, json={"data": {"id": "MCPT-1"}})

        async def _on_get(request: httpx.Request) -> httpx.Response:
            get_start.append(asyncio.get_running_loop().time())
            return httpx.Response(200, json={"data": []})

        write_delay = 0.2

        with respx.mock(base_url=BASE) as mock:
            mock.post("/projects/p/workitems").mock(side_effect=_on_post)
            mock.get("/projects").mock(side_effect=_on_get)

            async with PolarionClient(
                _config(), write_delay=write_delay, min_interval=0
            ) as client:
                await asyncio.gather(
                    client.post("/projects/p/workitems", json={}),
                    client.get("/projects"),
                )

        assert post_start and get_start
        # 0.9 slack absorb CI scheduling jitter; real margin = full write_delay.
        assert get_start[0] - post_start[0] >= write_delay * 0.9, (
            f"GET started {get_start[0] - post_start[0]:.3f}s after POST; "
            f"expected ≥ {write_delay * 0.9:.3f}s (write_delay held by lock)."
        )

    async def test_read_requests_paced_to_min_interval(self) -> None:
        """Reads obey the ≤3 req/s cap: two GETs spaced by ``min_interval``."""
        get_start: list[float] = []

        async def _on_get(request: httpx.Request) -> httpx.Response:
            get_start.append(asyncio.get_running_loop().time())
            return httpx.Response(200, json={"data": []})

        min_interval = 0.2

        with respx.mock(base_url=BASE) as mock:
            mock.get("/projects").mock(side_effect=_on_get)

            async with PolarionClient(
                _config(), write_delay=0, min_interval=min_interval
            ) as client:
                await client.get("/projects")
                await client.get("/projects")

        assert len(get_start) == 2
        # 0.9 slack absorb scheduler jitter (sleep may wake slightly early).
        assert get_start[1] - get_start[0] >= min_interval * 0.9, (
            f"second GET started {get_start[1] - get_start[0]:.3f}s after first; "
            f"expected ≥ {min_interval * 0.9:.3f}s (read pacing)."
        )

    async def test_slow_request_adds_no_extra_pacing(self) -> None:
        """Start-based pacing: request slower than ``min_interval`` add no wait."""
        request_time = 0.4
        min_interval = 0.2
        first_end: list[float] = []
        second_start: list[float] = []
        call = 0

        async def _on_get(request: httpx.Request) -> httpx.Response:
            nonlocal call
            call += 1
            if call == 1:
                await asyncio.sleep(request_time)
                first_end.append(asyncio.get_running_loop().time())
            else:
                second_start.append(asyncio.get_running_loop().time())
            return httpx.Response(200, json={"data": []})

        with respx.mock(base_url=BASE) as mock:
            mock.get("/projects").mock(side_effect=_on_get)

            async with PolarionClient(
                _config(), write_delay=0, min_interval=min_interval
            ) as client:
                await client.get("/projects")
                await client.get("/projects")

        assert first_end and second_start
        # request_time > min_interval — second GET add no extra sleep.
        assert second_start[0] - first_end[0] < min_interval, (
            f"second GET started {second_start[0] - first_end[0]:.3f}s after the "
            f"first ended; expected < {min_interval:.3f}s (no extra pacing)."
        )

    async def test_retry_restamps_pacing(self) -> None:
        """After retry, pacing track last attempt sent, not stale start."""
        starts: list[float] = []

        async def _on_get(request: httpx.Request) -> httpx.Response:
            starts.append(asyncio.get_running_loop().time())
            if len(starts) == 1:
                return httpx.Response(429, json={"error": "Too Many Requests"})
            return httpx.Response(200, json={"data": []})

        min_interval = 0.2

        with respx.mock(base_url=BASE) as mock:
            mock.get("/projects").mock(side_effect=_on_get)

            # Non-zero backoff so retry attempt land measurably after first.
            with patch("mcp_server_polarion.core.client._INITIAL_BACKOFF_SECONDS", 0.1):
                async with PolarionClient(
                    _config(), write_delay=0, min_interval=min_interval
                ) as client:
                    await client.get("/projects")  # 429 → retried → 200
                    await client.get("/projects")

        assert len(starts) == 3  # first 429, retry 200, follow-up 200
        # Follow-up space from retry attempt (starts[1]), not stale starts[0].
        assert starts[2] - starts[1] >= min_interval * 0.9, (
            f"follow-up GET started {starts[2] - starts[1]:.3f}s after the retry "
            f"attempt; expected ≥ {min_interval * 0.9:.3f}s (re-stamped pacing)."
        )


class TestGetBytes:
    """``get_bytes`` — streamed binary GET with client-side size cap."""

    async def test_returns_exact_bytes(self) -> None:
        payload = b"\x89PNG\r\n\x1a\n" + b"x" * 50
        with respx.mock(base_url=BASE) as mock:
            mock.get("/projects/P1/attachments/a1/content").mock(
                return_value=httpx.Response(200, content=payload),
            )

            async with PolarionClient(
                _config(), write_delay=0, min_interval=0
            ) as client:
                result = await client.get_bytes(
                    "/projects/P1/attachments/a1/content", max_bytes=1024
                )

            assert result == payload

    async def test_sends_octet_stream_accept_header(self) -> None:
        """Vendor content endpoint 406 on client-wide JSON-only Accept."""
        with respx.mock(base_url=BASE) as mock:
            route = mock.get("/projects/P1/attachments/a1/content").mock(
                return_value=httpx.Response(200, content=b"data"),
            )

            async with PolarionClient(
                _config(), write_delay=0, min_interval=0
            ) as client:
                await client.get_bytes(
                    "/projects/P1/attachments/a1/content", max_bytes=1024
                )

            request = route.calls.last.request
            assert (
                request.headers["accept"]
                == "application/octet-stream, application/json"
            )

    async def test_404_raises_not_found_error(self) -> None:
        with respx.mock(base_url=BASE) as mock:
            mock.get("/projects/P1/attachments/missing/content").mock(
                return_value=httpx.Response(
                    404,
                    json={"errors": [{"detail": "Attachment not found"}]},
                ),
            )

            async with PolarionClient(
                _config(), write_delay=0, min_interval=0
            ) as client:
                with pytest.raises(PolarionNotFoundError) as exc_info:
                    await client.get_bytes(
                        "/projects/P1/attachments/missing/content",
                        max_bytes=1024,
                    )

            assert exc_info.value.status_code == 404

    async def test_oversize_body_raises_too_large_error(self) -> None:
        with respx.mock(base_url=BASE) as mock:
            mock.get("/projects/P1/attachments/a1/content").mock(
                return_value=httpx.Response(200, content=b"x" * 100),
            )

            async with PolarionClient(
                _config(), write_delay=0, min_interval=0
            ) as client:
                with pytest.raises(PolarionResponseTooLargeError) as exc_info:
                    await client.get_bytes(
                        "/projects/P1/attachments/a1/content", max_bytes=10
                    )

            assert exc_info.value.limit == 10

    async def test_retries_on_429_then_succeeds(self) -> None:
        """429 consumed by retry loop; caller see only final 200."""
        with respx.mock(base_url=BASE) as mock:
            route = mock.get("/projects/P1/attachments/a1/content").mock(
                side_effect=[
                    httpx.Response(429, json={"error": "Too Many Requests"}),
                    httpx.Response(200, content=b"data"),
                ],
            )

            async with PolarionClient(
                _config(), write_delay=0, min_interval=0
            ) as client:
                result = await client.get_bytes(
                    "/projects/P1/attachments/a1/content", max_bytes=1024
                )

            assert result == b"data"
            assert route.call_count == 2

    async def test_oversize_body_not_retried(self) -> None:
        """Client-side cap abort must not trigger the 429/5xx retry loop."""
        with respx.mock(base_url=BASE) as mock:
            route = mock.get("/projects/P1/attachments/a1/content").mock(
                return_value=httpx.Response(200, content=b"x" * 100),
            )

            async with PolarionClient(
                _config(), write_delay=0, min_interval=0
            ) as client:
                with pytest.raises(PolarionResponseTooLargeError):
                    await client.get_bytes(
                        "/projects/P1/attachments/a1/content", max_bytes=10
                    )

            assert route.call_count == 1


class TestPostMultipart:
    """multipart/form-data POST; same delay/retry contract as :meth:`post`."""

    _PATH = "/projects/P1/spaces/S1/documents/D1/attachments"

    async def test_returns_json_and_sends_multipart_body(self) -> None:
        with respx.mock(base_url=BASE) as mock:
            route = mock.post(self._PATH).mock(
                return_value=httpx.Response(
                    201,
                    json={"data": [{"type": "document_attachments", "id": "a.png"}]},
                ),
            )

            async with PolarionClient(
                _config(), write_delay=0, min_interval=0
            ) as client:
                result = await client.post_multipart(
                    self._PATH,
                    data={"resource": json.dumps({"data": [{"type": "x"}]})},
                    files=[
                        (
                            "files",
                            ("a.png", b"binary-content", "application/octet-stream"),
                        )
                    ],
                )

            assert result["data"] == [{"type": "document_attachments", "id": "a.png"}]

            request = route.calls.last.request
            content_type = request.headers["content-type"]
            assert content_type.startswith("multipart/form-data; boundary=")
            boundary = content_type.split("boundary=", 1)[1]

            body = request.content
            assert boundary.encode() in body
            assert b'name="resource"' in body
            assert b'name="files"; filename="a.png"' in body
            assert b"binary-content" in body

    async def test_write_delay_applied(self) -> None:
        """Post-success delay, same as :meth:`post`."""
        with respx.mock(base_url=BASE) as mock:
            mock.post(self._PATH).mock(
                return_value=httpx.Response(201, json={"data": []}),
            )

            write_delay = 0.2
            async with PolarionClient(
                _config(), write_delay=write_delay, min_interval=0
            ) as client:
                start = asyncio.get_running_loop().time()
                await client.post_multipart(
                    self._PATH,
                    data={"resource": "{}"},
                    files=[("files", ("a.png", b"x", "application/octet-stream"))],
                )
                elapsed = asyncio.get_running_loop().time() - start

            assert elapsed >= write_delay * 0.9

    async def test_retries_on_429_then_succeeds(self) -> None:
        with respx.mock(base_url=BASE) as mock:
            route = mock.post(self._PATH).mock(
                side_effect=[
                    httpx.Response(429, json={"error": "Too Many Requests"}),
                    httpx.Response(201, json={"data": []}),
                ],
            )

            async with PolarionClient(
                _config(), write_delay=0, min_interval=0
            ) as client:
                result = await client.post_multipart(
                    self._PATH,
                    data={"resource": "{}"},
                    files=[("files", ("a.png", b"x", "application/octet-stream"))],
                )

            assert result == {"data": []}
            assert route.call_count == 2

    async def test_retries_on_503_then_succeeds(self) -> None:
        with respx.mock(base_url=BASE) as mock:
            route = mock.post(self._PATH).mock(
                side_effect=[
                    httpx.Response(503, json={"error": "Unavailable"}),
                    httpx.Response(201, json={"data": []}),
                ],
            )

            async with PolarionClient(
                _config(), write_delay=0, min_interval=0
            ) as client:
                result = await client.post_multipart(
                    self._PATH,
                    data={"resource": "{}"},
                    files=[("files", ("a.png", b"x", "application/octet-stream"))],
                )

            assert result == {"data": []}
            assert route.call_count == 2

    async def test_no_retry_on_400(self) -> None:
        with respx.mock(base_url=BASE) as mock:
            route = mock.post(self._PATH).mock(
                return_value=httpx.Response(400, json={"error": "Bad Request"}),
            )

            async with PolarionClient(
                _config(), write_delay=0, min_interval=0
            ) as client:
                with pytest.raises(PolarionError) as exc_info:
                    await client.post_multipart(
                        self._PATH,
                        data={"resource": "{}"},
                        files=[("files", ("a.png", b"x", "application/octet-stream"))],
                    )

            assert type(exc_info.value) is PolarionError
            assert route.call_count == 1

    async def test_no_retry_on_409_duplicate_filename(self) -> None:
        with respx.mock(base_url=BASE) as mock:
            route = mock.post(self._PATH).mock(
                return_value=httpx.Response(
                    409,
                    json={"errors": [{"detail": "same ID already exists"}]},
                ),
            )

            async with PolarionClient(
                _config(), write_delay=0, min_interval=0
            ) as client:
                with pytest.raises(PolarionError) as exc_info:
                    await client.post_multipart(
                        self._PATH,
                        data={"resource": "{}"},
                        files=[("files", ("a.png", b"x", "application/octet-stream"))],
                    )

            assert exc_info.value.status_code == 409
            assert route.call_count == 1

    async def test_no_retry_on_413(self) -> None:
        with respx.mock(base_url=BASE) as mock:
            route = mock.post(self._PATH).mock(
                return_value=httpx.Response(413, json={"error": "Too Large"}),
            )

            async with PolarionClient(
                _config(), write_delay=0, min_interval=0
            ) as client:
                with pytest.raises(PolarionError) as exc_info:
                    await client.post_multipart(
                        self._PATH,
                        data={"resource": "{}"},
                        files=[("files", ("a.png", b"x", "application/octet-stream"))],
                    )

            assert exc_info.value.status_code == 413
            assert route.call_count == 1

    async def test_401_raises_auth_error(self) -> None:
        with respx.mock(base_url=BASE) as mock:
            mock.post(self._PATH).mock(
                return_value=httpx.Response(401, json={"error": "Unauthorized"}),
            )

            async with PolarionClient(
                _config(), write_delay=0, min_interval=0
            ) as client:
                with pytest.raises(PolarionAuthError) as exc_info:
                    await client.post_multipart(
                        self._PATH,
                        data={"resource": "{}"},
                        files=[("files", ("a.png", b"x", "application/octet-stream"))],
                    )

            assert exc_info.value.status_code == 401

    async def test_403_raises_auth_error(self) -> None:
        with respx.mock(base_url=BASE) as mock:
            mock.post(self._PATH).mock(
                return_value=httpx.Response(403, json={"error": "Forbidden"}),
            )

            async with PolarionClient(
                _config(), write_delay=0, min_interval=0
            ) as client:
                with pytest.raises(PolarionAuthError) as exc_info:
                    await client.post_multipart(
                        self._PATH,
                        data={"resource": "{}"},
                        files=[("files", ("a.png", b"x", "application/octet-stream"))],
                    )

            assert exc_info.value.status_code == 403

    async def test_404_raises_not_found_error(self) -> None:
        with respx.mock(base_url=BASE) as mock:
            mock.post(self._PATH).mock(
                return_value=httpx.Response(404, json={"error": "Not Found"}),
            )

            async with PolarionClient(
                _config(), write_delay=0, min_interval=0
            ) as client:
                with pytest.raises(PolarionNotFoundError) as exc_info:
                    await client.post_multipart(
                        self._PATH,
                        data={"resource": "{}"},
                        files=[("files", ("a.png", b"x", "application/octet-stream"))],
                    )

            assert exc_info.value.status_code == 404


class TestContextManager:
    """Async context-manager protocol."""

    async def test_context_manager_closes_client(self) -> None:
        """``async with`` call ``close()`` on exit."""
        async with PolarionClient(_config(), write_delay=0, min_interval=0) as client:
            assert client.base_url.endswith("/polarion/rest/v1")

        assert client.is_closed

    async def test_manual_close(self) -> None:
        """Direct ``close()`` also shut down client."""
        client = PolarionClient(_config(), write_delay=0, min_interval=0)
        await client.close()
        assert client.is_closed


class TestConfigWiring:
    """``PolarionConfig`` values wired correctly."""

    def test_base_url_construction(self) -> None:
        config = PolarionConfig(
            polarion_url="https://my-instance.com/",
            polarion_token="t",
        )
        client = PolarionClient(config)
        assert client.base_url == "https://my-instance.com/polarion/rest/v1"

    def test_trailing_slash_stripped(self) -> None:
        config = PolarionConfig(
            polarion_url="https://example.com///",
            polarion_token="t",
        )
        assert config.base_api_url == "https://example.com/polarion/rest/v1"

    def test_verify_ssl_default_true_passed_to_httpx(self) -> None:
        with patch("mcp_server_polarion.core.client.httpx.AsyncClient") as spy:
            PolarionClient(_config())
        assert spy.call_args.kwargs["verify"] is True

    def test_verify_ssl_false_passed_to_httpx(self) -> None:
        config = PolarionConfig(
            polarion_url="https://polarion.example.com",
            polarion_token="test-token",
            polarion_verify_ssl=False,
        )
        with patch("mcp_server_polarion.core.client.httpx.AsyncClient") as spy:
            PolarionClient(config)
        assert spy.call_args.kwargs["verify"] is False
