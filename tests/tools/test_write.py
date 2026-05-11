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
from mcp_server_polarion.models import (
    DocumentUpdateResult,
    Hyperlink,
    WorkItemCreateResult,
    WorkItemMoveResult,
    WorkItemUpdateResult,
)
from mcp_server_polarion.server import mcp
from mcp_server_polarion.tools import write as _write_mod

# In FastMCP 3.0, @mcp.tool returns the original function unchanged
# (not a FunctionTool wrapper), so we reference them directly.
create_work_item = _write_mod.create_work_item
move_work_item_to_document = _write_mod.move_work_item_to_document
update_document = _write_mod.update_document
update_work_item = _write_mod.update_work_item
_build_move_to_document_payload = _write_mod._build_move_to_document_payload
_build_update_document_payload = _write_mod._build_update_document_payload
_build_update_work_item_payload = _write_mod._build_update_work_item_payload
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
    client.patch = AsyncMock()
    client.get = AsyncMock()
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


# ===========================================================================
# move_work_item_to_document
# ===========================================================================


# ---------------------------------------------------------------------------
# _build_move_to_document_payload
# ---------------------------------------------------------------------------


class TestBuildMoveToDocumentPayload:
    """Tests for the private ``_build_move_to_document_payload`` helper."""

    def test_minimal_payload_with_previous_part(self) -> None:
        payload = _build_move_to_document_payload(
            project_id="MyProj",
            target_space_id="Requirements",
            target_document_name="SRS",
            previous_part_id="workitem_MCPT-001",
            next_part_id=None,
        )

        # NOT JSON:API — flat object with two top-level keys.
        assert payload == {
            "targetDocument": "MyProj/Requirements/SRS",
            "previousPart": "MyProj/Requirements/SRS/workitem_MCPT-001",
        }

    def test_minimal_payload_with_next_part(self) -> None:
        payload = _build_move_to_document_payload(
            project_id="MyProj",
            target_space_id="_default",
            target_document_name="My Doc",
            previous_part_id=None,
            next_part_id="heading_MCPT-9",
        )

        assert payload == {
            "targetDocument": "MyProj/_default/My Doc",
            "nextPart": "MyProj/_default/My Doc/heading_MCPT-9",
        }
        # previousPart and nextPart are mutually exclusive in the body.
        assert "previousPart" not in payload

    def test_document_name_with_slashes_preserved_verbatim(self) -> None:
        # JSON body IDs must NOT be URL-encoded — only URL paths are.
        payload = _build_move_to_document_payload(
            project_id="MyProj",
            target_space_id="Design",
            target_document_name="Folder/Sub Doc",
            previous_part_id="workitem_MCPT-2",
            next_part_id=None,
        )

        assert payload["targetDocument"] == "MyProj/Design/Folder/Sub Doc"
        assert payload["previousPart"] == "MyProj/Design/Folder/Sub Doc/workitem_MCPT-2"

    def test_helper_rejects_neither_position(self) -> None:
        # Defensive guard: the tool layer validates first, but a future
        # direct caller must not be able to produce a ".../None" literal.
        with pytest.raises(ValueError, match="exactly one"):
            _build_move_to_document_payload(
                project_id="MyProj",
                target_space_id="S",
                target_document_name="D",
                previous_part_id=None,
                next_part_id=None,
            )

    def test_helper_rejects_both_positions(self) -> None:
        with pytest.raises(ValueError, match="exactly one"):
            _build_move_to_document_payload(
                project_id="MyProj",
                target_space_id="S",
                target_document_name="D",
                previous_part_id="workitem_MCPT-2",
                next_part_id="workitem_MCPT-3",
            )


# ---------------------------------------------------------------------------
# move_work_item_to_document — position validation
# ---------------------------------------------------------------------------


class TestMoveWorkItemToDocumentPositionValidation:
    """Tests for the exactly-one-of-position rule."""

    async def test_neither_position_raises_value_error(
        self, mock_ctx: MagicMock, mock_client: AsyncMock
    ) -> None:
        with pytest.raises(ValueError, match="Exactly one"):
            await move_work_item_to_document(
                mock_ctx,
                project_id="MyProj",
                work_item_id="MCPT-1",
                target_space_id="S",
                target_document_name="D",
                previous_part_id=None,
                next_part_id=None,
                dry_run=False,
            )
        mock_client.post.assert_not_called()

    async def test_both_positions_raise_value_error(
        self, mock_ctx: MagicMock, mock_client: AsyncMock
    ) -> None:
        with pytest.raises(ValueError, match="Exactly one"):
            await move_work_item_to_document(
                mock_ctx,
                project_id="MyProj",
                work_item_id="MCPT-1",
                target_space_id="S",
                target_document_name="D",
                previous_part_id="workitem_MCPT-2",
                next_part_id="workitem_MCPT-3",
                dry_run=False,
            )
        mock_client.post.assert_not_called()

    async def test_previous_only_passes_validation(
        self, mock_ctx: MagicMock, mock_client: AsyncMock
    ) -> None:
        result = await move_work_item_to_document(
            mock_ctx,
            project_id="MyProj",
            work_item_id="MCPT-1",
            target_space_id="S",
            target_document_name="D",
            previous_part_id="workitem_MCPT-2",
            next_part_id=None,
            dry_run=True,
        )
        assert result.dry_run is True

    async def test_next_only_passes_validation(
        self, mock_ctx: MagicMock, mock_client: AsyncMock
    ) -> None:
        result = await move_work_item_to_document(
            mock_ctx,
            project_id="MyProj",
            work_item_id="MCPT-1",
            target_space_id="S",
            target_document_name="D",
            previous_part_id=None,
            next_part_id="workitem_MCPT-2",
            dry_run=True,
        )
        assert result.dry_run is True


# ---------------------------------------------------------------------------
# move_work_item_to_document — dry run
# ---------------------------------------------------------------------------


class TestMoveWorkItemToDocumentDryRun:
    """Tests for ``move_work_item_to_document`` with ``dry_run=True``."""

    async def test_dry_run_returns_payload_without_calling_post(
        self, mock_ctx: MagicMock, mock_client: AsyncMock
    ) -> None:
        result = await move_work_item_to_document(
            mock_ctx,
            project_id="MyProj",
            work_item_id="MCPT-42",
            target_space_id="Requirements",
            target_document_name="SRS",
            previous_part_id="workitem_MCPT-1",
            next_part_id=None,
            dry_run=True,
        )

        mock_client.post.assert_not_called()
        assert isinstance(result, WorkItemMoveResult)
        assert result.moved is False
        assert result.dry_run is True
        assert result.payload_preview is not None
        assert isinstance(result.payload_preview, dict)
        assert result.payload_preview["targetDocument"] == "MyProj/Requirements/SRS"


# ---------------------------------------------------------------------------
# move_work_item_to_document — happy path
# ---------------------------------------------------------------------------


class TestMoveWorkItemToDocumentHappyPath:
    """Tests for a successful ``move_work_item_to_document`` call."""

    async def test_returns_moved_true_on_204(
        self, mock_ctx: MagicMock, mock_client: AsyncMock
    ) -> None:
        # client.post returns {} on 204 No Content.
        mock_client.post.return_value = {}

        result = await move_work_item_to_document(
            mock_ctx,
            project_id="MyProj",
            work_item_id="MCPT-42",
            target_space_id="Requirements",
            target_document_name="SRS",
            previous_part_id="workitem_MCPT-1",
            next_part_id=None,
            dry_run=False,
        )

        assert isinstance(result, WorkItemMoveResult)
        assert result.moved is True
        assert result.dry_run is False
        assert result.payload_preview is None

    async def test_post_called_with_correct_path_and_body(
        self, mock_ctx: MagicMock, mock_client: AsyncMock
    ) -> None:
        mock_client.post.return_value = {}

        await move_work_item_to_document(
            mock_ctx,
            project_id="MyProj",
            work_item_id="MCPT-42",
            target_space_id="Requirements",
            target_document_name="My Doc",
            previous_part_id="workitem_MCPT-1",
            next_part_id=None,
            dry_run=False,
        )

        args, kwargs = mock_client.post.call_args
        # Path uses the WI ID, with URL-encoded segments.
        expected_path = "/projects/MyProj/workitems/MCPT-42/actions/moveToDocument"
        assert args == (expected_path,)
        body = kwargs["json"]
        assert body == {
            "targetDocument": "MyProj/Requirements/My Doc",
            "previousPart": "MyProj/Requirements/My Doc/workitem_MCPT-1",
        }

    async def test_path_url_encodes_special_chars_in_project_id(
        self, mock_ctx: MagicMock, mock_client: AsyncMock
    ) -> None:
        # Defensive: ensure encode_path_segment is applied to project_id
        # path segment.
        mock_client.post.return_value = {}

        await move_work_item_to_document(
            mock_ctx,
            project_id="My Proj",
            work_item_id="MCPT-1",
            target_space_id="S",
            target_document_name="D",
            previous_part_id="workitem_MCPT-2",
            next_part_id=None,
            dry_run=False,
        )

        args, _ = mock_client.post.call_args
        assert args == ("/projects/My%20Proj/workitems/MCPT-1/actions/moveToDocument",)


# ---------------------------------------------------------------------------
# move_work_item_to_document — error mapping
# ---------------------------------------------------------------------------


class TestMoveWorkItemToDocumentErrorMapping:
    """Tests that domain exceptions are mapped at the tool layer."""

    async def test_401_raises_permission_error(
        self, mock_ctx: MagicMock, mock_client: AsyncMock
    ) -> None:
        mock_client.post.side_effect = PolarionAuthError("auth", status_code=401)

        with pytest.raises(PermissionError):
            await move_work_item_to_document(
                mock_ctx,
                project_id="MyProj",
                work_item_id="MCPT-1",
                target_space_id="S",
                target_document_name="D",
                previous_part_id="workitem_MCPT-2",
                next_part_id=None,
                dry_run=False,
            )

    async def test_404_raises_value_error(
        self, mock_ctx: MagicMock, mock_client: AsyncMock
    ) -> None:
        mock_client.post.side_effect = PolarionNotFoundError(
            "not found", status_code=404
        )

        with pytest.raises(ValueError, match="not found"):
            await move_work_item_to_document(
                mock_ctx,
                project_id="MyProj",
                work_item_id="MCPT-ghost",
                target_space_id="S",
                target_document_name="D",
                previous_part_id="workitem_MCPT-2",
                next_part_id=None,
                dry_run=False,
            )

    async def test_other_error_raises_runtime_error(
        self, mock_ctx: MagicMock, mock_client: AsyncMock
    ) -> None:
        mock_client.post.side_effect = PolarionError("boom", status_code=500)

        with pytest.raises(RuntimeError, match="boom"):
            await move_work_item_to_document(
                mock_ctx,
                project_id="MyProj",
                work_item_id="MCPT-1",
                target_space_id="S",
                target_document_name="D",
                previous_part_id="workitem_MCPT-2",
                next_part_id=None,
                dry_run=False,
            )


# ---------------------------------------------------------------------------
# move_work_item_to_document — Pydantic Field constraints
# ---------------------------------------------------------------------------


class TestMoveWorkItemToDocumentFieldValidation:
    """Verify ``min_length=1`` constraints attached to required parameters."""

    @staticmethod
    def _adapter_for(param_name: str) -> TypeAdapter[object]:
        hints = get_type_hints(move_work_item_to_document)
        sig = inspect.signature(move_work_item_to_document)
        field_info = sig.parameters[param_name].default
        return TypeAdapter(Annotated[hints[param_name], field_info])

    def test_work_item_id_rejects_empty_string(self) -> None:
        with pytest.raises(ValidationError):
            self._adapter_for("work_item_id").validate_python("")

    def test_target_space_id_rejects_empty_string(self) -> None:
        with pytest.raises(ValidationError):
            self._adapter_for("target_space_id").validate_python("")

    def test_target_document_name_rejects_empty_string(self) -> None:
        with pytest.raises(ValidationError):
            self._adapter_for("target_document_name").validate_python("")

    def test_work_item_id_accepts_non_empty(self) -> None:
        assert self._adapter_for("work_item_id").validate_python("MCPT-1") == "MCPT-1"


# ===========================================================================
# update_work_item
# ===========================================================================


# ---------------------------------------------------------------------------
# _build_update_work_item_payload
# ---------------------------------------------------------------------------


class TestBuildUpdateWorkItemPayload:
    """Tests for the private ``_build_update_work_item_payload`` helper."""

    def test_minimal_payload_with_only_title(self) -> None:
        payload = _build_update_work_item_payload(
            project_id="MyProj",
            work_item_id="MCPT-1",
            title="New title",
            description_html=None,
            status=None,
            priority=None,
            severity=None,
            due_date=None,
            initial_estimate=None,
            resolution=None,
            hyperlinks=None,
            assignee_ids=None,
        )

        # PATCH body wraps `data` as a single object, not a list.
        assert payload == {
            "data": {
                "type": "workitems",
                "id": "MyProj/MCPT-1",
                "attributes": {"title": "New title"},
            }
        }
        item = cast(dict[str, object], payload["data"])
        assert "relationships" not in item

    def test_id_is_project_slash_work_item_id(self) -> None:
        payload = _build_update_work_item_payload(
            project_id="proj",
            work_item_id="MCPT-99",
            title="x",
            description_html=None,
            status=None,
            priority=None,
            severity=None,
            due_date=None,
            initial_estimate=None,
            resolution=None,
            hyperlinks=None,
            assignee_ids=None,
        )

        item = cast(dict[str, object], payload["data"])
        assert item["id"] == "proj/MCPT-99"

    def test_skips_none_and_empty_string_fields(self) -> None:
        payload = _build_update_work_item_payload(
            project_id="MyProj",
            work_item_id="MCPT-1",
            title=None,
            description_html="",
            status="",
            priority=None,
            severity="",
            due_date="",
            initial_estimate=None,
            resolution="",
            hyperlinks=[],
            assignee_ids=[],
        )

        # No attributes, no relationships — just the resource header.
        item = cast(dict[str, object], payload["data"])
        assert item == {"type": "workitems", "id": "MyProj/MCPT-1"}

    def test_includes_description_block(self) -> None:
        payload = _build_update_work_item_payload(
            project_id="MyProj",
            work_item_id="MCPT-1",
            title=None,
            description_html="<p>hi</p>",
            status=None,
            priority=None,
            severity=None,
            due_date=None,
            initial_estimate=None,
            resolution=None,
            hyperlinks=None,
            assignee_ids=None,
        )

        item = cast(dict[str, object], payload["data"])
        attrs = cast(dict[str, object], item["attributes"])
        assert attrs["description"] == {
            "type": "text/html",
            "value": "<p>hi</p>",
        }

    def test_assignee_ids_become_to_many_users_relationship(self) -> None:
        payload = _build_update_work_item_payload(
            project_id="MyProj",
            work_item_id="MCPT-1",
            title=None,
            description_html=None,
            status=None,
            priority=None,
            severity=None,
            due_date=None,
            initial_estimate=None,
            resolution=None,
            hyperlinks=None,
            assignee_ids=["alice", "bob"],
        )

        item = cast(dict[str, object], payload["data"])
        rels = cast(dict[str, object], item["relationships"])
        assert rels["assignee"] == {
            "data": [
                {"type": "users", "id": "alice"},
                {"type": "users", "id": "bob"},
            ]
        }

    def test_hyperlinks_serialise_role_title_uri(self) -> None:
        payload = _build_update_work_item_payload(
            project_id="MyProj",
            work_item_id="MCPT-1",
            title=None,
            description_html=None,
            status=None,
            priority=None,
            severity=None,
            due_date=None,
            initial_estimate=None,
            resolution=None,
            hyperlinks=[
                Hyperlink(role="ref_ext", title="Spec", uri="https://example.com"),
            ],
            assignee_ids=None,
        )

        item = cast(dict[str, object], payload["data"])
        attrs = cast(dict[str, object], item["attributes"])
        assert attrs["hyperlinks"] == [
            {"role": "ref_ext", "title": "Spec", "uri": "https://example.com"},
        ]

    def test_all_optional_attrs_included_when_set(self) -> None:
        payload = _build_update_work_item_payload(
            project_id="MyProj",
            work_item_id="MCPT-1",
            title="t",
            description_html=None,
            status="open",
            priority="50.0",
            severity="major",
            due_date="2026-05-31",
            initial_estimate="5 1/2d",
            resolution="fixed",
            hyperlinks=None,
            assignee_ids=None,
        )

        item = cast(dict[str, object], payload["data"])
        attrs = cast(dict[str, object], item["attributes"])
        assert attrs["title"] == "t"
        assert attrs["status"] == "open"
        assert attrs["priority"] == "50.0"
        assert attrs["severity"] == "major"
        assert attrs["dueDate"] == "2026-05-31"
        assert attrs["initialEstimate"] == "5 1/2d"
        assert attrs["resolution"] == "fixed"


# ---------------------------------------------------------------------------
# update_work_item — shared helpers for tool-level tests
# ---------------------------------------------------------------------------


async def _call_update(
    mock_ctx: MagicMock, **overrides: object
) -> WorkItemUpdateResult:
    """Call ``update_work_item`` with safe defaults.

    The tool's ``Field(...)`` defaults stay as ``FieldInfo`` objects when
    invoked outside FastMCP, so every parameter must be passed
    explicitly. This helper supplies plain Python defaults; tests
    override only the parameters they care about.
    """
    defaults: dict[str, object] = {
        "project_id": "MyProj",
        "work_item_id": "MCPT-1",
        "title": None,
        "description": None,
        "status": None,
        "priority": None,
        "severity": None,
        "due_date": None,
        "initial_estimate": None,
        "resolution": None,
        "hyperlinks": None,
        "assignee_ids": None,
        "workflow_action": None,
        "change_type_to": None,
        "dry_run": False,
    }
    defaults.update(overrides)
    return await update_work_item(mock_ctx, **defaults)


# ---------------------------------------------------------------------------
# update_work_item — at-least-one-field validation
# ---------------------------------------------------------------------------


class TestUpdateWorkItemValidation:
    """Tests for the at-least-one-field guard in ``update_work_item``."""

    async def test_no_fields_raises_value_error(
        self, mock_ctx: MagicMock, mock_client: AsyncMock
    ) -> None:
        with pytest.raises(ValueError, match="Nothing to update"):
            await _call_update(mock_ctx)
        mock_client.patch.assert_not_called()
        mock_client.get.assert_not_called()

    async def test_workflow_action_alone_raises_value_error(
        self, mock_ctx: MagicMock, mock_client: AsyncMock
    ) -> None:
        # Polarion rejects PATCH bodies with no attributes/relationships,
        # so workflow_action / change_type_to must be paired with at
        # least one body field. Catch this at the tool layer.
        with pytest.raises(ValueError, match="at least one body field"):
            await _call_update(mock_ctx, workflow_action="close")
        mock_client.patch.assert_not_called()
        mock_client.get.assert_not_called()

    async def test_change_type_to_alone_raises_value_error(
        self, mock_ctx: MagicMock, mock_client: AsyncMock
    ) -> None:
        with pytest.raises(ValueError, match="at least one body field"):
            await _call_update(mock_ctx, change_type_to="defect")
        mock_client.patch.assert_not_called()

    async def test_workflow_action_alone_dry_run_also_rejected(
        self, mock_ctx: MagicMock, mock_client: AsyncMock
    ) -> None:
        # Dry-run is rejected too — the payload that *would* be sent is
        # invalid, so previewing it gives no useful signal.
        with pytest.raises(ValueError, match="at least one body field"):
            await _call_update(mock_ctx, workflow_action="close", dry_run=True)

    async def test_workflow_action_with_title_passes(
        self, mock_ctx: MagicMock, mock_client: AsyncMock
    ) -> None:
        # Pairing the action with any body field satisfies Polarion.
        mock_client.patch.return_value = {}
        mock_client.get.return_value = _make_get_response()

        result = await _call_update(
            mock_ctx,
            workflow_action="close",
            title="closing this WI",
        )

        assert result.updated is True
        patch_path = mock_client.patch.call_args.args[0]
        assert patch_path == "/projects/MyProj/workitems/MCPT-1?workflowAction=close"
        body = mock_client.patch.call_args.kwargs["json"]
        assert body["data"]["attributes"]["title"] == "closing this WI"


# ---------------------------------------------------------------------------
# update_work_item — dry run
# ---------------------------------------------------------------------------


class TestUpdateWorkItemDryRun:
    """Tests for ``update_work_item`` with ``dry_run=True``."""

    async def test_dry_run_does_not_call_polarion(
        self, mock_ctx: MagicMock, mock_client: AsyncMock
    ) -> None:
        result = await _call_update(
            mock_ctx,
            title="New title",
            dry_run=True,
        )

        mock_client.patch.assert_not_called()
        mock_client.get.assert_not_called()
        assert isinstance(result, WorkItemUpdateResult)
        assert result.updated is False
        assert result.dry_run is True
        assert result.current is None
        assert result.changes == {"title": "New title"}
        # payload_preview is populated on dry-run (mirrors create_work_item).
        assert result.payload_preview is not None
        item = cast(dict[str, object], result.payload_preview["data"])
        assert item["id"] == "MyProj/MCPT-1"
        attrs = cast(dict[str, object], item["attributes"])
        assert attrs == {"title": "New title"}

    async def test_changes_uses_python_typed_values_not_json_api_shape(
        self, mock_ctx: MagicMock, mock_client: AsyncMock
    ) -> None:
        # description in `changes` is the original Markdown; the
        # JSON:API HTML wrapping happens only in the wire payload preview.
        result = await _call_update(
            mock_ctx,
            description="**bold**",
            assignee_ids=["alice"],
            dry_run=True,
        )

        assert result.changes == {
            "description": "**bold**",
            "assignee_ids": ["alice"],
        }
        # The wire-shaped preview holds the HTML-wrapped description.
        assert result.payload_preview is not None
        item = cast(dict[str, object], result.payload_preview["data"])
        attrs = cast(dict[str, object], item["attributes"])
        desc = cast(dict[str, object], attrs["description"])
        assert desc["type"] == "text/html"


# ---------------------------------------------------------------------------
# update_work_item — happy path
# ---------------------------------------------------------------------------


def _make_get_response(
    *,
    work_item_id: str = "MCPT-1",
    project_id: str = "MyProj",
    title: str = "after",
    status: str = "open",
    description_html: str = "",
    assignee_ids: list[str] | None = None,
) -> dict[str, object]:
    """Build a minimal JSON:API GET response for the follow-up fetch."""
    rels: dict[str, object] = {}
    if assignee_ids is not None:
        rels["assignee"] = {
            "data": [{"type": "users", "id": uid} for uid in assignee_ids]
        }
    attrs: dict[str, object] = {
        "title": title,
        "type": "task",
        "status": status,
        "priority": "50.0",
        "updated": "2026-05-04T10:00:00Z",
    }
    if description_html:
        attrs["description"] = {"type": "text/html", "value": description_html}
    return {
        "data": {
            "type": "workitems",
            "id": f"{project_id}/{work_item_id}",
            "attributes": attrs,
            "relationships": rels,
        }
    }


class TestUpdateWorkItemHappyPath:
    """Tests for a successful ``update_work_item`` call."""

    async def test_returns_updated_with_post_update_state(
        self, mock_ctx: MagicMock, mock_client: AsyncMock
    ) -> None:
        # PATCH returns {} on 204; GET returns the post-update detail.
        mock_client.patch.return_value = {}
        mock_client.get.return_value = _make_get_response(title="after")

        result = await _call_update(mock_ctx, title="after")

        assert isinstance(result, WorkItemUpdateResult)
        assert result.updated is True
        assert result.dry_run is False
        assert result.current is not None
        assert result.current.title == "after"
        assert result.changes == {"title": "after"}
        assert result.payload_preview is None

    async def test_patch_called_with_correct_path_and_body(
        self, mock_ctx: MagicMock, mock_client: AsyncMock
    ) -> None:
        mock_client.patch.return_value = {}
        mock_client.get.return_value = _make_get_response()

        await _call_update(
            mock_ctx,
            work_item_id="MCPT-42",
            status="open",
            assignee_ids=["alice"],
        )

        args, kwargs = mock_client.patch.call_args
        assert args == ("/projects/MyProj/workitems/MCPT-42",)
        body = kwargs["json"]
        item = body["data"]
        assert item["type"] == "workitems"
        assert item["id"] == "MyProj/MCPT-42"
        assert item["attributes"]["status"] == "open"
        assert item["relationships"]["assignee"]["data"] == [
            {"type": "users", "id": "alice"}
        ]

    async def test_followup_get_called_with_detail_fields(
        self, mock_ctx: MagicMock, mock_client: AsyncMock
    ) -> None:
        mock_client.patch.return_value = {}
        mock_client.get.return_value = _make_get_response()

        await _call_update(mock_ctx, title="t")

        args, kwargs = mock_client.get.call_args
        assert args == ("/projects/MyProj/workitems/MCPT-1",)
        params = kwargs["params"]
        assert params["include"] == "assignee"
        # WI_DETAIL_FIELDS is the bare ``@all`` token so inline custom
        # fields surface on ``current.custom_fields``; this assertion
        # pins that semantics (changing it would silently drop customs).
        assert params["fields[workitems]"] == "@all"

    async def test_current_carries_custom_fields_from_post_patch_get(
        self, mock_ctx: MagicMock, mock_client: AsyncMock
    ) -> None:
        # Polarion inlines customs as top-level attrs; this WI happens to
        # have ``asil`` and ``requirement_id`` populated. The post-PATCH
        # GET reuses ``parse_work_item_detail`` so they must land on
        # ``result.current.custom_fields`` automatically — guarding the
        # cross-tool inheritance the fix relies on.
        mock_client.patch.return_value = {}
        get_response = _make_get_response(title="after")
        data = cast(dict[str, object], get_response["data"])
        attrs = cast(dict[str, object], data["attributes"])
        attrs["asil"] = "B"
        attrs["requirement_id"] = "REQ-42"
        mock_client.get.return_value = get_response

        result = await _call_update(mock_ctx, title="after")

        assert result.current is not None
        assert result.current.custom_fields == {
            "asil": "B",
            "requirement_id": "REQ-42",
        }

    async def test_workflow_action_appended_as_query_param(
        self, mock_ctx: MagicMock, mock_client: AsyncMock
    ) -> None:
        # workflow_action must be paired with a body field (see
        # TestUpdateWorkItemValidation). Pair it with a title here.
        mock_client.patch.return_value = {}
        mock_client.get.return_value = _make_get_response()

        await _call_update(mock_ctx, workflow_action="close", title="t")

        patch_path = mock_client.patch.call_args.args[0]
        assert patch_path == "/projects/MyProj/workitems/MCPT-1?workflowAction=close"
        # Follow-up GET uses the base path (no query) so we always read
        # the canonical detail view.
        get_path = mock_client.get.call_args.args[0]
        assert get_path == "/projects/MyProj/workitems/MCPT-1"

    async def test_change_type_to_appended_as_query_param(
        self, mock_ctx: MagicMock, mock_client: AsyncMock
    ) -> None:
        mock_client.patch.return_value = {}
        mock_client.get.return_value = _make_get_response()

        await _call_update(mock_ctx, change_type_to="task", title="t")

        patch_path = mock_client.patch.call_args.args[0]
        assert patch_path == "/projects/MyProj/workitems/MCPT-1?changeTypeTo=task"

    async def test_description_is_converted_and_sanitized(
        self, mock_ctx: MagicMock, mock_client: AsyncMock
    ) -> None:
        mock_client.patch.return_value = {}
        mock_client.get.return_value = _make_get_response()

        await _call_update(
            mock_ctx,
            description="**bold** [link](https://example.com)",
        )

        body = mock_client.patch.call_args.kwargs["json"]
        desc = body["data"]["attributes"]["description"]
        assert desc["type"] == "text/html"
        assert "<strong>bold</strong>" in desc["value"]
        assert 'href="https://example.com"' in desc["value"]

    async def test_description_strips_dangerous_link_schemes(
        self, mock_ctx: MagicMock, mock_client: AsyncMock
    ) -> None:
        mock_client.patch.return_value = {}
        mock_client.get.return_value = _make_get_response()

        await _call_update(
            mock_ctx,
            description="[click](javascript:alert(1))",
        )

        body = mock_client.patch.call_args.kwargs["json"]
        desc_html = body["data"]["attributes"]["description"]["value"]
        assert 'href="javascript:' not in desc_html
        assert "href='javascript:" not in desc_html

    async def test_path_url_encodes_special_chars(
        self, mock_ctx: MagicMock, mock_client: AsyncMock
    ) -> None:
        mock_client.patch.return_value = {}
        mock_client.get.return_value = _make_get_response()

        await _call_update(mock_ctx, project_id="My Proj", title="t")

        assert (
            mock_client.patch.call_args.args[0]
            == "/projects/My%20Proj/workitems/MCPT-1"
        )


# ---------------------------------------------------------------------------
# update_work_item — error mapping
# ---------------------------------------------------------------------------


class TestUpdateWorkItemErrorMapping:
    """Tests that domain exceptions are mapped at the tool layer."""

    async def test_patch_401_raises_permission_error(
        self, mock_ctx: MagicMock, mock_client: AsyncMock
    ) -> None:
        mock_client.patch.side_effect = PolarionAuthError("auth", status_code=401)

        with pytest.raises(PermissionError):
            await _call_update(mock_ctx, title="t")
        mock_client.get.assert_not_called()

    async def test_patch_404_raises_value_error(
        self, mock_ctx: MagicMock, mock_client: AsyncMock
    ) -> None:
        mock_client.patch.side_effect = PolarionNotFoundError(
            "not found", status_code=404
        )

        with pytest.raises(ValueError, match="not found"):
            await _call_update(mock_ctx, work_item_id="ghost", title="t")

    async def test_patch_other_error_raises_runtime_error(
        self, mock_ctx: MagicMock, mock_client: AsyncMock
    ) -> None:
        mock_client.patch.side_effect = PolarionError("boom", status_code=500)

        with pytest.raises(RuntimeError, match="boom"):
            await _call_update(mock_ctx, title="t")

    async def test_followup_get_404_raises_value_error(
        self, mock_ctx: MagicMock, mock_client: AsyncMock
    ) -> None:
        # PATCH succeeds, but the follow-up GET 404s — surface it the
        # same way (very rare race; mostly defensive).
        mock_client.patch.return_value = {}
        mock_client.get.side_effect = PolarionNotFoundError(
            "not found", status_code=404
        )

        with pytest.raises(ValueError, match="not found"):
            await _call_update(mock_ctx, title="t")


# ---------------------------------------------------------------------------
# update_work_item — Pydantic Field constraints
# ---------------------------------------------------------------------------


class TestUpdateWorkItemFieldValidation:
    """Verify ``min_length=1`` constraints attached to required parameters."""

    @staticmethod
    def _adapter_for(param_name: str) -> TypeAdapter[object]:
        hints = get_type_hints(update_work_item)
        sig = inspect.signature(update_work_item)
        field_info = sig.parameters[param_name].default
        return TypeAdapter(Annotated[hints[param_name], field_info])

    def test_project_id_rejects_empty_string(self) -> None:
        with pytest.raises(ValidationError):
            self._adapter_for("project_id").validate_python("")

    def test_work_item_id_rejects_empty_string(self) -> None:
        with pytest.raises(ValidationError):
            self._adapter_for("work_item_id").validate_python("")

    def test_project_id_accepts_non_empty(self) -> None:
        assert self._adapter_for("project_id").validate_python("p") == "p"

    def test_work_item_id_accepts_non_empty(self) -> None:
        assert self._adapter_for("work_item_id").validate_python("MCPT-1") == "MCPT-1"


# ===========================================================================
# update_document
# ===========================================================================


# ---------------------------------------------------------------------------
# _build_update_document_payload
# ---------------------------------------------------------------------------


class TestBuildUpdateDocumentPayload:
    """Tests for the private ``_build_update_document_payload`` helper."""

    def test_only_set_fields_appear_in_attributes(self) -> None:
        # Skip-None semantics: omitted fields are not serialized so
        # JSON:API omit-preserve takes effect server-side.
        payload = _build_update_document_payload(
            project_id="MyProj",
            space_id="Requirements",
            document_name="SRS",
            title="New Title",
            status=None,
            type=None,
        )

        # data is a single dict (PATCH-shape), NOT a list.
        assert payload == {
            "data": {
                "type": "documents",
                "id": "MyProj/Requirements/SRS",
                "attributes": {"title": "New Title"},
            }
        }
        assert isinstance(payload["data"], dict)

    def test_all_three_fields_serialised_when_set(self) -> None:
        payload = _build_update_document_payload(
            project_id="MyProj",
            space_id="S",
            document_name="D",
            title="T",
            status="approved",
            type="req_specification",
        )

        data = cast(dict[str, object], payload["data"])
        attrs = cast(dict[str, object], data["attributes"])
        assert attrs == {
            "title": "T",
            "status": "approved",
            "type": "req_specification",
        }

    def test_no_attributes_when_all_fields_are_none(self) -> None:
        # Helper produces a body with no ``attributes`` key when every
        # field is None. The tool layer rejects this case before
        # reaching the helper, but a future direct caller should not
        # silently emit an empty PATCH body.
        payload = _build_update_document_payload(
            project_id="MyProj",
            space_id="S",
            document_name="D",
            title=None,
            status=None,
            type=None,
        )

        data = cast(dict[str, object], payload["data"])
        assert "attributes" not in data
        assert data["id"] == "MyProj/S/D"

    def test_homepagecontent_not_emitted_under_any_input(self) -> None:
        # Body editing was intentionally removed from update_document.
        payload = _build_update_document_payload(
            project_id="MyProj",
            space_id="S",
            document_name="D",
            title="T",
            status="approved",
            type="generic",
        )
        body_str = repr(payload)
        assert "homePageContent" not in body_str

    def test_document_name_with_slashes_preserved_verbatim(self) -> None:
        # JSON body IDs must NOT be URL-encoded.
        payload = _build_update_document_payload(
            project_id="MyProj",
            space_id="Design",
            document_name="Folder/Sub Doc",
            title="t",
            status=None,
            type=None,
        )

        data = cast(dict[str, object], payload["data"])
        assert data["id"] == "MyProj/Design/Folder/Sub Doc"


# ---------------------------------------------------------------------------
# update_document — at-least-one-field validation
# ---------------------------------------------------------------------------


class TestUpdateDocumentValidation:
    """Tool-layer validation that protects against empty / no-op PATCHes."""

    async def test_no_fields_raises_value_error(
        self, mock_ctx: MagicMock, mock_client: AsyncMock
    ) -> None:
        with pytest.raises(ValueError, match="at least one"):
            await update_document(
                mock_ctx,
                project_id="MyProj",
                space_id="S",
                document_name="D",
                title=None,
                status=None,
                type=None,
                workflow_action=None,
                dry_run=True,
            )
        mock_client.patch.assert_not_called()

    async def test_workflow_action_alone_raises_value_error(
        self, mock_ctx: MagicMock, mock_client: AsyncMock
    ) -> None:
        with pytest.raises(ValueError, match="workflow_action alone"):
            await update_document(
                mock_ctx,
                project_id="MyProj",
                space_id="S",
                document_name="D",
                title=None,
                status=None,
                type=None,
                workflow_action="approve",
                dry_run=True,
            )
        mock_client.patch.assert_not_called()

    async def test_workflow_action_with_status_passes(
        self, mock_ctx: MagicMock, mock_client: AsyncMock
    ) -> None:
        # workflow_action paired with at least one attribute is OK.
        result = await update_document(
            mock_ctx,
            project_id="MyProj",
            space_id="S",
            document_name="D",
            title=None,
            status="approved",
            type=None,
            workflow_action="approve",
            dry_run=True,
        )
        assert result.dry_run is True


# ---------------------------------------------------------------------------
# update_document — dry run
# ---------------------------------------------------------------------------


class TestUpdateDocumentDryRun:
    """Tests for ``update_document`` with ``dry_run=True``."""

    async def test_dry_run_returns_payload_without_calling_patch(
        self, mock_ctx: MagicMock, mock_client: AsyncMock
    ) -> None:
        result = await update_document(
            mock_ctx,
            project_id="MyProj",
            space_id="Requirements",
            document_name="SRS",
            title="New Title",
            status=None,
            type=None,
            workflow_action=None,
            dry_run=True,
        )

        mock_client.patch.assert_not_called()
        assert isinstance(result, DocumentUpdateResult)
        assert result.updated is False
        assert result.dry_run is True
        assert result.payload_preview is not None
        data = cast(dict[str, object], result.payload_preview["data"])
        assert data["type"] == "documents"
        attrs = cast(dict[str, object], data["attributes"])
        assert attrs == {"title": "New Title"}


# ---------------------------------------------------------------------------
# update_document — happy path
# ---------------------------------------------------------------------------


class TestUpdateDocumentHappyPath:
    """Tests for a successful ``update_document`` call."""

    async def test_returns_updated_true_on_204(
        self, mock_ctx: MagicMock, mock_client: AsyncMock
    ) -> None:
        mock_client.patch.return_value = {}

        result = await update_document(
            mock_ctx,
            project_id="MyProj",
            space_id="Requirements",
            document_name="SRS",
            title="New Title",
            status=None,
            type=None,
            workflow_action=None,
            dry_run=False,
        )

        assert isinstance(result, DocumentUpdateResult)
        assert result.updated is True
        assert result.dry_run is False
        assert result.payload_preview is None

    async def test_patch_called_with_correct_path_and_body(
        self, mock_ctx: MagicMock, mock_client: AsyncMock
    ) -> None:
        mock_client.patch.return_value = {}

        await update_document(
            mock_ctx,
            project_id="MyProj",
            space_id="Requirements",
            document_name="My Doc",
            title="T",
            status=None,
            type=None,
            workflow_action=None,
            dry_run=False,
        )

        args, kwargs = mock_client.patch.call_args
        expected_path = "/projects/MyProj/spaces/Requirements/documents/My%20Doc"
        assert args == (expected_path,)
        body = kwargs["json"]
        assert isinstance(body["data"], dict)
        assert body["data"]["id"] == "MyProj/Requirements/My Doc"
        assert body["data"]["attributes"] == {"title": "T"}

    async def test_workflow_action_appended_as_query_param(
        self, mock_ctx: MagicMock, mock_client: AsyncMock
    ) -> None:
        mock_client.patch.return_value = {}

        await update_document(
            mock_ctx,
            project_id="MyProj",
            space_id="S",
            document_name="D",
            title=None,
            status="approved",
            type=None,
            workflow_action="approve",
            dry_run=False,
        )

        args, _ = mock_client.patch.call_args
        path = args[0]
        assert path.startswith("/projects/MyProj/spaces/S/documents/D")
        assert "workflowAction=approve" in path

    async def test_homepagecontent_never_in_request_body(
        self, mock_ctx: MagicMock, mock_client: AsyncMock
    ) -> None:
        # Sanity: regardless of inputs, body editing is not exposed.
        mock_client.patch.return_value = {}

        await update_document(
            mock_ctx,
            project_id="MyProj",
            space_id="S",
            document_name="D",
            title="T",
            status="approved",
            type="generic",
            workflow_action=None,
            dry_run=False,
        )

        _, kwargs = mock_client.patch.call_args
        body_str = repr(kwargs["json"])
        assert "homePageContent" not in body_str

    async def test_explicit_empty_title_is_serialized(
        self, mock_ctx: MagicMock, mock_client: AsyncMock
    ) -> None:
        # ``title=""`` differs from ``title=None``: the empty string
        # passes the at-least-one check and IS sent in attributes,
        # clearing the title server-side.
        mock_client.patch.return_value = {}

        await update_document(
            mock_ctx,
            project_id="MyProj",
            space_id="S",
            document_name="D",
            title="",
            status=None,
            type=None,
            workflow_action=None,
            dry_run=False,
        )

        _, kwargs = mock_client.patch.call_args
        attrs = kwargs["json"]["data"]["attributes"]
        assert attrs == {"title": ""}

    async def test_path_url_encodes_special_chars_in_space_id(
        self, mock_ctx: MagicMock, mock_client: AsyncMock
    ) -> None:
        mock_client.patch.return_value = {}

        await update_document(
            mock_ctx,
            project_id="MyProj",
            space_id="My Space",
            document_name="D",
            title="t",
            status=None,
            type=None,
            workflow_action=None,
            dry_run=False,
        )

        args, _ = mock_client.patch.call_args
        assert args == ("/projects/MyProj/spaces/My%20Space/documents/D",)

    async def test_workflow_action_url_encoded_when_special(
        self, mock_ctx: MagicMock, mock_client: AsyncMock
    ) -> None:
        # urlencode is responsible for escaping action IDs that contain
        # whitespace or other reserved chars; this locks the contract.
        mock_client.patch.return_value = {}

        await update_document(
            mock_ctx,
            project_id="MyProj",
            space_id="S",
            document_name="D",
            title=None,
            status="approved",
            type=None,
            workflow_action="needs review",
            dry_run=False,
        )

        args, _ = mock_client.patch.call_args
        path = args[0]
        # Space in action ID -> "+" or "%20"; both are valid URL
        # encodings and Polarion accepts either.
        assert "workflowAction=needs+review" in path or (
            "workflowAction=needs%20review" in path
        )


# ---------------------------------------------------------------------------
# update_document — error mapping
# ---------------------------------------------------------------------------


class TestUpdateDocumentErrorMapping:
    """Tests that domain exceptions are mapped at the tool layer."""

    async def test_401_raises_permission_error(
        self, mock_ctx: MagicMock, mock_client: AsyncMock
    ) -> None:
        mock_client.patch.side_effect = PolarionAuthError("auth", status_code=401)

        with pytest.raises(PermissionError):
            await update_document(
                mock_ctx,
                project_id="MyProj",
                space_id="S",
                document_name="D",
                title="t",
                status=None,
                type=None,
                workflow_action=None,
                dry_run=False,
            )

    async def test_404_raises_value_error_with_doc_in_message(
        self, mock_ctx: MagicMock, mock_client: AsyncMock
    ) -> None:
        mock_client.patch.side_effect = PolarionNotFoundError(
            "not found", status_code=404
        )

        with pytest.raises(ValueError, match="ghost-doc") as exc_info:
            await update_document(
                mock_ctx,
                project_id="MyProj",
                space_id="ghost-space",
                document_name="ghost-doc",
                title="t",
                status=None,
                type=None,
                workflow_action=None,
                dry_run=False,
            )
        assert "ghost-space" in str(exc_info.value)

    async def test_other_error_raises_runtime_error(
        self, mock_ctx: MagicMock, mock_client: AsyncMock
    ) -> None:
        mock_client.patch.side_effect = PolarionError("boom", status_code=500)

        with pytest.raises(RuntimeError, match="boom"):
            await update_document(
                mock_ctx,
                project_id="MyProj",
                space_id="S",
                document_name="D",
                title="t",
                status=None,
                type=None,
                workflow_action=None,
                dry_run=False,
            )


# ---------------------------------------------------------------------------
# update_document — Pydantic Field constraints
# ---------------------------------------------------------------------------


class TestUpdateDocumentFieldValidation:
    """Verify ``min_length=1`` constraints on required path parameters."""

    @staticmethod
    def _adapter_for(param_name: str) -> TypeAdapter[object]:
        hints = get_type_hints(update_document)
        sig = inspect.signature(update_document)
        field_info = sig.parameters[param_name].default
        return TypeAdapter(Annotated[hints[param_name], field_info])

    def test_space_id_rejects_empty_string(self) -> None:
        with pytest.raises(ValidationError):
            self._adapter_for("space_id").validate_python("")

    def test_document_name_rejects_empty_string(self) -> None:
        with pytest.raises(ValidationError):
            self._adapter_for("document_name").validate_python("")

    def test_optional_metadata_fields_accept_none(self) -> None:
        for name in ("title", "status", "type", "workflow_action"):
            assert self._adapter_for(name).validate_python(None) is None


# ---------------------------------------------------------------------------
# MCP tool annotations
# ---------------------------------------------------------------------------


class TestWriteToolAnnotations:
    """Verify each write tool advertises the expected MCP annotations.

    Annotations let MCP clients display risk hints (destructive/idempotent)
    and apply per-tool auto-approval policies. Read tools advertise
    ``readOnlyHint=True``; write tools must mirror with the inverse plus
    ``destructiveHint`` / ``idempotentHint`` / ``openWorldHint``.
    """

    @staticmethod
    async def _annotations_for(tool_name: str) -> object:
        tools = await mcp.list_tools()
        for tool in tools:
            if tool.name == tool_name:
                return tool.annotations
        msg = f"tool {tool_name!r} not registered on FastMCP instance"
        raise AssertionError(msg)

    @pytest.mark.parametrize(
        ("tool_name", "expected"),
        [
            (
                "create_work_item",
                {
                    "readOnlyHint": False,
                    "destructiveHint": False,
                    "idempotentHint": False,
                    "openWorldHint": True,
                },
            ),
            (
                "update_work_item",
                {
                    "readOnlyHint": False,
                    "destructiveHint": True,
                    "idempotentHint": True,
                    "openWorldHint": True,
                },
            ),
            (
                "move_work_item_to_document",
                {
                    "readOnlyHint": False,
                    "destructiveHint": True,
                    "idempotentHint": False,
                    "openWorldHint": True,
                },
            ),
            (
                "update_document",
                {
                    "readOnlyHint": False,
                    "destructiveHint": True,
                    "idempotentHint": True,
                    "openWorldHint": True,
                },
            ),
        ],
    )
    async def test_write_tool_annotation(
        self,
        tool_name: str,
        expected: dict[str, bool],
    ) -> None:
        annotations = await self._annotations_for(tool_name)
        for key, value in expected.items():
            assert getattr(annotations, key) is value, (
                f"{tool_name}.{key} expected {value}, got {getattr(annotations, key)}"
            )
