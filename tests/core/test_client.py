"""Tests for ``PolarionClient`` — HTTP behaviour, retry, error mapping.

Every test mocks the Polarion REST API via ``respx`` so no real server
is needed.  The client uses ``write_delay=0`` to avoid sleeping.
"""

from __future__ import annotations

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
)

BASE = "https://polarion.example.com/polarion/rest/v1"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _config() -> PolarionConfig:
    return PolarionConfig(
        polarion_url="https://polarion.example.com",
        polarion_token="test-token",
    )


# ---------------------------------------------------------------------------
# 1. Authentication — Bearer token in default headers
# ---------------------------------------------------------------------------


class TestAuthentication:
    """Verify that the client sends the correct ``Authorization`` header."""

    async def test_bearer_token_sent(self) -> None:
        """GET includes ``Authorization: Bearer <token>``."""
        with respx.mock(base_url=BASE) as mock:
            route = mock.get("/projects").mock(
                return_value=httpx.Response(200, json={"data": []}),
            )

            async with PolarionClient(_config(), write_delay=0) as client:
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

            async with PolarionClient(_config(), write_delay=0) as client:
                await client.get("/projects")

            request = route.calls.last.request
            assert request.headers["content-type"] == "application/json"


# ---------------------------------------------------------------------------
# 2. Successful responses
# ---------------------------------------------------------------------------


class TestSuccessfulResponses:
    """Verify correct parsing of 2xx responses."""

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

            async with PolarionClient(_config(), write_delay=0) as client:
                result = await client.get("/projects")

            assert result["data"] == [{"id": "proj1"}]

    async def test_get_with_query_params(self) -> None:
        with respx.mock(base_url=BASE) as mock:
            route = mock.get("/projects/P1/workitems").mock(
                return_value=httpx.Response(200, json={"data": []}),
            )

            async with PolarionClient(_config(), write_delay=0) as client:
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

            async with PolarionClient(_config(), write_delay=0) as client:
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

            async with PolarionClient(_config(), write_delay=0) as client:
                result = await client.patch(
                    "/projects/P1/workitems/WI-001",
                    json={"data": {"attributes": {"title": "Updated"}}},
                )

            # 204 No Content → empty dict
            assert result == {}

    async def test_non_dict_body_wrapped(self) -> None:
        """If the response body is a list, wrap it in ``{"data": ...}``."""
        with respx.mock(base_url=BASE) as mock:
            mock.get("/some/list").mock(
                return_value=httpx.Response(200, json=[1, 2, 3]),
            )

            async with PolarionClient(_config(), write_delay=0) as client:
                result = await client.get("/some/list")

            assert result == {"data": [1, 2, 3]}


# ---------------------------------------------------------------------------
# 3. Error mapping
# ---------------------------------------------------------------------------


class TestErrorMapping:
    """Verify HTTP status → domain exception mapping."""

    async def test_401_raises_auth_error(self) -> None:
        with respx.mock(base_url=BASE) as mock:
            mock.get("/projects").mock(
                return_value=httpx.Response(
                    401,
                    json={"error": "Unauthorized"},
                ),
            )

            async with PolarionClient(_config(), write_delay=0) as client:
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

            async with PolarionClient(_config(), write_delay=0) as client:
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

            async with PolarionClient(_config(), write_delay=0) as client:
                with pytest.raises(PolarionNotFoundError) as exc_info:
                    await client.get("/projects/MISSING/workitems/WI-999")

            assert exc_info.value.status_code == 404

    async def test_400_raises_generic_error(self) -> None:
        with respx.mock(base_url=BASE) as mock:
            mock.post("/projects/P1/workitems").mock(
                return_value=httpx.Response(
                    400,
                    json={"error": "Bad Request"},
                ),
            )

            async with PolarionClient(_config(), write_delay=0) as client:
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

            async with PolarionClient(_config(), write_delay=0) as client:
                with pytest.raises(PolarionError, match="Internal"):
                    await client.get("/projects")

    async def test_error_with_non_json_body(self) -> None:
        """Non-JSON error bodies should still produce an error message."""
        with respx.mock(base_url=BASE) as mock:
            mock.get("/projects").mock(
                return_value=httpx.Response(
                    401,
                    text="<html>Unauthorized</html>",
                ),
            )

            async with PolarionClient(_config(), write_delay=0) as client:
                with pytest.raises(PolarionAuthError, match="Unauthorized"):
                    await client.get("/projects")

    async def test_html_tags_stripped_from_error_message(self) -> None:
        """HTML in non-JSON error body must be stripped, not exposed raw."""
        with respx.mock(base_url=BASE) as mock:
            mock.get("/projects").mock(
                return_value=httpx.Response(
                    503,
                    text="<html><body><h1>Service Unavailable</h1></body></html>",
                ),
            )

            async with PolarionClient(_config(), write_delay=0) as client:
                with pytest.raises(PolarionError) as exc_info:
                    await client.get("/projects")

        message = str(exc_info.value)
        assert "<html>" not in message
        assert "Service Unavailable" in message

    async def test_long_error_body_is_truncated(self) -> None:
        """Very long error detail must be capped at _MAX_ERROR_DETAIL_LEN."""
        long_text = "x" * 500
        with respx.mock(base_url=BASE) as mock:
            mock.get("/projects").mock(
                return_value=httpx.Response(
                    400,
                    json={"message": long_text},
                ),
            )

            async with PolarionClient(_config(), write_delay=0) as client:
                with pytest.raises(PolarionError) as exc_info:
                    await client.get("/projects")

        # The error detail portion must not exceed the configured limit.
        status_prefix = "Polarion API error 400 Bad Request: "
        detail_part = str(exc_info.value)[len(status_prefix) :]
        assert len(detail_part) <= _MAX_ERROR_DETAIL_LEN


# ---------------------------------------------------------------------------
# 4. Transport / network errors
# ---------------------------------------------------------------------------


class TestTransportErrors:
    """Verify that httpx transport errors are wrapped in PolarionError."""

    async def test_connection_error_becomes_polarion_error(self) -> None:
        with respx.mock(base_url=BASE) as mock:
            mock.get("/projects").mock(
                side_effect=httpx.ConnectError("refused"),
            )

            async with PolarionClient(_config(), write_delay=0) as client:
                with pytest.raises(PolarionError, match="transport error"):
                    await client.get("/projects")

    async def test_timeout_error_becomes_polarion_error(self) -> None:
        with respx.mock(base_url=BASE) as mock:
            mock.get("/projects").mock(
                side_effect=httpx.ReadTimeout("timed out"),
            )

            async with PolarionClient(_config(), write_delay=0) as client:
                with pytest.raises(PolarionError, match="transport error"):
                    await client.get("/projects")


# ---------------------------------------------------------------------------
# 5. Exponential-backoff retry
# ---------------------------------------------------------------------------


class TestRetry:
    """Verify retry behaviour on 429 and 5xx status codes."""

    async def test_retries_on_429_then_succeeds(self) -> None:
        """First request → 429, second → 200."""
        with respx.mock(base_url=BASE) as mock:
            route = mock.get("/projects").mock(
                side_effect=[
                    httpx.Response(429, json={"error": "Too Many Requests"}),
                    httpx.Response(200, json={"data": []}),
                ],
            )

            async with PolarionClient(_config(), write_delay=0) as client:
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

            async with PolarionClient(_config(), write_delay=0) as client:
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

            async with PolarionClient(_config(), write_delay=0) as client:
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

            async with PolarionClient(_config(), write_delay=0) as client:
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

            async with PolarionClient(_config(), write_delay=0) as client:
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

            async with PolarionClient(_config(), write_delay=0) as client:
                result = await client.get("/projects")

            assert result == {"data": "ok"}
            assert route.call_count == 3


# ---------------------------------------------------------------------------
# 6. Pagination helper — get_all_pages
# ---------------------------------------------------------------------------


class TestPagination:
    """Verify ``get_all_pages`` iterates through all pages."""

    async def test_single_page(self) -> None:
        """Only one page of results (fewer items than page_size)."""
        with respx.mock(base_url=BASE) as mock:
            mock.get("/projects").mock(
                return_value=httpx.Response(
                    200,
                    json={
                        "data": [{"id": "P1"}, {"id": "P2"}],
                        "links": {"self": "/projects"},
                    },
                ),
            )

            async with PolarionClient(_config(), write_delay=0) as client:
                items = await client.get_all_pages(
                    "/projects",
                    page_size=100,
                )

            assert len(items) == 2
            assert items[0]["id"] == "P1"

    async def test_multiple_pages(self) -> None:
        """Two full pages, then a partial final page."""
        page_size = 2
        with respx.mock(base_url=BASE) as mock:
            mock.get("/projects").mock(
                side_effect=[
                    httpx.Response(
                        200,
                        json={
                            "data": [{"id": "P1"}, {"id": "P2"}],
                            "links": {"self": "...", "next": "..."},
                        },
                    ),
                    httpx.Response(
                        200,
                        json={
                            "data": [{"id": "P3"}, {"id": "P4"}],
                            "links": {"self": "...", "next": "..."},
                        },
                    ),
                    httpx.Response(
                        200,
                        json={
                            "data": [{"id": "P5"}],
                            "links": {"self": "..."},
                        },
                    ),
                ],
            )

            async with PolarionClient(_config(), write_delay=0) as client:
                items = await client.get_all_pages(
                    "/projects",
                    page_size=page_size,
                )

            assert len(items) == 5
            assert [i["id"] for i in items] == [
                "P1",
                "P2",
                "P3",
                "P4",
                "P5",
            ]

    async def test_stops_when_no_next_link(self) -> None:
        """Full page but no ``next`` link — stop paginating."""
        page_size = 2
        with respx.mock(base_url=BASE) as mock:
            mock.get("/projects").mock(
                return_value=httpx.Response(
                    200,
                    json={
                        "data": [{"id": "P1"}, {"id": "P2"}],
                        "links": {"self": "..."},
                    },
                ),
            )

            async with PolarionClient(_config(), write_delay=0) as client:
                items = await client.get_all_pages(
                    "/projects",
                    page_size=page_size,
                )

            assert len(items) == 2

    async def test_empty_first_page(self) -> None:
        """Empty ``data`` on the first page returns an empty list."""
        with respx.mock(base_url=BASE) as mock:
            mock.get("/projects").mock(
                return_value=httpx.Response(200, json={"data": []}),
            )

            async with PolarionClient(_config(), write_delay=0) as client:
                items = await client.get_all_pages("/projects")

            assert items == []

    async def test_pagination_merges_params(self) -> None:
        """User-supplied params are preserved across pages."""
        with respx.mock(base_url=BASE) as mock:
            route = mock.get("/projects/P1/workitems").mock(
                return_value=httpx.Response(200, json={"data": []}),
            )

            async with PolarionClient(_config(), write_delay=0) as client:
                await client.get_all_pages(
                    "/projects/P1/workitems",
                    params={"fields[workitems]": "title"},
                    page_size=50,
                )

            request = route.calls.last.request
            url_str = str(request.url)
            assert "fields%5Bworkitems%5D=title" in url_str
            assert "page%5Bsize%5D=50" in url_str

    async def test_server_capped_page_size_with_next_link_continues(self) -> None:
        """page_size mismatch must NOT halt pagination when ``links.next`` is present.

        If the server caps its page size below the caller-requested value
        (e.g., caller asks 200, server returns 100), the old heuristic
        ``len(data) < page_size`` would stop early and silently drop items.
        The fix: rely on ``links.next`` as the authoritative stop signal.
        """
        server_items = [{"id": f"P{i}"} for i in range(100)]
        with respx.mock(base_url=BASE) as mock:
            mock.get("/projects").mock(
                side_effect=[
                    httpx.Response(
                        200,
                        json={
                            "data": server_items,
                            "links": {"self": "...", "next": "..."},
                        },
                    ),
                    httpx.Response(
                        200,
                        json={
                            "data": [{"id": "P100"}],
                            "links": {"self": "..."},
                        },
                    ),
                ],
            )

            async with PolarionClient(_config(), write_delay=0) as client:
                # Caller requests page_size=200, but server returns only 100.
                items = await client.get_all_pages("/projects", page_size=200)

        # Both pages must be fetched — no silent drop.
        assert len(items) == 101


# ---------------------------------------------------------------------------
# 7. Context-manager & close
# ---------------------------------------------------------------------------


class TestContextManager:
    """Verify async context-manager protocol."""

    async def test_context_manager_closes_client(self) -> None:
        """``async with`` should call ``close()`` on exit."""
        async with PolarionClient(_config(), write_delay=0) as client:
            assert client.base_url.endswith("/polarion/rest/v1")

        # After closing, the underlying httpx client is closed.
        assert client._client.is_closed

    async def test_manual_close(self) -> None:
        """Calling ``close()`` directly also shuts down the client."""
        client = PolarionClient(_config(), write_delay=0)
        await client.close()
        assert client._client.is_closed


# ---------------------------------------------------------------------------
# 8. Config → base_url wiring
# ---------------------------------------------------------------------------


class TestConfigWiring:
    """Verify that ``PolarionConfig`` values are wired correctly."""

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
