"""Auth-error surface: Polarion 403/401 detail must reach the model, plus the
best-effort document-status hint that separates a workflow lock from a token
problem.
"""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from mcp_server_polarion.core.exceptions import PolarionAuthError, PolarionError
from mcp_server_polarion.tools._shared.errors import auth_error, document_status_hint


class TestAuthError:
    """Tests for `auth_error`."""

    def test_message_carries_server_detail(self) -> None:
        exc = PolarionAuthError(
            "Polarion API error 403 403: Cannot move Work Item(s) to a Document "
            "due to limited permissions.",
            status_code=403,
        )

        result = auth_error("move work item", exc)

        assert "Cannot move work item --" in str(result)
        assert "due to limited permissions." in str(result)

    def test_token_hint_only_without_document_hint(self) -> None:
        exc = PolarionAuthError("Polarion API error 403 403: nope", status_code=403)

        assert "POLARION_TOKEN" in str(auth_error("list documents", exc))

    def test_document_hint_replaces_token_hint(self) -> None:
        # Cause already identified -- token advice send model to remedy that
        # cannot work.
        exc = PolarionAuthError("Polarion API error 403 403: nope", status_code=403)

        result = auth_error("update document", exc, document_hint=" HINT.")

        assert str(result).endswith("nope HINT.")
        assert "POLARION_TOKEN" not in str(result)

    def test_returns_permission_error(self) -> None:
        exc = PolarionAuthError("boom", status_code=401)

        assert isinstance(auth_error("list projects", exc), PermissionError)


class TestDocumentStatusHint:
    """Tests for `document_status_hint`."""

    async def test_hint_names_space_document_and_status(self) -> None:
        client = AsyncMock()
        client.get.return_value = {"data": {"attributes": {"status": "reviewed"}}}

        hint = await document_status_hint(client, "proj1", "Design", "SRS")

        assert "'Design/SRS'" in hint
        assert "'reviewed'" in hint
        client.get.assert_awaited_once_with(
            "/projects/proj1/spaces/Design/documents/SRS",
            params={"fields[documents]": "status"},
        )

    async def test_document_name_slash_encoded(self) -> None:
        client = AsyncMock()
        client.get.return_value = {"data": {"attributes": {"status": "approved"}}}

        await document_status_hint(client, "proj1", "Design", "sub/doc")

        assert client.get.await_args.args[0].endswith("/documents/sub%2Fdoc")

    async def test_blank_status_yields_no_hint(self) -> None:
        client = AsyncMock()
        client.get.return_value = {"data": {"attributes": {}}}

        assert await document_status_hint(client, "proj1", "Design", "SRS") == ""

    @pytest.mark.parametrize(
        "error",
        [
            PolarionAuthError("forbidden", status_code=403),
            PolarionError("boom", status_code=500),
        ],
    )
    async def test_failed_lookup_never_masks_original_error(
        self, error: PolarionError
    ) -> None:
        # Hint best-effort: caller's pending 403 matter more than lookup failure.
        client = AsyncMock()
        client.get.side_effect = error

        assert await document_status_hint(client, "proj1", "Design", "SRS") == ""
