"""Tests for the write MCP tools (currently ``create_work_item``).

Mirrors the test patterns in ``test_read.py``: each tool is exercised by
calling the async function directly with a mock ``PolarionClient``
injected via a mock ``Context``.
"""

from __future__ import annotations

import inspect
from typing import Annotated, cast, get_type_hints
from unittest.mock import AsyncMock, MagicMock

import pytest
from pydantic import TypeAdapter, ValidationError

from mcp_server_polarion.core.client import PolarionClient
from mcp_server_polarion.core.exceptions import (
    PolarionAuthError,
    PolarionError,
    PolarionNotFoundError,
)
from mcp_server_polarion.models import Hyperlink, WorkItemCreateResult
from mcp_server_polarion.tools import write as _write_mod

# In FastMCP 3.0, @mcp.tool returns the original function unchanged
# (not a FunctionTool wrapper), so we reference them directly.
create_work_item = _write_mod.create_work_item
_build_work_item_payload = _write_mod._build_work_item_payload
_extract_created_id = _write_mod._extract_created_id


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def mock_client() -> AsyncMock:
    """Return a mock PolarionClient with async methods."""
    client = AsyncMock(spec=PolarionClient)
    client.post = AsyncMock()
    return client


@pytest.fixture
def mock_ctx(mock_client: AsyncMock) -> MagicMock:
    """Return a mock FastMCP Context with the mock client."""
    ctx = MagicMock()
    ctx.lifespan_context = {
        "polarion_client": mock_client,
    }
    return ctx


# ---------------------------------------------------------------------------
# _build_work_item_payload
# ---------------------------------------------------------------------------


class TestBuildWorkItemPayload:
    """Tests for the private ``_build_work_item_payload`` helper."""

    def test_minimal_payload_has_only_required_attrs(self) -> None:
        payload = _build_work_item_payload(
            title="My WI",
            type="task",
            description_html="",
            status=None,
            priority=None,
            severity=None,
            assignee_ids=None,
            due_date=None,
            initial_estimate=None,
            hyperlinks=None,
        )

        assert payload == {
            "data": [
                {
                    "type": "workitems",
                    "attributes": {"title": "My WI", "type": "task"},
                }
            ]
        }
        # No relationships key, no description, no other attributes.
        item = cast(list[dict[str, object]], payload["data"])[0]
        assert "relationships" not in item
        attrs = cast(dict[str, object], item["attributes"])
        assert set(attrs.keys()) == {"title", "type"}

    def test_skips_none_and_empty_string_fields(self) -> None:
        payload = _build_work_item_payload(
            title="x",
            type="task",
            description_html="",
            status="",
            priority=None,
            severity="",
            assignee_ids=[],
            due_date="",
            initial_estimate=None,
            hyperlinks=[],
        )

        item = cast(list[dict[str, object]], payload["data"])[0]
        attrs = cast(dict[str, object], item["attributes"])
        # Only title + type — nothing else slipped through.
        assert set(attrs.keys()) == {"title", "type"}
        assert "relationships" not in item

    def test_includes_description_block(self) -> None:
        payload = _build_work_item_payload(
            title="x",
            type="task",
            description_html="<p>hello</p>",
            status=None,
            priority=None,
            severity=None,
            assignee_ids=None,
            due_date=None,
            initial_estimate=None,
            hyperlinks=None,
        )

        item = cast(list[dict[str, object]], payload["data"])[0]
        attrs = cast(dict[str, object], item["attributes"])
        assert attrs["description"] == {
            "type": "text/html",
            "value": "<p>hello</p>",
        }

    def test_assignee_ids_become_to_many_users_relationship(self) -> None:
        payload = _build_work_item_payload(
            title="x",
            type="task",
            description_html="",
            status=None,
            priority=None,
            severity=None,
            assignee_ids=["alice", "bob"],
            due_date=None,
            initial_estimate=None,
            hyperlinks=None,
        )

        item = cast(list[dict[str, object]], payload["data"])[0]
        rels = cast(dict[str, object], item["relationships"])
        assert rels["assignee"] == {
            "data": [
                {"type": "users", "id": "alice"},
                {"type": "users", "id": "bob"},
            ]
        }

    def test_hyperlinks_serialise_role_title_uri(self) -> None:
        payload = _build_work_item_payload(
            title="x",
            type="task",
            description_html="",
            status=None,
            priority=None,
            severity=None,
            assignee_ids=None,
            due_date=None,
            initial_estimate=None,
            hyperlinks=[
                Hyperlink(role="ref_ext", title="Spec", uri="https://example.com"),
                Hyperlink(role="implementation", uri="https://example.com/code"),
            ],
        )

        item = cast(list[dict[str, object]], payload["data"])[0]
        attrs = cast(dict[str, object], item["attributes"])
        assert attrs["hyperlinks"] == [
            {
                "role": "ref_ext",
                "title": "Spec",
                "uri": "https://example.com",
            },
            {
                "role": "implementation",
                "title": "",
                "uri": "https://example.com/code",
            },
        ]

    def test_all_optional_attrs_included_when_set(self) -> None:
        payload = _build_work_item_payload(
            title="x",
            type="task",
            description_html="",
            status="open",
            priority="50.0",
            severity="major",
            assignee_ids=None,
            due_date="2026-05-31",
            initial_estimate="5 1/2d",
            hyperlinks=None,
        )

        item = cast(list[dict[str, object]], payload["data"])[0]
        attrs = cast(dict[str, object], item["attributes"])
        assert attrs["status"] == "open"
        assert attrs["priority"] == "50.0"
        assert attrs["severity"] == "major"
        assert attrs["dueDate"] == "2026-05-31"
        assert attrs["initialEstimate"] == "5 1/2d"


# ---------------------------------------------------------------------------
# _extract_created_id
# ---------------------------------------------------------------------------


class TestExtractCreatedId:
    """Tests for the private ``_extract_created_id`` helper."""

    def test_extracts_short_id_from_data_array(self) -> None:
        response: dict[str, object] = {
            "data": [
                {
                    "type": "workitems",
                    "id": "MyProj/MCPT-042",
                    "links": {"self": "..."},
                }
            ]
        }
        assert _extract_created_id(response) == "MCPT-042"

    def test_returns_none_when_data_missing(self) -> None:
        assert _extract_created_id({}) is None

    def test_returns_none_when_data_empty_list(self) -> None:
        assert _extract_created_id({"data": []}) is None

    def test_returns_none_when_data_not_a_list(self) -> None:
        assert _extract_created_id({"data": {"id": "MyProj/MCPT-1"}}) is None

    def test_returns_none_when_first_entry_missing_id(self) -> None:
        assert _extract_created_id({"data": [{"type": "workitems"}]}) is None

    def test_returns_none_when_first_entry_not_dict(self) -> None:
        assert _extract_created_id({"data": ["not a dict"]}) is None


# ---------------------------------------------------------------------------
# create_work_item — dry run
# ---------------------------------------------------------------------------


class TestCreateWorkItemDryRun:
    """Tests for ``create_work_item`` with ``dry_run=True``."""

    async def test_dry_run_returns_payload_without_calling_post(
        self, mock_ctx: MagicMock, mock_client: AsyncMock
    ) -> None:
        result = await create_work_item(
            mock_ctx,
            project_id="MyProj",
            title="Dry test",
            type="task",
            description=None,
            status=None,
            priority=None,
            severity=None,
            assignee_ids=None,
            due_date=None,
            initial_estimate=None,
            hyperlinks=None,
            dry_run=True,
        )

        mock_client.post.assert_not_called()
        assert isinstance(result, WorkItemCreateResult)
        assert result.dry_run is True
        assert result.created is False
        assert result.work_item_id is None
        assert result.payload_preview is not None
        # payload_preview is a plain dict (no Pydantic objects leaked).
        assert isinstance(result.payload_preview, dict)
        item = cast(list[dict[str, object]], result.payload_preview["data"])[0]
        attrs = cast(dict[str, object], item["attributes"])
        assert attrs == {"title": "Dry test", "type": "task"}


# ---------------------------------------------------------------------------
# create_work_item — happy path
# ---------------------------------------------------------------------------


class TestCreateWorkItemHappyPath:
    """Tests for a successful ``create_work_item`` call."""

    async def test_returns_short_id_on_201(
        self, mock_ctx: MagicMock, mock_client: AsyncMock
    ) -> None:
        mock_client.post.return_value = {
            "data": [
                {
                    "type": "workitems",
                    "id": "MyProj/MCPT-042",
                    "links": {"self": "..."},
                }
            ]
        }

        result = await create_work_item(
            mock_ctx,
            project_id="MyProj",
            title="Real",
            type="task",
            description=None,
            status=None,
            priority=None,
            severity=None,
            assignee_ids=None,
            due_date=None,
            initial_estimate=None,
            hyperlinks=None,
            dry_run=False,
        )

        assert isinstance(result, WorkItemCreateResult)
        assert result.created is True
        assert result.dry_run is False
        assert result.work_item_id == "MCPT-042"
        assert result.payload_preview is None

    async def test_post_called_with_correct_path_and_body(
        self, mock_ctx: MagicMock, mock_client: AsyncMock
    ) -> None:
        mock_client.post.return_value = {
            "data": [{"type": "workitems", "id": "MyProj/MCPT-1"}]
        }

        await create_work_item(
            mock_ctx,
            project_id="MyProj",
            title="t",
            type="task",
            description=None,
            status="open",
            priority=None,
            severity=None,
            assignee_ids=["alice"],
            due_date=None,
            initial_estimate=None,
            hyperlinks=None,
            dry_run=False,
        )

        args, kwargs = mock_client.post.call_args
        assert args == ("/projects/MyProj/workitems",)
        body = kwargs["json"]
        item = body["data"][0]
        assert item["attributes"]["title"] == "t"
        assert item["attributes"]["type"] == "task"
        assert item["attributes"]["status"] == "open"
        assert item["relationships"]["assignee"]["data"] == [
            {"type": "users", "id": "alice"}
        ]

    async def test_description_is_converted_and_sanitized(
        self, mock_ctx: MagicMock, mock_client: AsyncMock
    ) -> None:
        mock_client.post.return_value = {
            "data": [{"type": "workitems", "id": "MyProj/MCPT-1"}]
        }

        await create_work_item(
            mock_ctx,
            project_id="MyProj",
            title="t",
            type="task",
            description="**bold** [link](https://example.com)",
            status=None,
            priority=None,
            severity=None,
            assignee_ids=None,
            due_date=None,
            initial_estimate=None,
            hyperlinks=None,
            dry_run=False,
        )

        _, kwargs = mock_client.post.call_args
        desc = kwargs["json"]["data"][0]["attributes"]["description"]
        assert desc["type"] == "text/html"
        # Markdown was rendered to HTML by markdown_to_html.
        assert "<strong>bold</strong>" in desc["value"]
        # Safe https link survives both markdown-it and sanitize_html.
        assert 'href="https://example.com"' in desc["value"]

    async def test_description_strips_dangerous_link_schemes(
        self, mock_ctx: MagicMock, mock_client: AsyncMock
    ) -> None:
        """Verify no anchor with a javascript: href is ever sent.

        markdown-it-py already rejects javascript: in link URLs (it
        leaves the literal source text unrendered), and sanitize_html
        strips javascript: hrefs as a defense-in-depth second layer.
        Either way, the sent payload must not contain a usable XSS
        anchor.
        """
        mock_client.post.return_value = {
            "data": [{"type": "workitems", "id": "MyProj/MCPT-1"}]
        }

        await create_work_item(
            mock_ctx,
            project_id="MyProj",
            title="t",
            type="task",
            description="[click](javascript:alert(1))",
            status=None,
            priority=None,
            severity=None,
            assignee_ids=None,
            due_date=None,
            initial_estimate=None,
            hyperlinks=None,
            dry_run=False,
        )

        _, kwargs = mock_client.post.call_args
        desc_html = kwargs["json"]["data"][0]["attributes"]["description"]["value"]
        # No dangerous href attribute — neither markdown-it nor
        # sanitize_html should let one through.
        assert 'href="javascript:' not in desc_html
        assert "href='javascript:" not in desc_html


# ---------------------------------------------------------------------------
# create_work_item — error mapping
# ---------------------------------------------------------------------------


class TestCreateWorkItemErrorMapping:
    """Tests that domain exceptions are mapped at the tool layer."""

    async def test_401_raises_permission_error(
        self, mock_ctx: MagicMock, mock_client: AsyncMock
    ) -> None:
        mock_client.post.side_effect = PolarionAuthError("auth", status_code=401)

        with pytest.raises(PermissionError):
            await create_work_item(
                mock_ctx,
                project_id="MyProj",
                title="t",
                type="task",
                description=None,
                status=None,
                priority=None,
                severity=None,
                assignee_ids=None,
                due_date=None,
                initial_estimate=None,
                hyperlinks=None,
                dry_run=False,
            )

    async def test_404_raises_value_error(
        self, mock_ctx: MagicMock, mock_client: AsyncMock
    ) -> None:
        mock_client.post.side_effect = PolarionNotFoundError(
            "not found", status_code=404
        )

        with pytest.raises(ValueError, match="list_projects"):
            await create_work_item(
                mock_ctx,
                project_id="ghost",
                title="t",
                type="task",
                description=None,
                status=None,
                priority=None,
                severity=None,
                assignee_ids=None,
                due_date=None,
                initial_estimate=None,
                hyperlinks=None,
                dry_run=False,
            )

    async def test_other_error_raises_runtime_error(
        self, mock_ctx: MagicMock, mock_client: AsyncMock
    ) -> None:
        mock_client.post.side_effect = PolarionError("boom", status_code=500)

        with pytest.raises(RuntimeError, match="boom"):
            await create_work_item(
                mock_ctx,
                project_id="MyProj",
                title="t",
                type="task",
                description=None,
                status=None,
                priority=None,
                severity=None,
                assignee_ids=None,
                due_date=None,
                initial_estimate=None,
                hyperlinks=None,
                dry_run=False,
            )


# ---------------------------------------------------------------------------
# create_work_item — response parsing failures
# ---------------------------------------------------------------------------


class TestCreateWorkItemResponseParsing:
    """Tests for unexpected 2xx response shapes from Polarion."""

    async def test_empty_data_array_raises_runtime_error(
        self, mock_ctx: MagicMock, mock_client: AsyncMock
    ) -> None:
        mock_client.post.return_value = {"data": []}

        with pytest.raises(RuntimeError, match="no work-item ID"):
            await create_work_item(
                mock_ctx,
                project_id="MyProj",
                title="t",
                type="task",
                description=None,
                status=None,
                priority=None,
                severity=None,
                assignee_ids=None,
                due_date=None,
                initial_estimate=None,
                hyperlinks=None,
                dry_run=False,
            )

    async def test_data_not_a_list_raises_runtime_error(
        self, mock_ctx: MagicMock, mock_client: AsyncMock
    ) -> None:
        mock_client.post.return_value = {"data": {"id": "MyProj/MCPT-1"}}

        with pytest.raises(RuntimeError, match="no work-item ID"):
            await create_work_item(
                mock_ctx,
                project_id="MyProj",
                title="t",
                type="task",
                description=None,
                status=None,
                priority=None,
                severity=None,
                assignee_ids=None,
                due_date=None,
                initial_estimate=None,
                hyperlinks=None,
                dry_run=False,
            )

    async def test_missing_id_field_raises_runtime_error(
        self, mock_ctx: MagicMock, mock_client: AsyncMock
    ) -> None:
        mock_client.post.return_value = {"data": [{"type": "workitems"}]}

        with pytest.raises(RuntimeError, match="no work-item ID"):
            await create_work_item(
                mock_ctx,
                project_id="MyProj",
                title="t",
                type="task",
                description=None,
                status=None,
                priority=None,
                severity=None,
                assignee_ids=None,
                due_date=None,
                initial_estimate=None,
                hyperlinks=None,
                dry_run=False,
            )


# ---------------------------------------------------------------------------
# create_work_item — Pydantic Field constraints
# ---------------------------------------------------------------------------


class TestCreateWorkItemFieldValidation:
    """Verify ``min_length=1`` constraints attached to required parameters.

    FastMCP enforces these via JSON Schema at the MCP protocol layer
    before the tool function is invoked; calling the function directly
    in unit tests bypasses that gate. To prove the constraint is wired
    correctly, we rebuild a ``TypeAdapter`` from each parameter's
    annotation + ``FieldInfo`` and assert the constraint actually
    rejects bad input at the schema layer.
    """

    @staticmethod
    def _adapter_for(param_name: str) -> TypeAdapter[object]:
        hints = get_type_hints(create_work_item)
        sig = inspect.signature(create_work_item)
        field_info = sig.parameters[param_name].default
        return TypeAdapter(Annotated[hints[param_name], field_info])

    def test_title_rejects_empty_string(self) -> None:
        with pytest.raises(ValidationError):
            self._adapter_for("title").validate_python("")

    def test_title_accepts_non_empty(self) -> None:
        assert self._adapter_for("title").validate_python("hello") == "hello"

    def test_type_rejects_empty_string(self) -> None:
        with pytest.raises(ValidationError):
            self._adapter_for("type").validate_python("")

    def test_type_accepts_non_empty(self) -> None:
        assert self._adapter_for("type").validate_python("task") == "task"
