"""Tests for the write MCP tools.

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
    DocumentCommentSpec,
    DocumentCommentUpdateResult,
    DocumentCreateResult,
    DocumentUpdateResult,
    Hyperlink,
    WorkItemCreateSpec,
    WorkItemLinkRef,
    WorkItemLinksCreateResult,
    WorkItemLinksDeleteResult,
    WorkItemLinkSpec,
    WorkItemLinkUpdateResult,
    WorkItemLinkUpdateSpec,
    WorkItemMoveResult,
    WorkItemsCreateResult,
    WorkItemUpdateResult,
)
from mcp_server_polarion.server import mcp
from mcp_server_polarion.tools import _cache as _cache_mod
from mcp_server_polarion.tools import write as _write_mod

# ``@mcp.tool`` returns the original function unchanged (not a FunctionTool
# wrapper), so the tool callables are referenced directly.

# Public tool callables (alphabetical).
create_document = _write_mod.create_document
create_document_comments = _write_mod.create_document_comments
create_work_item_links = _write_mod.create_work_item_links
create_work_items = _write_mod.create_work_items
delete_work_item_links = _write_mod.delete_work_item_links
move_work_item_from_document = _write_mod.move_work_item_from_document
move_work_item_to_document = _write_mod.move_work_item_to_document
update_document = _write_mod.update_document
update_document_comment = _write_mod.update_document_comment
update_work_item = _write_mod.update_work_item
update_work_item_link = _write_mod.update_work_item_link

# Private payload builders (alphabetical).
_build_create_document_payload = _write_mod._build_create_document_payload
_build_create_links_payload = _write_mod._build_create_links_payload
_build_create_work_items_payload = _write_mod._build_create_work_items_payload
_build_delete_links_payload = _write_mod._build_delete_links_payload
_build_document_comment_update_payload = (
    _write_mod._build_document_comment_update_payload
)
_build_document_comments_payload = _write_mod._build_document_comments_payload
_build_move_to_document_payload = _write_mod._build_move_to_document_payload
_build_update_document_payload = _write_mod._build_update_document_payload
_build_update_link_payload = _write_mod._build_update_link_payload
_build_update_work_item_payload = _write_mod._build_update_work_item_payload
_build_work_item_resource = _write_mod._build_work_item_resource

# Private response extractors (alphabetical).
_extract_created_link_ids = _write_mod._extract_created_link_ids
_extract_created_work_item_ids = _write_mod._extract_created_work_item_ids


def _clear_guard_caches() -> None:
    """Drop the guard caches owned by ``tools/_cache.py``."""
    _cache_mod._enum_option_cache.clear()
    _cache_mod._work_item_custom_key_cache.clear()
    _cache_mod._document_custom_key_cache.clear()


@pytest.fixture(autouse=True)
def _reset_guard_caches() -> None:
    """Start every write test with cold enum/custom-field guard caches.

    The guards memoise option ids and observed custom-field keys in
    module-level caches; without a reset, a key primed by one test would
    leak into the next and mask a missing priming GET.
    """
    _clear_guard_caches()


@pytest.fixture
def mock_client() -> AsyncMock:
    """Return a mock PolarionClient with async methods."""
    client = AsyncMock(spec=PolarionClient)
    client.post = AsyncMock()
    client.patch = AsyncMock()
    client.get = AsyncMock()
    client.delete = AsyncMock()
    return client


@pytest.fixture
def mock_ctx(mock_client: AsyncMock) -> MagicMock:
    """Return a mock FastMCP Context with the mock client."""
    ctx = MagicMock()
    ctx.lifespan_context = {
        "polarion_client": mock_client,
    }
    return ctx


class TestBuildWorkItemResource:
    """Tests for the private ``_build_work_item_resource`` helper (one resource)."""

    def test_minimal_item_has_only_required_attrs(self) -> None:
        item = _build_work_item_resource(
            spec=WorkItemCreateSpec(title="My work item", type="task"),
            description_html="",
        )

        assert item == {
            "type": "workitems",
            "attributes": {"title": "My work item", "type": "task"},
        }
        # No relationships key, no description, no other attributes.
        assert "relationships" not in item
        attributes = cast(dict[str, object], item["attributes"])
        assert set(attributes.keys()) == {"title", "type"}

    def test_skips_none_and_empty_string_fields(self) -> None:
        item = _build_work_item_resource(
            spec=WorkItemCreateSpec(
                title="x",
                type="task",
                status="",
                severity="",
                assignee_ids=[],
                due_date="",
                hyperlinks=[],
            ),
            description_html="",
        )

        attributes = cast(dict[str, object], item["attributes"])
        # Only title + type — nothing else slipped through.
        assert set(attributes.keys()) == {"title", "type"}
        assert "relationships" not in item

    def test_includes_description_block(self) -> None:
        item = _build_work_item_resource(
            spec=WorkItemCreateSpec(title="x", type="task"),
            description_html="<p>hello</p>",
        )

        attributes = cast(dict[str, object], item["attributes"])
        assert attributes["description"] == {
            "type": "text/html",
            "value": "<p>hello</p>",
        }

    def test_assignee_ids_become_to_many_users_relationship(self) -> None:
        item = _build_work_item_resource(
            spec=WorkItemCreateSpec(
                title="x", type="task", assignee_ids=["alice", "bob"]
            ),
            description_html="",
        )

        relationships = cast(dict[str, object], item["relationships"])
        assert relationships["assignee"] == {
            "data": [
                {"type": "users", "id": "alice"},
                {"type": "users", "id": "bob"},
            ]
        }

    def test_hyperlinks_serialise_role_title_uri(self) -> None:
        item = _build_work_item_resource(
            spec=WorkItemCreateSpec(
                title="x",
                type="task",
                hyperlinks=[
                    Hyperlink(role="ref_ext", title="Spec", uri="https://example.com"),
                    Hyperlink(role="implementation", uri="https://example.com/code"),
                ],
            ),
            description_html="",
        )

        attributes = cast(dict[str, object], item["attributes"])
        assert attributes["hyperlinks"] == [
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
        item = _build_work_item_resource(
            spec=WorkItemCreateSpec(
                title="x",
                type="task",
                status="open",
                priority="50.0",
                severity="major",
                due_date="2026-05-31",
                initial_estimate="5 1/2d",
            ),
            description_html="",
        )

        attributes = cast(dict[str, object], item["attributes"])
        assert attributes["status"] == "open"
        assert attributes["priority"] == "50.0"
        assert attributes["severity"] == "major"
        assert attributes["dueDate"] == "2026-05-31"
        assert attributes["initialEstimate"] == "5 1/2d"

    def test_custom_fields_inlined_alongside_standard_attrs(self) -> None:
        item = _build_work_item_resource(
            spec=WorkItemCreateSpec(
                title="x",
                type="softwarerequirement",
                custom_fields={"riskLevel": "high", "effortHours": 12.0},
            ),
            description_html="",
        )

        attributes = cast(dict[str, object], item["attributes"])
        assert attributes["riskLevel"] == "high"
        assert attributes["effortHours"] == 12.0
        # Customs land flat under attributes, NOT inside a `customFields`
        # container — Polarion silently drops the latter shape.
        assert "customFields" not in attributes

    def test_custom_fields_collision_with_standard_attr_raises(self) -> None:
        # ``title`` is a Polarion-defined standard attribute; collision
        # would silently shadow the explicit ``title`` param. Reject.
        with pytest.raises(ValueError, match="custom_fields keys collide"):
            _build_work_item_resource(
                spec=WorkItemCreateSpec(
                    title="x", type="task", custom_fields={"title": "y"}
                ),
                description_html="",
            )

    def test_custom_fields_skips_none_values_inside_dict(self) -> None:
        # The merge helper already has direct coverage for skip-None;
        # this test pins that the item builder honours the same semantics —
        # a ``None`` value inside the dict MUST NOT land under
        # ``attributes``, while falsy non-``None`` values (e.g. 0) pass.
        item = _build_work_item_resource(
            spec=WorkItemCreateSpec(
                title="t",
                type="task",
                custom_fields={"riskLevel": None, "effortHours": 0},
            ),
            description_html="",
        )
        attributes = cast(dict[str, object], item["attributes"])
        assert "riskLevel" not in attributes
        assert attributes["effortHours"] == 0


class TestBuildCreateWorkItemsPayload:
    """Tests for the bulk ``_build_create_work_items_payload`` wrapper."""

    def test_single_spec_wraps_in_data_list(self) -> None:
        payload = _build_create_work_items_payload(
            specs=[WorkItemCreateSpec(title="one", type="task")],
            descriptions_html=[""],
        )
        assert payload == {
            "data": [
                {"type": "workitems", "attributes": {"title": "one", "type": "task"}}
            ]
        }

    def test_multiple_specs_preserve_order_and_pair_html(self) -> None:
        payload = _build_create_work_items_payload(
            specs=[
                WorkItemCreateSpec(title="a", type="task"),
                WorkItemCreateSpec(title="b", type="task"),
            ],
            descriptions_html=["<p>aaa</p>", ""],
        )
        data = cast(list[dict[str, object]], payload["data"])
        assert len(data) == 2
        first = cast(dict[str, object], data[0]["attributes"])
        second = cast(dict[str, object], data[1]["attributes"])
        assert first["title"] == "a"
        assert first["description"] == {"type": "text/html", "value": "<p>aaa</p>"}
        assert second["title"] == "b"
        assert "description" not in second

    def test_mismatched_lengths_raise(self) -> None:
        # ``zip(strict=True)`` guards the spec/html pairing invariant.
        with pytest.raises(ValueError):
            _build_create_work_items_payload(
                specs=[WorkItemCreateSpec(title="a", type="task")],
                descriptions_html=[],
            )


class TestExtractCreatedWorkItemIds:
    """Tests for the private ``_extract_created_work_item_ids`` helper."""

    def test_extracts_short_ids_in_order(self) -> None:
        response: dict[str, object] = {
            "data": [
                {"type": "workitems", "id": "MyProj/MCPT-042"},
                {"type": "workitems", "id": "MyProj/MCPT-043"},
            ]
        }
        assert _extract_created_work_item_ids(response) == ["MCPT-042", "MCPT-043"]

    def test_returns_empty_when_data_missing(self) -> None:
        assert _extract_created_work_item_ids({}) == []

    def test_returns_empty_when_data_not_a_list(self) -> None:
        assert _extract_created_work_item_ids({"data": {"id": "MyProj/MCPT-1"}}) == []

    def test_skips_entries_missing_id_or_not_dict(self) -> None:
        response: dict[str, object] = {
            "data": [
                {"type": "workitems", "id": "MyProj/MCPT-1"},
                {"type": "workitems"},
                "not a dict",
            ]
        }
        assert _extract_created_work_item_ids(response) == ["MCPT-1"]


class TestCreateWorkItemsDryRun:
    """Tests for ``create_work_items`` with ``dry_run=True``."""

    async def test_dry_run_returns_payload_without_calling_post(
        self, mock_ctx: MagicMock, mock_client: AsyncMock
    ) -> None:
        result = await create_work_items(
            mock_ctx,
            project_id="MyProj",
            items=[WorkItemCreateSpec(title="Dry test", type="task")],
            dry_run=True,
        )

        mock_client.post.assert_not_called()
        assert isinstance(result, WorkItemsCreateResult)
        assert result.dry_run is True
        assert result.created is False
        assert result.work_item_ids == []
        assert result.payload_preview is not None
        # payload_preview is a plain dict (no Pydantic objects leaked).
        assert isinstance(result.payload_preview, dict)
        item = cast(list[dict[str, object]], result.payload_preview["data"])[0]
        attributes = cast(dict[str, object], item["attributes"])
        assert attributes == {"title": "Dry test", "type": "task"}


class TestCreateWorkItemsHappyPath:
    """Tests for a successful ``create_work_items`` call."""

    async def test_single_item_returns_short_id_on_201(
        self, mock_ctx: MagicMock, mock_client: AsyncMock
    ) -> None:
        mock_client.post.return_value = {
            "data": [
                {"type": "workitems", "id": "MyProj/MCPT-042", "links": {"self": "..."}}
            ]
        }

        result = await create_work_items(
            mock_ctx,
            project_id="MyProj",
            items=[WorkItemCreateSpec(title="Real", type="task")],
            dry_run=False,
        )

        assert isinstance(result, WorkItemsCreateResult)
        assert result.created is True
        assert result.dry_run is False
        assert result.work_item_ids == ["MCPT-042"]
        assert result.payload_preview is None

    async def test_multiple_items_return_ids_in_order(
        self, mock_ctx: MagicMock, mock_client: AsyncMock
    ) -> None:
        mock_client.post.return_value = {
            "data": [
                {"type": "workitems", "id": "MyProj/MCPT-1"},
                {"type": "workitems", "id": "MyProj/MCPT-2"},
            ]
        }

        result = await create_work_items(
            mock_ctx,
            project_id="MyProj",
            items=[
                WorkItemCreateSpec(title="a", type="task"),
                WorkItemCreateSpec(title="b", type="task"),
            ],
            dry_run=False,
        )

        assert result.work_item_ids == ["MCPT-1", "MCPT-2"]
        # A single POST creates the whole batch.
        assert mock_client.post.call_count == 1

    async def test_post_called_with_correct_path_and_body(
        self, mock_ctx: MagicMock, mock_client: AsyncMock
    ) -> None:
        mock_client.post.return_value = {
            "data": [{"type": "workitems", "id": "MyProj/MCPT-1"}]
        }

        await create_work_items(
            mock_ctx,
            project_id="MyProj",
            items=[
                WorkItemCreateSpec(
                    title="t", type="task", status="open", assignee_ids=["alice"]
                )
            ],
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

        await create_work_items(
            mock_ctx,
            project_id="MyProj",
            items=[
                WorkItemCreateSpec(
                    title="t",
                    type="task",
                    description="**bold** [link](https://example.com)",
                )
            ],
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

        await create_work_items(
            mock_ctx,
            project_id="MyProj",
            items=[
                WorkItemCreateSpec(
                    title="t", type="task", description="[click](javascript:alert(1))"
                )
            ],
            dry_run=False,
        )

        _, kwargs = mock_client.post.call_args
        desc_html = kwargs["json"]["data"][0]["attributes"]["description"]["value"]
        # No dangerous href attribute — neither markdown-it nor
        # sanitize_html should let one through.
        assert 'href="javascript:' not in desc_html
        assert "href='javascript:" not in desc_html


class TestCreateWorkItemsErrorMapping:
    """Tests that domain exceptions are mapped at the tool layer."""

    async def test_401_raises_permission_error(
        self, mock_ctx: MagicMock, mock_client: AsyncMock
    ) -> None:
        mock_client.post.side_effect = PolarionAuthError("auth", status_code=401)

        with pytest.raises(PermissionError):
            await create_work_items(
                mock_ctx,
                project_id="MyProj",
                items=[WorkItemCreateSpec(title="t", type="task")],
                dry_run=False,
            )

    async def test_404_raises_value_error(
        self, mock_ctx: MagicMock, mock_client: AsyncMock
    ) -> None:
        mock_client.post.side_effect = PolarionNotFoundError(
            "not found", status_code=404
        )

        with pytest.raises(ValueError, match="list_projects"):
            await create_work_items(
                mock_ctx,
                project_id="ghost",
                items=[WorkItemCreateSpec(title="t", type="task")],
                dry_run=False,
            )

    async def test_other_error_raises_runtime_error(
        self, mock_ctx: MagicMock, mock_client: AsyncMock
    ) -> None:
        mock_client.post.side_effect = PolarionError("boom", status_code=500)

        with pytest.raises(RuntimeError, match="boom"):
            await create_work_items(
                mock_ctx,
                project_id="MyProj",
                items=[WorkItemCreateSpec(title="t", type="task")],
                dry_run=False,
            )


class TestCreateWorkItemsResponseParsing:
    """Tests for unexpected / partial 2xx response shapes from Polarion."""

    async def test_id_count_mismatch_raises_runtime_error(
        self, mock_ctx: MagicMock, mock_client: AsyncMock
    ) -> None:
        # Two items submitted, one id back -> possible partial commit.
        mock_client.post.return_value = {
            "data": [{"type": "workitems", "id": "MyProj/MCPT-1"}]
        }

        with pytest.raises(RuntimeError, match="list_work_items"):
            await create_work_items(
                mock_ctx,
                project_id="MyProj",
                items=[
                    WorkItemCreateSpec(title="a", type="task"),
                    WorkItemCreateSpec(title="b", type="task"),
                ],
                dry_run=False,
            )

    async def test_empty_data_array_raises_runtime_error(
        self, mock_ctx: MagicMock, mock_client: AsyncMock
    ) -> None:
        mock_client.post.return_value = {"data": []}

        with pytest.raises(RuntimeError, match="list_work_items"):
            await create_work_items(
                mock_ctx,
                project_id="MyProj",
                items=[WorkItemCreateSpec(title="t", type="task")],
                dry_run=False,
            )

    async def test_data_not_a_list_raises_runtime_error(
        self, mock_ctx: MagicMock, mock_client: AsyncMock
    ) -> None:
        mock_client.post.return_value = {"data": {"id": "MyProj/MCPT-1"}}

        with pytest.raises(RuntimeError, match="list_work_items"):
            await create_work_items(
                mock_ctx,
                project_id="MyProj",
                items=[WorkItemCreateSpec(title="t", type="task")],
                dry_run=False,
            )


class TestCreateWorkItemsFieldValidation:
    """Verify constraints attached to ``items`` and to ``WorkItemCreateSpec``.

    FastMCP enforces these via JSON Schema at the MCP protocol layer
    before the tool function is invoked; calling the function directly
    in unit tests bypasses that gate. The per-item constraints now live
    on the spec model (validated directly), and the collection-level
    ``min_length`` / ``max_length`` are proven by rebuilding a
    ``TypeAdapter`` from the ``items`` parameter annotation + ``FieldInfo``.
    """

    @staticmethod
    def _items_adapter() -> TypeAdapter[object]:
        param_name = "items"
        hints = get_type_hints(create_work_items)
        sig = inspect.signature(create_work_items)
        field_info = sig.parameters[param_name].default
        return TypeAdapter(Annotated[hints[param_name], field_info])

    def test_empty_items_list_rejected(self) -> None:
        with pytest.raises(ValidationError):
            self._items_adapter().validate_python([])

    def test_over_cap_items_list_rejected(self) -> None:
        too_many = [{"title": "t", "type": "task"} for _ in range(51)]
        with pytest.raises(ValidationError):
            self._items_adapter().validate_python(too_many)

    def test_cap_boundary_accepted(self) -> None:
        exactly_50 = [{"title": "t", "type": "task"} for _ in range(50)]
        result = cast(list[object], self._items_adapter().validate_python(exactly_50))
        assert len(result) == 50

    def test_spec_title_rejects_empty_string(self) -> None:
        with pytest.raises(ValidationError):
            WorkItemCreateSpec(title="", type="task")

    def test_spec_type_rejects_empty_string(self) -> None:
        with pytest.raises(ValidationError):
            WorkItemCreateSpec(title="t", type="")

    def test_spec_description_rejects_overlong_input(self) -> None:
        """``max_length`` on the spec defends against runaway Markdown."""
        WorkItemCreateSpec(title="t", type="task", description="hello")
        with pytest.raises(ValidationError):
            WorkItemCreateSpec(
                title="t", type="task", description="x" * (2_000_000 + 1)
            )


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

    def test_payload_omits_position_keys_when_both_none(self) -> None:
        # Per Polarion REST API, omitting both previousPart and nextPart
        # appends the work item at the end of the target document.
        payload = _build_move_to_document_payload(
            project_id="MyProj",
            target_space_id="S",
            target_document_name="D",
            previous_part_id=None,
            next_part_id=None,
        )

        assert payload == {"targetDocument": "MyProj/S/D"}
        assert "previousPart" not in payload
        assert "nextPart" not in payload

    def test_helper_rejects_both_positions(self) -> None:
        with pytest.raises(ValueError, match="at most one"):
            _build_move_to_document_payload(
                project_id="MyProj",
                target_space_id="S",
                target_document_name="D",
                previous_part_id="workitem_MCPT-2",
                next_part_id="workitem_MCPT-3",
            )


class TestMoveWorkItemToDocumentPositionValidation:
    """Tests for the at-most-one-of-position rule."""

    async def test_neither_position_appends_at_end(
        self, mock_ctx: MagicMock, mock_client: AsyncMock
    ) -> None:
        result = await move_work_item_to_document(
            mock_ctx,
            project_id="MyProj",
            work_item_id="MCPT-1",
            target_space_id="S",
            target_document_name="D",
            previous_part_id=None,
            next_part_id=None,
            dry_run=True,
        )
        assert result.dry_run is True
        assert result.payload_preview is not None
        assert "previousPart" not in result.payload_preview
        assert "nextPart" not in result.payload_preview
        mock_client.post.assert_not_called()

    async def test_both_positions_raise_value_error(
        self, mock_ctx: MagicMock, mock_client: AsyncMock
    ) -> None:
        with pytest.raises(ValueError, match="at most one"):
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
        # Path uses the work item ID, with URL-encoded segments.
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


class TestMoveWorkItemFromDocumentDryRun:
    """Tests for ``move_work_item_from_document`` with ``dry_run=True``."""

    async def test_dry_run_returns_empty_payload_without_calling_post(
        self, mock_ctx: MagicMock, mock_client: AsyncMock
    ) -> None:
        result = await move_work_item_from_document(
            mock_ctx,
            project_id="MyProj",
            work_item_id="MCPT-42",
            dry_run=True,
        )

        mock_client.post.assert_not_called()
        assert isinstance(result, WorkItemMoveResult)
        assert result.moved is False
        assert result.dry_run is True
        # moveFromDocument has no body — payload preview is an empty dict.
        assert result.payload_preview == {}


class TestMoveWorkItemFromDocumentHappyPath:
    """Tests for a successful ``move_work_item_from_document`` call."""

    async def test_returns_moved_true_on_204(
        self, mock_ctx: MagicMock, mock_client: AsyncMock
    ) -> None:
        mock_client.post.return_value = {}

        result = await move_work_item_from_document(
            mock_ctx,
            project_id="MyProj",
            work_item_id="MCPT-42",
            dry_run=False,
        )

        assert isinstance(result, WorkItemMoveResult)
        assert result.moved is True
        assert result.dry_run is False
        assert result.payload_preview is None

    async def test_post_called_with_correct_path_and_no_body(
        self, mock_ctx: MagicMock, mock_client: AsyncMock
    ) -> None:
        mock_client.post.return_value = {}

        await move_work_item_from_document(
            mock_ctx,
            project_id="MyProj",
            work_item_id="MCPT-42",
            dry_run=False,
        )

        args, kwargs = mock_client.post.call_args
        expected_path = "/projects/MyProj/workitems/MCPT-42/actions/moveFromDocument"
        assert args == (expected_path,)
        # API spec: "send the request without a request body and any parameters".
        assert kwargs.get("json") is None

    async def test_path_url_encodes_special_chars(
        self, mock_ctx: MagicMock, mock_client: AsyncMock
    ) -> None:
        mock_client.post.return_value = {}

        await move_work_item_from_document(
            mock_ctx,
            project_id="My Proj",
            work_item_id="MCPT-1",
            dry_run=False,
        )

        args, _ = mock_client.post.call_args
        assert args == (
            "/projects/My%20Proj/workitems/MCPT-1/actions/moveFromDocument",
        )


class TestMoveWorkItemFromDocumentErrorMapping:
    """Tests that domain exceptions are mapped at the tool layer."""

    async def test_401_raises_permission_error(
        self, mock_ctx: MagicMock, mock_client: AsyncMock
    ) -> None:
        mock_client.post.side_effect = PolarionAuthError("auth", status_code=401)

        with pytest.raises(PermissionError):
            await move_work_item_from_document(
                mock_ctx,
                project_id="MyProj",
                work_item_id="MCPT-1",
                dry_run=False,
            )

    async def test_404_raises_value_error(
        self, mock_ctx: MagicMock, mock_client: AsyncMock
    ) -> None:
        mock_client.post.side_effect = PolarionNotFoundError(
            "not found", status_code=404
        )

        with pytest.raises(ValueError, match="not found"):
            await move_work_item_from_document(
                mock_ctx,
                project_id="MyProj",
                work_item_id="MCPT-ghost",
                dry_run=False,
            )

    async def test_400_already_detached_raises_runtime_error(
        self, mock_ctx: MagicMock, mock_client: AsyncMock
    ) -> None:
        # Calling moveFromDocument on a work item that is already
        # free-floating returns HTTP 400. Per the standard mapping,
        # 400 → PolarionError → RuntimeError at the tool layer.
        mock_client.post.side_effect = PolarionError(
            "Work item is not in a Document", status_code=400
        )

        with pytest.raises(RuntimeError, match="not in a Document"):
            await move_work_item_from_document(
                mock_ctx,
                project_id="MyProj",
                work_item_id="MCPT-1",
                dry_run=False,
            )

    async def test_other_error_raises_runtime_error(
        self, mock_ctx: MagicMock, mock_client: AsyncMock
    ) -> None:
        mock_client.post.side_effect = PolarionError("boom", status_code=500)

        with pytest.raises(RuntimeError, match="boom"):
            await move_work_item_from_document(
                mock_ctx,
                project_id="MyProj",
                work_item_id="MCPT-1",
                dry_run=False,
            )


class TestMoveWorkItemFromDocumentFieldValidation:
    """Verify ``min_length=1`` on the required ``work_item_id`` parameter."""

    @staticmethod
    def _adapter_for(param_name: str) -> TypeAdapter[object]:
        hints = get_type_hints(move_work_item_from_document)
        sig = inspect.signature(move_work_item_from_document)
        field_info = sig.parameters[param_name].default
        return TypeAdapter(Annotated[hints[param_name], field_info])

    def test_work_item_id_rejects_empty_string(self) -> None:
        with pytest.raises(ValidationError):
            self._adapter_for("work_item_id").validate_python("")

    def test_work_item_id_accepts_non_empty(self) -> None:
        assert self._adapter_for("work_item_id").validate_python("MCPT-1") == "MCPT-1"


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
        attributes = cast(dict[str, object], item["attributes"])
        assert attributes["description"] == {
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
        relationships = cast(dict[str, object], item["relationships"])
        assert relationships["assignee"] == {
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
        attributes = cast(dict[str, object], item["attributes"])
        assert attributes["hyperlinks"] == [
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
        attributes = cast(dict[str, object], item["attributes"])
        assert attributes["title"] == "t"
        assert attributes["status"] == "open"
        assert attributes["priority"] == "50.0"
        assert attributes["severity"] == "major"
        assert attributes["dueDate"] == "2026-05-31"
        assert attributes["initialEstimate"] == "5 1/2d"
        assert attributes["resolution"] == "fixed"

    def test_custom_fields_inlined_in_patch_attributes(self) -> None:
        rich = {"type": "text/html", "value": "<p>note</p>"}
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
            assignee_ids=None,
            custom_fields={"riskLevel": "low", "reviewerNote": rich},
        )

        item = cast(dict[str, object], payload["data"])
        attributes = cast(dict[str, object], item["attributes"])
        assert attributes == {"riskLevel": "low", "reviewerNote": rich}

    def test_custom_fields_alone_keeps_attributes_dict(self) -> None:
        # Without any standard fields, custom_fields alone should still
        # produce an ``attributes`` block (otherwise PATCH 400s).
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
            assignee_ids=None,
            custom_fields={"riskLevel": "high"},
        )
        item = cast(dict[str, object], payload["data"])
        assert "attributes" in item

    def test_custom_fields_collision_raises(self) -> None:
        with pytest.raises(ValueError, match="custom_fields keys collide"):
            _build_update_work_item_payload(
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
                assignee_ids=None,
                custom_fields={"status": "open"},
            )


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
        "description_html": None,
        "status": None,
        "priority": None,
        "severity": None,
        "due_date": None,
        "initial_estimate": None,
        "resolution": None,
        "hyperlinks": None,
        "assignee_ids": None,
        "custom_fields": None,
        "workflow_action": None,
        "change_type_to": None,
        "include_current_description_html": False,
        "dry_run": False,
    }
    defaults.update(overrides)
    return await update_work_item(mock_ctx, **defaults)


class TestUpdateWorkItemValidation:
    """Tests for the at-least-one-field guard in ``update_work_item``."""

    async def test_no_fields_raises_value_error(
        self, mock_ctx: MagicMock, mock_client: AsyncMock
    ) -> None:
        with pytest.raises(ValueError, match="Nothing to update"):
            await _call_update(mock_ctx)
        mock_client.patch.assert_not_called()
        mock_client.get.assert_not_called()

    async def test_empty_description_html_is_noop(
        self, mock_ctx: MagicMock, mock_client: AsyncMock
    ) -> None:
        """``description_html=''`` is "leave unchanged" — never PATCHes.

        Asymmetric vs ``update_document(home_page_content_html='')`` which
        RAISES (see test_home_page_content_html_empty_string_raises). The
        difference is justified by blast radius: clearing a single work item's
        description is recoverable; wiping a document body orphans every
        heading work item inside it.
        """
        with pytest.raises(ValueError, match="Nothing to update"):
            await _call_update(mock_ctx, description_html="")
        mock_client.patch.assert_not_called()
        mock_client.get.assert_not_called()

    async def test_empty_description_html_with_other_field_drops_description(
        self, mock_ctx: MagicMock, mock_client: AsyncMock
    ) -> None:
        """``description_html=''`` is skipped from the PATCH body even when
        paired with other fields — the existing description is preserved."""
        result = await _call_update(
            mock_ctx,
            title="new title",
            description_html="",
            dry_run=True,
        )
        # changes summary excludes the empty description_html.
        assert result.changes == {"title": "new title"}
        # Wire payload has only the title — no description key at all.
        assert result.payload_preview is not None
        item = cast(dict[str, object], result.payload_preview["data"])
        attributes = cast(dict[str, object], item["attributes"])
        assert "description" not in attributes
        assert attributes == {"title": "new title"}

    async def test_custom_fields_alone_satisfies_at_least_one_check(
        self, mock_ctx: MagicMock, mock_client: AsyncMock
    ) -> None:
        # custom_fields counts as a body field — neither title nor any
        # other standard param is required when customs are present. The
        # prefetch primes the custom-key cache so the guard accepts the key.
        mock_client.get.return_value = _make_get_response(
            custom_fields={"riskLevel": "high"}
        )
        result = await _call_update(
            mock_ctx,
            custom_fields={"riskLevel": "high"},
            dry_run=True,
        )
        assert result.dry_run is True
        assert result.changes == {"custom_fields": {"riskLevel": "high"}}

    async def test_collision_raises_value_error(
        self, mock_ctx: MagicMock, mock_client: AsyncMock
    ) -> None:
        # Tool-layer collision detection prevents an explicit standard
        # parameter from being shadowed by a same-named custom key.
        with pytest.raises(ValueError, match="custom_fields keys collide"):
            await _call_update(
                mock_ctx,
                title="x",
                custom_fields={"title": "y"},
                dry_run=True,
            )
        mock_client.patch.assert_not_called()

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
            title="closing this work item",
        )

        assert result.updated is True
        patch_path = mock_client.patch.call_args.args[0]
        assert patch_path == "/projects/MyProj/workitems/MCPT-1?workflowAction=close"
        body = mock_client.patch.call_args.kwargs["json"]
        assert body["data"]["attributes"]["title"] == "closing this work item"


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
        # payload_preview is populated on dry-run (mirrors create_work_items).
        assert result.payload_preview is not None
        item = cast(dict[str, object], result.payload_preview["data"])
        assert item["id"] == "MyProj/MCPT-1"
        attributes = cast(dict[str, object], item["attributes"])
        assert attributes == {"title": "New title"}

    async def test_changes_uses_python_typed_values_not_json_api_shape(
        self, mock_ctx: MagicMock, mock_client: AsyncMock
    ) -> None:
        # description_html in `changes` is the raw HTML the caller passed;
        # the JSON:API ``{type,value}`` wrapping happens only in the wire
        # payload preview.
        result = await _call_update(
            mock_ctx,
            description_html="<p>bold</p>",
            assignee_ids=["alice"],
            dry_run=True,
        )

        assert result.changes == {
            "description_html": "<p>bold</p>",
            "assignee_ids": ["alice"],
        }
        # The wire-shaped preview wraps the same raw HTML — VERBATIM, no
        # sanitization or Markdown conversion in between.
        assert result.payload_preview is not None
        item = cast(dict[str, object], result.payload_preview["data"])
        attributes = cast(dict[str, object], item["attributes"])
        desc = cast(dict[str, object], attributes["description"])
        assert desc == {"type": "text/html", "value": "<p>bold</p>"}


def _make_get_response(
    *,
    work_item_id: str = "MCPT-1",
    project_id: str = "MyProj",
    title: str = "after",
    status: str = "open",
    description_html: str = "",
    assignee_ids: list[str] | None = None,
    custom_fields: dict[str, object] | None = None,
) -> dict[str, object]:
    """Build a minimal JSON:API GET response for the follow-up fetch."""
    relationships: dict[str, object] = {}
    if assignee_ids is not None:
        relationships["assignee"] = {
            "data": [{"type": "users", "id": uid} for uid in assignee_ids]
        }
    attributes: dict[str, object] = {
        "title": title,
        "type": "task",
        "status": status,
        "priority": "50.0",
        "updated": "2026-05-04T10:00:00Z",
    }
    if description_html:
        attributes["description"] = {"type": "text/html", "value": description_html}
    if custom_fields:
        # Inline alongside standard attrs (Polarion's JSON:API shape — no
        # ``customFields`` container). The ``update_work_item`` pre-fetch
        # guard parses these as custom keys, priming the cache so a
        # follow-up update with the same keys is accepted.
        attributes.update(custom_fields)
    return {
        "data": {
            "type": "workitems",
            "id": f"{project_id}/{work_item_id}",
            "attributes": attributes,
            "relationships": relationships,
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

    async def test_current_description_html_blanked_by_default(
        self, mock_ctx: MagicMock, mock_client: AsyncMock
    ) -> None:
        """Default (False) → ``current.description_html`` is ``""``.

        The follow-up GET still returns the body over the wire (Polarion
        @all is the only sparse-fieldset that surfaces customs), but the
        tool layer blanks it so a metadata-only update does not blow up
        LLM context. Caller opts in with
        ``include_current_description_html=True`` when verifying a body
        edit.
        """
        mock_client.patch.return_value = {}
        mock_client.get.return_value = _make_get_response(
            description_html="<p>large body that should be hidden</p>",
        )

        result = await _call_update(mock_ctx, status="approved")

        assert result.current is not None
        assert result.current.description_html == ""
        # Other metadata is unaffected.
        assert result.current.status == "open"  # _make_get_response default

    async def test_current_description_html_kept_when_flag_true(
        self, mock_ctx: MagicMock, mock_client: AsyncMock
    ) -> None:
        """include_current_description_html=True → raw HTML in current."""
        mock_client.patch.return_value = {}
        raw = "<p>verified body <strong>after</strong></p>"
        mock_client.get.return_value = _make_get_response(description_html=raw)

        result = await _call_update(
            mock_ctx,
            description_html=raw,
            include_current_description_html=True,
        )

        assert result.current is not None
        assert result.current.description_html == raw

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
        # WORK_ITEM_DETAIL_FIELDS is the bare ``@all`` token so inline custom
        # fields surface on ``current.custom_fields``; this assertion
        # pins that semantics (changing it would silently drop customs).
        assert params["fields[workitems]"] == "@all"

    async def test_current_carries_custom_fields_from_post_patch_get(
        self, mock_ctx: MagicMock, mock_client: AsyncMock
    ) -> None:
        # Polarion inlines customs as top-level attributes; this work item happens to
        # have ``riskLevel`` and ``effortHours`` populated. The post-PATCH
        # GET reuses ``parse_work_item_detail`` so they must land on
        # ``result.current.custom_fields`` automatically — guarding the
        # cross-tool inheritance the fix relies on.
        mock_client.patch.return_value = {}
        get_response = _make_get_response(title="after")
        data = cast(dict[str, object], get_response["data"])
        attributes = cast(dict[str, object], data["attributes"])
        attributes["riskLevel"] = "high"
        attributes["effortHours"] = 12.0
        mock_client.get.return_value = get_response

        result = await _call_update(mock_ctx, title="after")

        assert result.current is not None
        assert result.current.custom_fields == {
            "riskLevel": "high",
            "effortHours": 12.0,
        }

    async def test_custom_fields_inlined_into_patch_body(
        self, mock_ctx: MagicMock, mock_client: AsyncMock
    ) -> None:
        # The PATCH body must carry customs at the top of ``attributes``
        # (NOT nested under a ``customFields`` container — Polarion drops
        # that). Pin the wire shape here.
        mock_client.patch.return_value = {}
        rich = {"type": "text/html", "value": "<p>note</p>"}
        mock_client.get.return_value = _make_get_response(
            custom_fields={"riskLevel": "high", "reviewerNote": rich}
        )

        await _call_update(
            mock_ctx,
            custom_fields={"riskLevel": "low", "reviewerNote": rich},
        )

        _, kwargs = mock_client.patch.call_args
        body = kwargs["json"]
        item = body["data"]
        attributes = item["attributes"]
        assert attributes["riskLevel"] == "low"
        assert attributes["reviewerNote"] == rich
        assert "customFields" not in attributes

    async def test_changes_summary_records_custom_fields(
        self, mock_ctx: MagicMock, mock_client: AsyncMock
    ) -> None:
        # ``WorkItemUpdateResult.changes`` should reflect what was sent
        # so callers can confirm the intent client-side.
        mock_client.patch.return_value = {}
        mock_client.get.return_value = _make_get_response(
            custom_fields={"riskLevel": "low"}
        )

        result = await _call_update(
            mock_ctx,
            title="t",
            custom_fields={"riskLevel": "high"},
        )

        assert result.changes["title"] == "t"
        assert result.changes["custom_fields"] == {"riskLevel": "high"}

    async def test_changes_custom_fields_is_independent_of_input(
        self, mock_ctx: MagicMock, mock_client: AsyncMock
    ) -> None:
        # Mutating the caller's dict (or its nested rich-text dict) after the
        # call must not bleed into the returned ``changes`` snapshot.
        mock_client.patch.return_value = {}
        mock_client.get.return_value = _make_get_response(
            custom_fields={"reviewerNote": "x", "riskLevel": "low"}
        )

        rich = {"type": "text/html", "value": "<p>original</p>"}
        customs: dict[str, object] = {"reviewerNote": rich, "riskLevel": "high"}

        result = await _call_update(
            mock_ctx,
            title="t",
            custom_fields=customs,
        )

        customs["riskLevel"] = "low"
        rich["value"] = "<p>mutated</p>"

        recorded = cast(dict[str, object], result.changes["custom_fields"])
        assert recorded["riskLevel"] == "high"
        recorded_note = cast(dict[str, object], recorded["reviewerNote"])
        assert recorded_note["value"] == "<p>original</p>"

    async def test_dry_run_preview_includes_custom_fields(
        self, mock_ctx: MagicMock, mock_client: AsyncMock
    ) -> None:
        # Dry-run should echo the merged attributes (standard + custom)
        # so the LLM can verify the wire shape before committing. The
        # prefetch primes the custom-key cache so the guard accepts the key.
        mock_client.get.return_value = _make_get_response(
            custom_fields={"riskLevel": "high"}
        )
        result = await _call_update(
            mock_ctx,
            title="t",
            custom_fields={"riskLevel": "high"},
            dry_run=True,
        )

        mock_client.patch.assert_not_called()
        assert result.payload_preview is not None
        item = cast(dict[str, object], result.payload_preview["data"])
        attributes = cast(dict[str, object], item["attributes"])
        assert attributes["title"] == "t"
        assert attributes["riskLevel"] == "high"

    async def test_round_trip_read_response_can_be_written_back(
        self, mock_ctx: MagicMock, mock_client: AsyncMock
    ) -> None:
        # The dict shape that flows out of WorkItemDetail.custom_fields
        # on read must be acceptable as the custom_fields argument on
        # write — without copying or transformation. This is the
        # critical end-to-end ergonomic that justifies symmetric shapes
        # on both sides.
        read_customs: dict[str, object] = {
            "riskLevel": "high",
            "effortHours": 8.0,
            "reviewerNote": {"type": "text/html", "value": "<p>x</p>"},
        }
        mock_client.patch.return_value = {}
        mock_client.get.return_value = _make_get_response(custom_fields=read_customs)

        result = await _call_update(mock_ctx, custom_fields=read_customs)

        _, kwargs = mock_client.patch.call_args
        attributes = kwargs["json"]["data"]["attributes"]
        # Every key from the read response landed inline under attributes.
        for key, value in read_customs.items():
            assert attributes[key] == value
        # Tool layer didn't accept it then re-emit a different shape.
        assert result.updated is True

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

    async def test_description_html_is_sent_verbatim(
        self, mock_ctx: MagicMock, mock_client: AsyncMock
    ) -> None:
        """update_work_item passes description_html through unchanged.

        Core round-trip guarantee: Polarion-specific spans and data-*
        attributes must survive the PATCH unchanged so the round-trip
        through ``get_work_item`` is lossless. No sanitize, no markdownify.
        """
        mock_client.patch.return_value = {}
        mock_client.get.return_value = _make_get_response()

        raw = (
            '<p>See <span class="polarion-rte-link" '
            'data-item-id="MCPT-7" data-scope="MyProj">MCPT-7</span></p>'
        )
        await _call_update(mock_ctx, description_html=raw)

        body = mock_client.patch.call_args.kwargs["json"]
        desc = body["data"]["attributes"]["description"]
        assert desc == {"type": "text/html", "value": raw}

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

    def test_description_html_rejects_overlong_input(self) -> None:
        """``max_length=MAX_BODY_HTML_LEN`` defends against runaway HTML.

        At the JSON Schema layer, FastMCP rejects bodies above the cap
        before the tool ever sees them. Re-prove the constraint here so
        a future docstring rewrite cannot silently drop ``max_length``.
        """
        adapter = self._adapter_for("description_html")
        # Well-formed payload below the cap is accepted unchanged.
        assert adapter.validate_python("<p>ok</p>") == "<p>ok</p>"
        # 2 MiB + 1 char is rejected.
        with pytest.raises(ValidationError):
            adapter.validate_python("x" * (2_000_000 + 1))


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
        attributes = cast(dict[str, object], data["attributes"])
        assert attributes == {
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

    def test_homepagecontent_omitted_when_not_passed(self) -> None:
        # JSON:API omit-preserve: body stays untouched when the caller
        # does not pass ``home_page_content_html``.
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

    def test_home_page_content_html_wrapped_verbatim(self) -> None:
        # Raw HTML pass-through — no sanitization, no markdownify.
        raw = '<p>x <span class="polarion-rte-link" data-item-id="MCPT-1">y</span></p>'
        payload = _build_update_document_payload(
            project_id="MyProj",
            space_id="S",
            document_name="D",
            title=None,
            status=None,
            type=None,
            home_page_content_html=raw,
        )

        data = cast(dict[str, object], payload["data"])
        attributes = cast(dict[str, object], data["attributes"])
        assert attributes["homePageContent"] == {"type": "text/html", "value": raw}

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

    def test_custom_fields_inlined_in_document_patch(self) -> None:
        payload = _build_update_document_payload(
            project_id="MyProj",
            space_id="S",
            document_name="D",
            title=None,
            status=None,
            type=None,
            home_page_content_html=None,
            custom_fields={"complianceLevel": "L3", "reviewerName": "alice"},
        )
        data = cast(dict[str, object], payload["data"])
        attributes = cast(dict[str, object], data["attributes"])
        assert attributes == {"complianceLevel": "L3", "reviewerName": "alice"}

    def test_custom_fields_collision_raises_for_document_standard(self) -> None:
        # ``moduleFolder`` is in the document standard set — collision.
        with pytest.raises(ValueError, match="custom_fields keys collide"):
            _build_update_document_payload(
                project_id="MyProj",
                space_id="S",
                document_name="D",
                title=None,
                status=None,
                type=None,
                home_page_content_html=None,
                custom_fields={"moduleFolder": "Other"},
            )


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
                home_page_content_html=None,
                custom_fields=None,
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
                home_page_content_html=None,
                custom_fields=None,
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
            home_page_content_html=None,
            custom_fields=None,
            workflow_action="approve",
            dry_run=True,
        )
        assert result.dry_run is True

    async def test_custom_fields_alone_satisfies_at_least_one_check(
        self, mock_ctx: MagicMock, mock_client: AsyncMock
    ) -> None:
        # custom_fields counts as a body field on update_document too. The
        # priming GET lets the custom-field guard see ``documentVersion``.
        mock_client.get.return_value = {
            "data": {"attributes": {"title": "D", "documentVersion": "0.1"}}
        }
        result = await update_document(
            mock_ctx,
            project_id="MyProj",
            space_id="S",
            document_name="D",
            title=None,
            status=None,
            type=None,
            home_page_content_html=None,
            custom_fields={"documentVersion": "0.2"},
            workflow_action=None,
            dry_run=True,
        )
        assert result.dry_run is True

    async def test_workflow_action_with_custom_fields_passes(
        self, mock_ctx: MagicMock, mock_client: AsyncMock
    ) -> None:
        # workflow_action paired with custom_fields-only should also
        # satisfy the body-field check.
        mock_client.get.return_value = {
            "data": {"attributes": {"title": "D", "documentVersion": "0.1"}}
        }
        result = await update_document(
            mock_ctx,
            project_id="MyProj",
            space_id="S",
            document_name="D",
            title=None,
            status=None,
            type=None,
            home_page_content_html=None,
            custom_fields={"documentVersion": "0.2"},
            workflow_action="approve",
            dry_run=True,
        )
        assert result.dry_run is True

    async def test_workflow_action_with_home_page_content_html_passes(
        self, mock_ctx: MagicMock, mock_client: AsyncMock
    ) -> None:
        """workflow_action paired with home_page_content_html only is OK.

        Polarion rejects empty PATCH bodies, so workflow_action MUST come
        with at least one attribute. home_page_content_html is one such
        attribute — this guard prevents the body-field check from
        regressing to "title/status/type/custom_fields only".
        """
        result = await update_document(
            mock_ctx,
            project_id="MyProj",
            space_id="S",
            document_name="D",
            title=None,
            status=None,
            type=None,
            home_page_content_html='<p id="b1">new body</p>',
            custom_fields=None,
            workflow_action="approve",
            dry_run=True,
        )
        assert result.dry_run is True
        # Sanity: payload includes both the body and the workflow query param.
        assert result.payload_preview is not None
        item = cast(dict[str, object], result.payload_preview["data"])
        attributes = cast(dict[str, object], item["attributes"])
        assert attributes["homePageContent"] == {
            "type": "text/html",
            "value": '<p id="b1">new body</p>',
        }

    async def test_custom_fields_collision_raises(
        self, mock_ctx: MagicMock, mock_client: AsyncMock
    ) -> None:
        with pytest.raises(ValueError, match="custom_fields keys collide"):
            await update_document(
                mock_ctx,
                project_id="MyProj",
                space_id="S",
                document_name="D",
                title="t",
                status=None,
                type=None,
                home_page_content_html=None,
                custom_fields={"title": "y"},
                workflow_action=None,
                dry_run=True,
            )
        mock_client.patch.assert_not_called()

    async def test_custom_fields_homepagecontent_collision_raises(
        self, mock_ctx: MagicMock, mock_client: AsyncMock
    ) -> None:
        """`homePageContent` is a STANDARD_DOCUMENT_ATTRIBUTES key.

        Allowing it via ``custom_fields`` would let a caller bypass the
        explicit ``home_page_content_html`` parameter (and its empty-string
        guard). The merge helper raises on collision; pin that semantics.
        """
        with pytest.raises(ValueError, match="custom_fields keys collide"):
            await update_document(
                mock_ctx,
                project_id="MyProj",
                space_id="S",
                document_name="D",
                title="t",
                status=None,
                type=None,
                home_page_content_html=None,
                custom_fields={
                    "homePageContent": {
                        "type": "text/html",
                        "value": "<p>sneak</p>",
                    }
                },
                workflow_action=None,
                dry_run=True,
            )
        mock_client.patch.assert_not_called()


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
            home_page_content_html=None,
            custom_fields=None,
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
        attributes = cast(dict[str, object], data["attributes"])
        assert attributes == {"title": "New Title"}


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
            home_page_content_html=None,
            custom_fields=None,
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
            home_page_content_html=None,
            custom_fields=None,
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
            home_page_content_html=None,
            custom_fields=None,
            workflow_action="approve",
            dry_run=False,
        )

        args, _ = mock_client.patch.call_args
        path = args[0]
        assert path.startswith("/projects/MyProj/spaces/S/documents/D")
        assert "workflowAction=approve" in path

    async def test_home_page_content_html_is_sent_verbatim(
        self, mock_ctx: MagicMock, mock_client: AsyncMock
    ) -> None:
        """home_page_content_html passes through with no sanitization."""
        mock_client.patch.return_value = {}

        raw = (
            '<p id="b1">Body with <span class="polarion-rte-link" '
            'data-item-id="MCPT-1">link</span></p>'
        )
        await update_document(
            mock_ctx,
            project_id="MyProj",
            space_id="S",
            document_name="D",
            title=None,
            status=None,
            type=None,
            home_page_content_html=raw,
            custom_fields=None,
            workflow_action=None,
            dry_run=False,
        )

        _, kwargs = mock_client.patch.call_args
        attributes = kwargs["json"]["data"]["attributes"]
        assert attributes["homePageContent"] == {"type": "text/html", "value": raw}
        # Nothing else slipped in.
        assert set(attributes.keys()) == {"homePageContent"}

    async def test_home_page_content_html_omitted_when_not_passed(
        self, mock_ctx: MagicMock, mock_client: AsyncMock
    ) -> None:
        """Omit-preserve: no homePageContent in body when not passed."""
        mock_client.patch.return_value = {}

        await update_document(
            mock_ctx,
            project_id="MyProj",
            space_id="S",
            document_name="D",
            title="T",
            status="approved",
            type="generic",
            home_page_content_html=None,
            custom_fields=None,
            workflow_action=None,
            dry_run=False,
        )

        _, kwargs = mock_client.patch.call_args
        body_str = repr(kwargs["json"])
        assert "homePageContent" not in body_str

    async def test_home_page_content_html_empty_string_raises(
        self, mock_ctx: MagicMock, mock_client: AsyncMock
    ) -> None:
        """Empty string is rejected at the tool layer (body-wipe guard)."""
        with pytest.raises(ValueError, match="would wipe"):
            await update_document(
                mock_ctx,
                project_id="MyProj",
                space_id="S",
                document_name="D",
                title=None,
                status=None,
                type=None,
                home_page_content_html="",
                custom_fields=None,
                workflow_action=None,
                dry_run=False,
            )
        mock_client.patch.assert_not_called()

    @pytest.mark.parametrize("whitespace", ["   ", "\n", "\t", "\n\n  \t"])
    async def test_home_page_content_html_whitespace_raises(
        self,
        mock_ctx: MagicMock,
        mock_client: AsyncMock,
        whitespace: str,
    ) -> None:
        """Whitespace-only strings strip to '' on the server, so reject too."""
        with pytest.raises(ValueError, match="would wipe"):
            await update_document(
                mock_ctx,
                project_id="MyProj",
                space_id="S",
                document_name="D",
                title=None,
                status=None,
                type=None,
                home_page_content_html=whitespace,
                custom_fields=None,
                workflow_action=None,
                dry_run=False,
            )
        mock_client.patch.assert_not_called()

    async def test_home_page_content_html_alone_passes_has_attrs_guard(
        self, mock_ctx: MagicMock, mock_client: AsyncMock
    ) -> None:
        """home_page_content_html alone counts as a body field."""
        result = await update_document(
            mock_ctx,
            project_id="MyProj",
            space_id="S",
            document_name="D",
            title=None,
            status=None,
            type=None,
            home_page_content_html='<p id="b1">x</p>',
            custom_fields=None,
            workflow_action=None,
            dry_run=True,
        )
        assert result.dry_run is True

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
            home_page_content_html=None,
            custom_fields=None,
            workflow_action=None,
            dry_run=False,
        )

        _, kwargs = mock_client.patch.call_args
        attributes = kwargs["json"]["data"]["attributes"]
        assert attributes == {"title": ""}

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
            home_page_content_html=None,
            custom_fields=None,
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
            home_page_content_html=None,
            custom_fields=None,
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
                home_page_content_html=None,
                custom_fields=None,
                workflow_action=None,
                dry_run=False,
            )

    async def test_404_raises_value_error_with_doc_in_message(
        self, mock_ctx: MagicMock, mock_client: AsyncMock
    ) -> None:
        mock_client.patch.side_effect = PolarionNotFoundError(
            "not found", status_code=404
        )

        with pytest.raises(ValueError, match="ghost-document") as exc_info:
            await update_document(
                mock_ctx,
                project_id="MyProj",
                space_id="ghost-space",
                document_name="ghost-document",
                title="t",
                status=None,
                type=None,
                home_page_content_html=None,
                custom_fields=None,
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
                home_page_content_html=None,
                custom_fields=None,
                workflow_action=None,
                dry_run=False,
            )


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

    def test_home_page_content_html_rejects_overlong_input(self) -> None:
        """``max_length=MAX_BODY_HTML_LEN`` defends against runaway HTML."""
        adapter = self._adapter_for("home_page_content_html")
        assert adapter.validate_python("<p>ok</p>") == "<p>ok</p>"
        with pytest.raises(ValidationError):
            adapter.validate_python("x" * (2_000_000 + 1))


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
                "create_work_items",
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
            (
                "create_work_item_links",
                {
                    "readOnlyHint": False,
                    "destructiveHint": False,
                    "idempotentHint": False,
                    "openWorldHint": True,
                },
            ),
            (
                "delete_work_item_links",
                {
                    "readOnlyHint": False,
                    "destructiveHint": True,
                    "idempotentHint": True,
                    "openWorldHint": True,
                },
            ),
            (
                "create_document_comments",
                {
                    "readOnlyHint": False,
                    "destructiveHint": False,
                    "idempotentHint": False,
                    "openWorldHint": True,
                },
            ),
            (
                "update_document_comment",
                {
                    "readOnlyHint": False,
                    "destructiveHint": False,
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


class TestUpdateDocumentPitfallDocumentation:
    """Lock the two body-edit pitfalls into the public docstring so a
    future slim pass cannot silently delete them.

    The pitfalls were reproduced against the live testdrive server and are
    user-facing (MCP hosts on other platforms never load CLAUDE.md), so the
    warnings must stay inside ``update_document.__doc__``.
    """

    def test_docstring_warns_about_anchorless_paragraph_returning_500(self) -> None:
        """Anchorless <p> appended via update_document breaks read_document_parts."""
        document = update_document.__doc__ or ""
        assert "anchorless" in document, (
            "update_document docstring must mention the anchorless <p> pitfall"
        )
        assert "HTTP 500" in document, (
            "update_document docstring must surface that the next read_document_parts "
            "call returns HTTP 500 after an anchorless <p> append"
        )
        assert "move_work_item_to_document" in document, (
            "update_document docstring must point callers at the correct attach path"
        )

    def test_docstring_warns_about_macro_div_module_relationship_gap(self) -> None:
        """Macro <div> reference injected via update_document leaves module unset."""
        document = update_document.__doc__ or ""
        assert "polarion_wiki macro" in document, (
            "update_document docstring must mention the polarion_wiki macro pitfall"
        )
        assert "module" in document, (
            "update_document docstring must surface that the work item's module "
            "relationship stays unset after a macro <div> injection"
        )


class TestBuildCreateDocumentPayload:
    """Tests for the private ``_build_create_document_payload`` helper."""

    def test_minimal_payload_has_only_required_attrs(self) -> None:
        payload = _build_create_document_payload(
            module_name="MySpec",
            title="My Spec",
            type="req_specification",
            home_page_content_html="",
            status=None,
        )

        assert payload == {
            "data": [
                {
                    "type": "documents",
                    "attributes": {
                        "moduleName": "MySpec",
                        "title": "My Spec",
                        "type": "req_specification",
                    },
                }
            ]
        }
        item = cast(list[dict[str, object]], payload["data"])[0]
        attributes = cast(dict[str, object], item["attributes"])
        assert "status" not in attributes
        assert "homePageContent" not in attributes

    def test_status_attached_when_set(self) -> None:
        payload = _build_create_document_payload(
            module_name="MySpec",
            title="t",
            type="generic",
            home_page_content_html="",
            status="draft",
        )

        item = cast(list[dict[str, object]], payload["data"])[0]
        attributes = cast(dict[str, object], item["attributes"])
        assert attributes["status"] == "draft"

    def test_home_page_content_wrapped_as_html_block(self) -> None:
        payload = _build_create_document_payload(
            module_name="MySpec",
            title="t",
            type="generic",
            home_page_content_html="<p>Hi</p>",
            status=None,
        )

        item = cast(list[dict[str, object]], payload["data"])[0]
        attributes = cast(dict[str, object], item["attributes"])
        assert attributes["homePageContent"] == {
            "type": "text/html",
            "value": "<p>Hi</p>",
        }

    def test_skips_none_status_and_empty_body(self) -> None:
        payload = _build_create_document_payload(
            module_name="MySpec",
            title="t",
            type="generic",
            home_page_content_html="",
            status=None,
        )

        item = cast(list[dict[str, object]], payload["data"])[0]
        attributes = cast(dict[str, object], item["attributes"])
        assert set(attributes.keys()) == {"moduleName", "title", "type"}

    def test_custom_fields_inlined_alongside_standard_attrs(self) -> None:
        payload = _build_create_document_payload(
            module_name="MySpec",
            title="t",
            type="generic",
            home_page_content_html="",
            status=None,
            custom_fields={"projectOwner": "alice", "phase": "design"},
        )

        item = cast(list[dict[str, object]], payload["data"])[0]
        attributes = cast(dict[str, object], item["attributes"])
        assert attributes["projectOwner"] == "alice"
        assert attributes["phase"] == "design"

    def test_custom_fields_collision_with_standard_attr_raises(self) -> None:
        with pytest.raises(ValueError, match="title"):
            _build_create_document_payload(
                module_name="MySpec",
                title="t",
                type="generic",
                home_page_content_html="",
                status=None,
                custom_fields={"title": "duplicate"},
            )


class TestCreateDocumentDryRun:
    """Tests for ``create_document`` with ``dry_run=True``."""

    async def test_dry_run_returns_payload_without_calling_post(
        self, mock_ctx: MagicMock, mock_client: AsyncMock
    ) -> None:
        result = await create_document(
            mock_ctx,
            project_id="MyProj",
            space_id="_default",
            module_name="MySpec",
            title="Dry test",
            type="req_specification",
            status=None,
            home_page_content=None,
            custom_fields=None,
            dry_run=True,
        )

        mock_client.post.assert_not_called()
        assert isinstance(result, DocumentCreateResult)
        assert result.dry_run is True
        assert result.created is False
        assert result.document_name is None
        assert result.payload_preview is not None
        assert isinstance(result.payload_preview, dict)
        item = cast(list[dict[str, object]], result.payload_preview["data"])[0]
        attributes = cast(dict[str, object], item["attributes"])
        assert attributes == {
            "moduleName": "MySpec",
            "title": "Dry test",
            "type": "req_specification",
        }


class TestCreateDocumentHappyPath:
    """Tests for a successful ``create_document`` call."""

    async def test_returns_document_name_on_201(
        self, mock_ctx: MagicMock, mock_client: AsyncMock
    ) -> None:
        mock_client.post.return_value = {
            "data": [
                {
                    "type": "documents",
                    "id": "MyProj/_default/MySpec",
                    "links": {"self": "..."},
                }
            ]
        }

        result = await create_document(
            mock_ctx,
            project_id="MyProj",
            space_id="_default",
            module_name="MySpec",
            title="Real",
            type="req_specification",
            status=None,
            home_page_content=None,
            custom_fields=None,
            dry_run=False,
        )

        assert isinstance(result, DocumentCreateResult)
        assert result.created is True
        assert result.dry_run is False
        assert result.document_name == "MySpec"
        assert result.payload_preview is None

    async def test_post_called_with_correct_path_and_body(
        self, mock_ctx: MagicMock, mock_client: AsyncMock
    ) -> None:
        mock_client.post.return_value = {
            "data": [{"type": "documents", "id": "MyProj/_default/MySpec"}]
        }

        await create_document(
            mock_ctx,
            project_id="MyProj",
            space_id="_default",
            module_name="MySpec",
            title="t",
            type="req_specification",
            status="draft",
            home_page_content=None,
            custom_fields=None,
            dry_run=False,
        )

        args, kwargs = mock_client.post.call_args
        assert args == ("/projects/MyProj/spaces/_default/documents",)
        body = kwargs["json"]
        item = body["data"][0]
        assert item["type"] == "documents"
        assert item["attributes"]["moduleName"] == "MySpec"
        assert item["attributes"]["title"] == "t"
        assert item["attributes"]["type"] == "req_specification"
        assert item["attributes"]["status"] == "draft"

    async def test_path_url_encodes_special_chars(
        self, mock_ctx: MagicMock, mock_client: AsyncMock
    ) -> None:
        mock_client.post.return_value = {
            "data": [{"type": "documents", "id": "Proj With Space/My Space/MySpec"}]
        }

        await create_document(
            mock_ctx,
            project_id="Proj With Space",
            space_id="My Space",
            module_name="MySpec",
            title="t",
            type="generic",
            status=None,
            home_page_content=None,
            custom_fields=None,
            dry_run=False,
        )

        args, _ = mock_client.post.call_args
        assert args == ("/projects/Proj%20With%20Space/spaces/My%20Space/documents",)

    async def test_home_page_content_markdown_converted_and_sanitized(
        self, mock_ctx: MagicMock, mock_client: AsyncMock
    ) -> None:
        mock_client.post.return_value = {
            "data": [{"type": "documents", "id": "MyProj/_default/MySpec"}]
        }

        await create_document(
            mock_ctx,
            project_id="MyProj",
            space_id="_default",
            module_name="MySpec",
            title="t",
            type="generic",
            status=None,
            home_page_content="**bold** [link](https://example.com)",
            custom_fields=None,
            dry_run=False,
        )

        _, kwargs = mock_client.post.call_args
        body = kwargs["json"]["data"][0]["attributes"]["homePageContent"]
        assert body["type"] == "text/html"
        assert "<strong>bold</strong>" in body["value"]
        assert 'href="https://example.com"' in body["value"]

    async def test_home_page_content_stamps_unique_block_ids(
        self, mock_ctx: MagicMock, mock_client: AsyncMock
    ) -> None:
        """Every block-level element from the target set gets a unique
        ``polarion_mcp_N`` id; headings are intentionally left bare so
        Polarion can rewrite them to the macro form on save."""
        mock_client.post.return_value = {
            "data": [{"type": "documents", "id": "MyProj/_default/MySpec"}]
        }

        await create_document(
            mock_ctx,
            project_id="MyProj",
            space_id="_default",
            module_name="MySpec",
            title="t",
            type="generic",
            status=None,
            home_page_content="# H\n\npara1\n\n* item\n\npara2",
            custom_fields=None,
            dry_run=False,
        )

        _, kwargs = mock_client.post.call_args
        body_html = kwargs["json"]["data"][0]["attributes"]["homePageContent"]["value"]
        assert '<p id="polarion_mcp_0">' in body_html
        assert '<ul id="polarion_mcp_1">' in body_html
        assert '<p id="polarion_mcp_2">' in body_html
        assert "<h1>" in body_html
        assert "<h1 id=" not in body_html

    async def test_home_page_content_strips_dangerous_link_schemes(
        self, mock_ctx: MagicMock, mock_client: AsyncMock
    ) -> None:
        mock_client.post.return_value = {
            "data": [{"type": "documents", "id": "MyProj/_default/MySpec"}]
        }

        await create_document(
            mock_ctx,
            project_id="MyProj",
            space_id="_default",
            module_name="MySpec",
            title="t",
            type="generic",
            status=None,
            home_page_content="[click](javascript:alert(1))",
            custom_fields=None,
            dry_run=False,
        )

        _, kwargs = mock_client.post.call_args
        body_html = kwargs["json"]["data"][0]["attributes"]["homePageContent"]["value"]
        assert 'href="javascript:' not in body_html
        assert "href='javascript:" not in body_html

    async def test_document_name_with_slashes_extracted_correctly(
        self, mock_ctx: MagicMock, mock_client: AsyncMock
    ) -> None:
        """``split_module_id`` preserves slashes in the document_name segment."""
        mock_client.post.return_value = {
            "data": [{"type": "documents", "id": "MyProj/_default/Folder/Sub/Doc"}]
        }

        result = await create_document(
            mock_ctx,
            project_id="MyProj",
            space_id="_default",
            module_name="Folder/Sub/Doc",
            title="t",
            type="generic",
            status=None,
            home_page_content=None,
            custom_fields=None,
            dry_run=False,
        )

        assert result.document_name == "Folder/Sub/Doc"

    async def test_invalidates_documents_cache_on_success(
        self, mock_ctx: MagicMock, mock_client: AsyncMock
    ) -> None:
        """``create_document`` drops the project's docs cache entry on 201."""
        _cache_mod.store_cached_documents("MyProj", [("_default", "OldDoc")])
        mock_client.post.return_value = {
            "data": [{"type": "documents", "id": "MyProj/_default/MySpec"}]
        }

        await create_document(
            mock_ctx,
            project_id="MyProj",
            space_id="_default",
            module_name="MySpec",
            title="t",
            type="generic",
            status=None,
            home_page_content=None,
            custom_fields=None,
            dry_run=False,
        )

        assert _cache_mod.get_cached_documents("MyProj") is None

    async def test_does_not_invalidate_cache_on_failure(
        self, mock_ctx: MagicMock, mock_client: AsyncMock
    ) -> None:
        """Cache is preserved when the POST raises — no half-state change."""
        _cache_mod.store_cached_documents("MyProj", [("_default", "OldDoc")])
        mock_client.post.side_effect = PolarionError("boom", status_code=500)

        with pytest.raises(RuntimeError):
            await create_document(
                mock_ctx,
                project_id="MyProj",
                space_id="_default",
                module_name="MySpec",
                title="t",
                type="generic",
                status=None,
                home_page_content=None,
                custom_fields=None,
                dry_run=False,
            )

        cached = _cache_mod.get_cached_documents("MyProj")
        assert cached == [("_default", "OldDoc")]
        _cache_mod.invalidate_documents_cache("MyProj")


class TestCreateDocumentErrorMapping:
    """Tests that domain exceptions are mapped at the tool layer."""

    async def test_401_raises_permission_error(
        self, mock_ctx: MagicMock, mock_client: AsyncMock
    ) -> None:
        mock_client.post.side_effect = PolarionAuthError("auth", status_code=401)

        with pytest.raises(PermissionError):
            await create_document(
                mock_ctx,
                project_id="MyProj",
                space_id="_default",
                module_name="MySpec",
                title="t",
                type="generic",
                status=None,
                home_page_content=None,
                custom_fields=None,
                dry_run=False,
            )

    async def test_404_raises_value_error_mentioning_project_and_space(
        self, mock_ctx: MagicMock, mock_client: AsyncMock
    ) -> None:
        mock_client.post.side_effect = PolarionNotFoundError(
            "not found", status_code=404
        )

        with pytest.raises(ValueError, match="space") as exc_info:
            await create_document(
                mock_ctx,
                project_id="ghost",
                space_id="ghost_space",
                module_name="MySpec",
                title="t",
                type="generic",
                status=None,
                home_page_content=None,
                custom_fields=None,
                dry_run=False,
            )
        assert "ghost" in str(exc_info.value)
        assert "ghost_space" in str(exc_info.value)

    async def test_other_error_raises_runtime_error(
        self, mock_ctx: MagicMock, mock_client: AsyncMock
    ) -> None:
        mock_client.post.side_effect = PolarionError("conflict", status_code=409)

        with pytest.raises(RuntimeError, match="conflict"):
            await create_document(
                mock_ctx,
                project_id="MyProj",
                space_id="_default",
                module_name="MySpec",
                title="t",
                type="generic",
                status=None,
                home_page_content=None,
                custom_fields=None,
                dry_run=False,
            )


class TestCreateDocumentResponseParsing:
    """Tests for unexpected 2xx response shapes from Polarion."""

    async def test_empty_data_array_raises_runtime_error(
        self, mock_ctx: MagicMock, mock_client: AsyncMock
    ) -> None:
        mock_client.post.return_value = {"data": []}

        with pytest.raises(RuntimeError, match="no document name"):
            await create_document(
                mock_ctx,
                project_id="MyProj",
                space_id="_default",
                module_name="MySpec",
                title="t",
                type="generic",
                status=None,
                home_page_content=None,
                custom_fields=None,
                dry_run=False,
            )

    async def test_data_not_a_list_raises_runtime_error(
        self, mock_ctx: MagicMock, mock_client: AsyncMock
    ) -> None:
        mock_client.post.return_value = {"data": {"id": "MyProj/_default/MySpec"}}

        with pytest.raises(RuntimeError, match="no document name"):
            await create_document(
                mock_ctx,
                project_id="MyProj",
                space_id="_default",
                module_name="MySpec",
                title="t",
                type="generic",
                status=None,
                home_page_content=None,
                custom_fields=None,
                dry_run=False,
            )

    async def test_two_segment_id_raises_runtime_error(
        self, mock_ctx: MagicMock, mock_client: AsyncMock
    ) -> None:
        """``split_module_id`` returns ``('','')`` for under-3-segment IDs."""
        mock_client.post.return_value = {
            "data": [{"type": "documents", "id": "MyProj/MySpec"}]
        }

        with pytest.raises(RuntimeError, match="no document name"):
            await create_document(
                mock_ctx,
                project_id="MyProj",
                space_id="_default",
                module_name="MySpec",
                title="t",
                type="generic",
                status=None,
                home_page_content=None,
                custom_fields=None,
                dry_run=False,
            )


class TestCreateDocumentFieldValidation:
    """Verify ``min_length`` / ``max_length`` constraints on parameters."""

    @staticmethod
    def _adapter_for(param_name: str) -> TypeAdapter[object]:
        hints = get_type_hints(create_document)
        sig = inspect.signature(create_document)
        field_info = sig.parameters[param_name].default
        return TypeAdapter(Annotated[hints[param_name], field_info])

    def test_project_id_rejects_empty_string(self) -> None:
        with pytest.raises(ValidationError):
            self._adapter_for("project_id").validate_python("")

    def test_space_id_rejects_empty_string(self) -> None:
        with pytest.raises(ValidationError):
            self._adapter_for("space_id").validate_python("")

    def test_module_name_rejects_empty_string(self) -> None:
        with pytest.raises(ValidationError):
            self._adapter_for("module_name").validate_python("")

    def test_module_name_accepts_non_empty(self) -> None:
        assert self._adapter_for("module_name").validate_python("MySpec") == "MySpec"

    def test_title_rejects_empty_string(self) -> None:
        with pytest.raises(ValidationError):
            self._adapter_for("title").validate_python("")

    def test_type_rejects_empty_string(self) -> None:
        with pytest.raises(ValidationError):
            self._adapter_for("type").validate_python("")

    def test_home_page_content_rejects_overlong_input(self) -> None:
        adapter = self._adapter_for("home_page_content")
        assert adapter.validate_python("hello") == "hello"
        with pytest.raises(ValidationError):
            adapter.validate_python("x" * (2_000_000 + 1))


class TestCreateDocumentRegistration:
    """The tool must be registered on the FastMCP server instance."""

    async def test_create_document_tool_registered(self) -> None:
        tools = await mcp.list_tools()
        assert any(tool.name == "create_document" for tool in tools)


class TestCreateDocumentDocstringGuidance:
    """Verify enum-resolution and ghost-write guidance lives in the docstring.

    Per CLAUDE.md, the write tools' docstrings are the only enforcement
    against ghost-enum writes — the server does not validate enum IDs.
    """

    def test_docstring_mentions_list_document_enum_options(self) -> None:
        document = create_document.__doc__ or ""
        assert "list_document_enum_options" in document

    def test_docstring_mentions_ghost_writes(self) -> None:
        document = create_document.__doc__ or ""
        assert "ghost" in document.lower()

    def test_docstring_mentions_module_name_uniqueness(self) -> None:
        document = create_document.__doc__ or ""
        assert "unique" in document.lower()
        assert "409" in document or "conflict" in document.lower()


class TestBuildCreateLinksPayload:
    """Tests for the private ``_build_create_links_payload`` helper."""

    def test_single_spec_minimal_skips_revision(self) -> None:
        payload = _build_create_links_payload(
            source_project_id="MyProj",
            links=[
                WorkItemLinkSpec(role="parent", target_work_item_id="MCPT-2"),
            ],
        )

        data = cast(list[dict[str, object]], payload["data"])
        assert len(data) == 1
        item = data[0]
        assert item["type"] == "linkedworkitems"
        attrs = cast(dict[str, object], item["attributes"])
        assert attrs == {"role": "parent", "suspect": False}
        rels = cast(dict[str, object], item["relationships"])
        wi_rel = cast(dict[str, object], rels["workItem"])
        assert wi_rel["data"] == {"type": "workitems", "id": "MyProj/MCPT-2"}

    def test_revision_inlined_when_set(self) -> None:
        payload = _build_create_links_payload(
            source_project_id="MyProj",
            links=[
                WorkItemLinkSpec(
                    role="verifies",
                    target_work_item_id="MCPT-2",
                    suspect=True,
                    revision="r1234",
                ),
            ],
        )

        attrs = cast(
            dict[str, object],
            cast(list[dict[str, object]], payload["data"])[0]["attributes"],
        )
        assert attrs == {"role": "verifies", "suspect": True, "revision": "r1234"}

    def test_cross_project_target_in_relationship_id(self) -> None:
        payload = _build_create_links_payload(
            source_project_id="MyProj",
            links=[
                WorkItemLinkSpec(
                    role="relates_to",
                    target_work_item_id="MCPT-99",
                    target_project_id="OtherProj",
                ),
            ],
        )

        rels = cast(
            dict[str, object],
            cast(list[dict[str, object]], payload["data"])[0]["relationships"],
        )
        wi_rel = cast(dict[str, object], rels["workItem"])
        assert wi_rel["data"] == {"type": "workitems", "id": "OtherProj/MCPT-99"}

    def test_multiple_specs_preserve_order(self) -> None:
        payload = _build_create_links_payload(
            source_project_id="MyProj",
            links=[
                WorkItemLinkSpec(role="parent", target_work_item_id="MCPT-2"),
                WorkItemLinkSpec(role="verifies", target_work_item_id="MCPT-3"),
                WorkItemLinkSpec(
                    role="relates_to",
                    target_work_item_id="MCPT-9",
                    target_project_id="OtherProj",
                ),
            ],
        )

        data = cast(list[dict[str, object]], payload["data"])
        assert len(data) == 3
        roles = [cast(dict[str, object], item["attributes"])["role"] for item in data]
        assert roles == ["parent", "verifies", "relates_to"]
        target_ids = [
            cast(
                dict[str, object],
                cast(dict[str, object], item["relationships"])["workItem"],
            )["data"]
            for item in data
        ]
        assert target_ids == [
            {"type": "workitems", "id": "MyProj/MCPT-2"},
            {"type": "workitems", "id": "MyProj/MCPT-3"},
            {"type": "workitems", "id": "OtherProj/MCPT-9"},
        ]


class TestExtractCreatedLinkIds:
    """Tests for the private ``_extract_created_link_ids`` helper."""

    def test_extracts_in_order(self) -> None:
        response: dict[str, object] = {
            "data": [
                {"type": "linkedworkitems", "id": "P/WI-1/parent/P/WI-2"},
                {"type": "linkedworkitems", "id": "P/WI-1/verifies/P/WI-3"},
            ]
        }
        assert _extract_created_link_ids(response) == [
            "P/WI-1/parent/P/WI-2",
            "P/WI-1/verifies/P/WI-3",
        ]

    def test_skips_entries_missing_id(self) -> None:
        response: dict[str, object] = {
            "data": [
                {"type": "linkedworkitems", "id": "P/WI-1/parent/P/WI-2"},
                {"type": "linkedworkitems"},
            ]
        }
        assert _extract_created_link_ids(response) == ["P/WI-1/parent/P/WI-2"]

    def test_returns_empty_on_missing_data(self) -> None:
        assert _extract_created_link_ids({}) == []

    def test_returns_empty_on_non_list_data(self) -> None:
        assert _extract_created_link_ids({"data": "oops"}) == []


def _echo_targets_exist(
    path: str, *, params: dict[str, object] | None = None, **_: object
) -> dict[str, object]:
    """Guard GET stub: report every id in the ``id:(...)`` query as existing."""
    project = path.split("/")[2]
    query = str((params or {}).get("query", ""))
    ids = query.removeprefix("id:(").removesuffix(")").split()
    return {"data": [{"type": "workitems", "id": f"{project}/{i}"} for i in ids]}


class TestCreateWorkItemLinksDryRun:
    """Tests for ``create_work_item_links`` with ``dry_run=True``."""

    @pytest.fixture(autouse=True)
    def _stub_target_existence(self, mock_client: AsyncMock) -> None:
        mock_client.get.side_effect = _echo_targets_exist

    async def test_dry_run_returns_payload_without_calling_post(
        self, mock_ctx: MagicMock, mock_client: AsyncMock
    ) -> None:
        result = await create_work_item_links(
            mock_ctx,
            project_id="MyProj",
            work_item_id="MCPT-1",
            links=[WorkItemLinkSpec(role="parent", target_work_item_id="MCPT-2")],
            dry_run=True,
        )

        mock_client.post.assert_not_called()
        assert isinstance(result, WorkItemLinksCreateResult)
        assert result.dry_run is True
        assert result.created is False
        assert result.link_ids == []
        assert result.payload_preview is not None
        item = cast(list[dict[str, object]], result.payload_preview["data"])[0]
        attrs = cast(dict[str, object], item["attributes"])
        assert attrs == {"role": "parent", "suspect": False}
        rels = cast(dict[str, object], item["relationships"])
        wi_rel = cast(dict[str, object], rels["workItem"])
        # target_project_id defaults to source project_id when None.
        assert wi_rel["data"] == {"type": "workitems", "id": "MyProj/MCPT-2"}


class TestCreateWorkItemLinksTargetGuard:
    """The target-existence guard runs before the write, on dry-run too."""

    async def test_dry_run_missing_target_raises_without_post(
        self, mock_ctx: MagicMock, mock_client: AsyncMock
    ) -> None:
        mock_client.get.return_value = {"data": []}

        with pytest.raises(ValueError, match="MyProj/MCPT-2"):
            await create_work_item_links(
                mock_ctx,
                project_id="MyProj",
                work_item_id="MCPT-1",
                links=[WorkItemLinkSpec(role="parent", target_work_item_id="MCPT-2")],
                dry_run=True,
            )

        mock_client.post.assert_not_called()

    async def test_real_missing_target_raises_without_post(
        self, mock_ctx: MagicMock, mock_client: AsyncMock
    ) -> None:
        mock_client.get.return_value = {"data": []}

        with pytest.raises(ValueError, match="dangling"):
            await create_work_item_links(
                mock_ctx,
                project_id="MyProj",
                work_item_id="MCPT-1",
                links=[WorkItemLinkSpec(role="parent", target_work_item_id="MCPT-2")],
                dry_run=False,
            )

        mock_client.post.assert_not_called()


class TestCreateWorkItemLinksHappyPath:
    """Tests for a successful ``create_work_item_links`` call."""

    @pytest.fixture(autouse=True)
    def _stub_target_existence(self, mock_client: AsyncMock) -> None:
        mock_client.get.side_effect = _echo_targets_exist

    async def test_returns_composite_link_ids_on_201(
        self, mock_ctx: MagicMock, mock_client: AsyncMock
    ) -> None:
        mock_client.post.return_value = {
            "data": [
                {
                    "type": "linkedworkitems",
                    "id": "MyProj/MCPT-1/parent/MyProj/MCPT-2",
                    "links": {"self": "..."},
                },
                {
                    "type": "linkedworkitems",
                    "id": "MyProj/MCPT-1/verifies/MyProj/MCPT-3",
                },
            ]
        }

        result = await create_work_item_links(
            mock_ctx,
            project_id="MyProj",
            work_item_id="MCPT-1",
            links=[
                WorkItemLinkSpec(role="parent", target_work_item_id="MCPT-2"),
                WorkItemLinkSpec(role="verifies", target_work_item_id="MCPT-3"),
            ],
            dry_run=False,
        )

        assert isinstance(result, WorkItemLinksCreateResult)
        assert result.created is True
        assert result.dry_run is False
        assert result.link_ids == [
            "MyProj/MCPT-1/parent/MyProj/MCPT-2",
            "MyProj/MCPT-1/verifies/MyProj/MCPT-3",
        ]
        assert result.payload_preview is None

    async def test_post_called_with_correct_path_and_body(
        self, mock_ctx: MagicMock, mock_client: AsyncMock
    ) -> None:
        mock_client.post.return_value = {
            "data": [
                {
                    "type": "linkedworkitems",
                    "id": "MyProj/MCPT-1/relates_to/MyProj/MCPT-2",
                }
            ]
        }

        await create_work_item_links(
            mock_ctx,
            project_id="MyProj",
            work_item_id="MCPT-1",
            links=[
                WorkItemLinkSpec(role="relates_to", target_work_item_id="MCPT-2"),
            ],
            dry_run=False,
        )

        args, kwargs = mock_client.post.call_args
        assert args == ("/projects/MyProj/workitems/MCPT-1/linkedworkitems",)
        body = kwargs["json"]
        item = body["data"][0]
        assert item["type"] == "linkedworkitems"
        assert item["attributes"]["role"] == "relates_to"
        assert item["relationships"]["workItem"]["data"]["id"] == "MyProj/MCPT-2"

    async def test_cross_project_target_uses_explicit_target_project(
        self, mock_ctx: MagicMock, mock_client: AsyncMock
    ) -> None:
        mock_client.post.return_value = {
            "data": [
                {
                    "type": "linkedworkitems",
                    "id": "MyProj/MCPT-1/verifies/OtherProj/MCPT-9",
                }
            ]
        }

        result = await create_work_item_links(
            mock_ctx,
            project_id="MyProj",
            work_item_id="MCPT-1",
            links=[
                WorkItemLinkSpec(
                    role="verifies",
                    target_work_item_id="MCPT-9",
                    target_project_id="OtherProj",
                ),
            ],
            dry_run=False,
        )

        assert result.link_ids == ["MyProj/MCPT-1/verifies/OtherProj/MCPT-9"]
        _, kwargs = mock_client.post.call_args
        assert (
            kwargs["json"]["data"][0]["relationships"]["workItem"]["data"]["id"]
            == "OtherProj/MCPT-9"
        )


class TestCreateWorkItemLinksErrorMapping:
    """Tests that domain exceptions are mapped at the tool layer."""

    @pytest.fixture(autouse=True)
    def _stub_target_existence(self, mock_client: AsyncMock) -> None:
        mock_client.get.side_effect = _echo_targets_exist

    async def test_401_raises_permission_error(
        self, mock_ctx: MagicMock, mock_client: AsyncMock
    ) -> None:
        mock_client.post.side_effect = PolarionAuthError("auth", status_code=401)

        with pytest.raises(PermissionError):
            await create_work_item_links(
                mock_ctx,
                project_id="MyProj",
                work_item_id="MCPT-1",
                links=[
                    WorkItemLinkSpec(role="parent", target_work_item_id="MCPT-2"),
                ],
                dry_run=False,
            )

    async def test_404_raises_value_error(
        self, mock_ctx: MagicMock, mock_client: AsyncMock
    ) -> None:
        mock_client.post.side_effect = PolarionNotFoundError(
            "not found", status_code=404
        )

        with pytest.raises(ValueError, match="list_work_items"):
            await create_work_item_links(
                mock_ctx,
                project_id="MyProj",
                work_item_id="MCPT-1",
                links=[
                    WorkItemLinkSpec(role="parent", target_work_item_id="MCPT-2"),
                ],
                dry_run=False,
            )

    async def test_generic_polarion_error_raises_runtime_error(
        self, mock_ctx: MagicMock, mock_client: AsyncMock
    ) -> None:
        mock_client.post.side_effect = PolarionError("duplicate link", status_code=409)

        with pytest.raises(RuntimeError, match="create work item links"):
            await create_work_item_links(
                mock_ctx,
                project_id="MyProj",
                work_item_id="MCPT-1",
                links=[
                    WorkItemLinkSpec(role="parent", target_work_item_id="MCPT-2"),
                ],
                dry_run=False,
            )


class TestCreateWorkItemLinksResponseParsing:
    """Tests for unexpected 2xx response shapes from Polarion."""

    @pytest.fixture(autouse=True)
    def _stub_target_existence(self, mock_client: AsyncMock) -> None:
        mock_client.get.side_effect = _echo_targets_exist

    async def test_empty_data_array_raises_runtime_error(
        self, mock_ctx: MagicMock, mock_client: AsyncMock
    ) -> None:
        mock_client.post.return_value = {"data": []}

        with pytest.raises(RuntimeError, match="0 ids for 1 requested links"):
            await create_work_item_links(
                mock_ctx,
                project_id="MyProj",
                work_item_id="MCPT-1",
                links=[
                    WorkItemLinkSpec(role="parent", target_work_item_id="MCPT-2"),
                ],
                dry_run=False,
            )

    async def test_missing_id_field_raises_runtime_error(
        self, mock_ctx: MagicMock, mock_client: AsyncMock
    ) -> None:
        mock_client.post.return_value = {"data": [{"type": "linkedworkitems"}]}

        with pytest.raises(RuntimeError, match="0 ids for 1 requested links"):
            await create_work_item_links(
                mock_ctx,
                project_id="MyProj",
                work_item_id="MCPT-1",
                links=[
                    WorkItemLinkSpec(role="parent", target_work_item_id="MCPT-2"),
                ],
                dry_run=False,
            )

    async def test_id_count_mismatch_raises_runtime_error(
        self, mock_ctx: MagicMock, mock_client: AsyncMock
    ) -> None:
        """Fewer returned ids than submitted links flags a partial create."""
        mock_client.post.return_value = {
            "data": [
                {
                    "type": "linkedworkitems",
                    "id": "MyProj/MCPT-1/parent/MyProj/MCPT-2",
                }
            ]
        }

        with pytest.raises(RuntimeError, match="1 ids for 2 requested links"):
            await create_work_item_links(
                mock_ctx,
                project_id="MyProj",
                work_item_id="MCPT-1",
                links=[
                    WorkItemLinkSpec(role="parent", target_work_item_id="MCPT-2"),
                    WorkItemLinkSpec(role="parent", target_work_item_id="MCPT-3"),
                ],
                dry_run=False,
            )


class TestCreateWorkItemLinksFieldValidation:
    """Verify ``min_length=1`` constraints on the required parameters."""

    @staticmethod
    def _adapter_for(param_name: str) -> TypeAdapter[object]:
        hints = get_type_hints(create_work_item_links)
        sig = inspect.signature(create_work_item_links)
        field_info = sig.parameters[param_name].default
        return TypeAdapter(Annotated[hints[param_name], field_info])

    def test_project_id_rejects_empty(self) -> None:
        with pytest.raises(ValidationError):
            self._adapter_for("project_id").validate_python("")

    def test_work_item_id_rejects_empty(self) -> None:
        with pytest.raises(ValidationError):
            self._adapter_for("work_item_id").validate_python("")

    def test_links_rejects_empty_list(self) -> None:
        with pytest.raises(ValidationError):
            self._adapter_for("links").validate_python([])

    def test_over_cap_links_rejected(self) -> None:
        too_many = [
            {"role": "parent", "target_work_item_id": "MCPT-2"} for _ in range(51)
        ]
        with pytest.raises(ValidationError):
            self._adapter_for("links").validate_python(too_many)

    def test_cap_boundary_accepted(self) -> None:
        exactly_50 = [
            {"role": "parent", "target_work_item_id": "MCPT-2"} for _ in range(50)
        ]
        result = cast(
            list[object], self._adapter_for("links").validate_python(exactly_50)
        )
        assert len(result) == 50

    def test_spec_role_rejects_empty(self) -> None:
        with pytest.raises(ValidationError):
            WorkItemLinkSpec(role="", target_work_item_id="MCPT-2")

    def test_spec_target_work_item_id_rejects_empty(self) -> None:
        with pytest.raises(ValidationError):
            WorkItemLinkSpec(role="parent", target_work_item_id="")

    def test_required_string_fields_accept_non_empty(self) -> None:
        for name in ("project_id", "work_item_id"):
            assert self._adapter_for(name).validate_python("x") == "x"


class TestBuildDeleteLinksPayload:
    """Tests for the private ``_build_delete_links_payload`` helper."""

    def test_single_ref_composite_id_same_project(self) -> None:
        link_ids, payload = _build_delete_links_payload(
            source_project_id="MyProj",
            source_work_item_id="MCPT-1",
            links=[WorkItemLinkRef(role="parent", target_work_item_id="MCPT-2")],
        )

        assert link_ids == ["MyProj/MCPT-1/parent/MyProj/MCPT-2"]
        data = cast(list[dict[str, object]], payload["data"])
        assert data == [
            {
                "type": "linkedworkitems",
                "id": "MyProj/MCPT-1/parent/MyProj/MCPT-2",
            }
        ]

    def test_cross_project_composite_id(self) -> None:
        link_ids, _payload = _build_delete_links_payload(
            source_project_id="MyProj",
            source_work_item_id="MCPT-1",
            links=[
                WorkItemLinkRef(
                    role="verifies",
                    target_work_item_id="MCPT-9",
                    target_project_id="OtherProj",
                ),
            ],
        )

        assert link_ids == ["MyProj/MCPT-1/verifies/OtherProj/MCPT-9"]

    def test_multiple_refs_preserve_order(self) -> None:
        link_ids, payload = _build_delete_links_payload(
            source_project_id="MyProj",
            source_work_item_id="MCPT-1",
            links=[
                WorkItemLinkRef(role="parent", target_work_item_id="MCPT-2"),
                WorkItemLinkRef(role="verifies", target_work_item_id="MCPT-3"),
            ],
        )

        assert link_ids == [
            "MyProj/MCPT-1/parent/MyProj/MCPT-2",
            "MyProj/MCPT-1/verifies/MyProj/MCPT-3",
        ]
        ids_in_body = [
            cast(dict[str, object], item)["id"]
            for item in cast(list[dict[str, object]], payload["data"])
        ]
        assert ids_in_body == link_ids


def _forward_links_response(composite_ids: list[str]) -> dict[str, object]:
    """A JSON:API forward-link page used by the delete pre-read."""
    return {
        "data": [{"type": "linkedworkitems", "id": cid} for cid in composite_ids],
        "meta": {"totalCount": len(composite_ids)},
    }


class TestDeleteWorkItemLinksDryRun:
    """Tests for ``delete_work_item_links`` with ``dry_run=True``."""

    async def test_dry_run_returns_payload_without_calling_delete(
        self, mock_ctx: MagicMock, mock_client: AsyncMock
    ) -> None:
        mock_client.get.return_value = _forward_links_response(
            ["MyProj/MCPT-1/parent/MyProj/MCPT-2"]
        )

        result = await delete_work_item_links(
            mock_ctx,
            project_id="MyProj",
            work_item_id="MCPT-1",
            links=[
                WorkItemLinkRef(role="parent", target_work_item_id="MCPT-2"),
                WorkItemLinkRef(role="verifies", target_work_item_id="MCPT-3"),
            ],
            dry_run=True,
        )

        mock_client.delete.assert_not_called()
        assert isinstance(result, WorkItemLinksDeleteResult)
        assert result.dry_run is True
        assert result.deleted is False
        # link_ids are always populated since they are reconstructed from input.
        assert result.link_ids == [
            "MyProj/MCPT-1/parent/MyProj/MCPT-2",
            "MyProj/MCPT-1/verifies/MyProj/MCPT-3",
        ]
        # The pre-read runs on dry_run so the preview's split is accurate.
        assert result.deleted_link_ids == ["MyProj/MCPT-1/parent/MyProj/MCPT-2"]
        assert result.not_found_link_ids == ["MyProj/MCPT-1/verifies/MyProj/MCPT-3"]
        assert result.payload_preview is not None
        body_ids = [
            cast(dict[str, object], item)["id"]
            for item in cast(list[dict[str, object]], result.payload_preview["data"])
        ]
        assert body_ids == result.link_ids


class TestDeleteWorkItemLinksHappyPath:
    """Tests for a successful ``delete_work_item_links`` call."""

    async def test_returns_deleted_true_on_204(
        self, mock_ctx: MagicMock, mock_client: AsyncMock
    ) -> None:
        mock_client.get.return_value = _forward_links_response(
            ["MyProj/MCPT-1/parent/MyProj/MCPT-2"]
        )
        mock_client.delete.return_value = {}

        result = await delete_work_item_links(
            mock_ctx,
            project_id="MyProj",
            work_item_id="MCPT-1",
            links=[WorkItemLinkRef(role="parent", target_work_item_id="MCPT-2")],
            dry_run=False,
        )

        assert isinstance(result, WorkItemLinksDeleteResult)
        assert result.deleted is True
        assert result.dry_run is False
        assert result.link_ids == ["MyProj/MCPT-1/parent/MyProj/MCPT-2"]
        assert result.deleted_link_ids == ["MyProj/MCPT-1/parent/MyProj/MCPT-2"]
        assert result.not_found_link_ids == []
        assert result.payload_preview is None

    async def test_matched_and_no_op_split_on_mixed_batch(
        self, mock_ctx: MagicMock, mock_client: AsyncMock
    ) -> None:
        mock_client.get.return_value = _forward_links_response(
            ["MyProj/MCPT-1/parent/MyProj/MCPT-2"]
        )
        mock_client.delete.return_value = {}

        result = await delete_work_item_links(
            mock_ctx,
            project_id="MyProj",
            work_item_id="MCPT-1",
            links=[
                WorkItemLinkRef(role="parent", target_work_item_id="MCPT-2"),
                WorkItemLinkRef(role="verifies", target_work_item_id="MCPT-3"),
            ],
            dry_run=False,
        )

        mock_client.delete.assert_awaited_once()
        assert result.deleted_link_ids == ["MyProj/MCPT-1/parent/MyProj/MCPT-2"]
        assert result.not_found_link_ids == ["MyProj/MCPT-1/verifies/MyProj/MCPT-3"]

    async def test_full_miss_reports_all_not_found(
        self, mock_ctx: MagicMock, mock_client: AsyncMock
    ) -> None:
        mock_client.get.return_value = _forward_links_response([])
        mock_client.delete.return_value = {}

        result = await delete_work_item_links(
            mock_ctx,
            project_id="MyProj",
            work_item_id="MCPT-1",
            links=[WorkItemLinkRef(role="parent", target_work_item_id="MCPT-2")],
            dry_run=False,
        )

        assert result.deleted_link_ids == []
        assert result.not_found_link_ids == ["MyProj/MCPT-1/parent/MyProj/MCPT-2"]

    async def test_delete_called_with_correct_path_and_body(
        self, mock_ctx: MagicMock, mock_client: AsyncMock
    ) -> None:
        mock_client.get.return_value = _forward_links_response([])
        mock_client.delete.return_value = {}

        await delete_work_item_links(
            mock_ctx,
            project_id="MyProj",
            work_item_id="MCPT-1",
            links=[
                WorkItemLinkRef(role="parent", target_work_item_id="MCPT-2"),
                WorkItemLinkRef(role="verifies", target_work_item_id="MCPT-3"),
            ],
            dry_run=False,
        )

        args, kwargs = mock_client.delete.call_args
        assert args == ("/projects/MyProj/workitems/MCPT-1/linkedworkitems",)
        body = kwargs["json"]
        ids = [item["id"] for item in body["data"]]
        assert ids == [
            "MyProj/MCPT-1/parent/MyProj/MCPT-2",
            "MyProj/MCPT-1/verifies/MyProj/MCPT-3",
        ]

    async def test_target_project_defaults_to_source(
        self, mock_ctx: MagicMock, mock_client: AsyncMock
    ) -> None:
        mock_client.get.return_value = _forward_links_response([])
        mock_client.delete.return_value = {}

        result = await delete_work_item_links(
            mock_ctx,
            project_id="MyProj",
            work_item_id="MCPT-1",
            links=[WorkItemLinkRef(role="parent", target_work_item_id="MCPT-2")],
            dry_run=False,
        )

        assert result.link_ids == ["MyProj/MCPT-1/parent/MyProj/MCPT-2"]


class TestDeleteWorkItemLinksErrorMapping:
    """Tests that domain exceptions are mapped at the tool layer."""

    async def test_401_raises_permission_error(
        self, mock_ctx: MagicMock, mock_client: AsyncMock
    ) -> None:
        mock_client.get.return_value = _forward_links_response(
            ["MyProj/MCPT-1/parent/MyProj/MCPT-2"]
        )
        mock_client.delete.side_effect = PolarionAuthError("auth", status_code=401)

        with pytest.raises(PermissionError):
            await delete_work_item_links(
                mock_ctx,
                project_id="MyProj",
                work_item_id="MCPT-1",
                links=[
                    WorkItemLinkRef(role="parent", target_work_item_id="MCPT-2"),
                ],
                dry_run=False,
            )

    async def test_404_raises_value_error_about_source_wi(
        self, mock_ctx: MagicMock, mock_client: AsyncMock
    ) -> None:
        """Path-level 404 means the source WI itself is missing.

        Body-level 'link not found' is silently ignored by Polarion
        (confirmed against the testdrive instance, 2026-05-22), so the
        only 404 the tool layer sees is the source-WI variant.
        """
        mock_client.get.return_value = _forward_links_response(
            ["MyProj/MCPT-1/parent/MyProj/MCPT-2"]
        )
        mock_client.delete.side_effect = PolarionNotFoundError(
            "not found", status_code=404
        )

        with pytest.raises(ValueError, match="Source work item 'MCPT-1' not found"):
            await delete_work_item_links(
                mock_ctx,
                project_id="MyProj",
                work_item_id="MCPT-1",
                links=[
                    WorkItemLinkRef(role="parent", target_work_item_id="MCPT-2"),
                ],
                dry_run=False,
            )

    async def test_generic_polarion_error_raises_runtime_error(
        self, mock_ctx: MagicMock, mock_client: AsyncMock
    ) -> None:
        mock_client.get.return_value = _forward_links_response(
            ["MyProj/MCPT-1/parent/MyProj/MCPT-2"]
        )
        mock_client.delete.side_effect = PolarionError("server error", status_code=500)

        with pytest.raises(RuntimeError, match="delete work item links"):
            await delete_work_item_links(
                mock_ctx,
                project_id="MyProj",
                work_item_id="MCPT-1",
                links=[
                    WorkItemLinkRef(role="parent", target_work_item_id="MCPT-2"),
                ],
                dry_run=False,
            )

    async def test_preread_404_raises_value_error_about_source_wi(
        self, mock_ctx: MagicMock, mock_client: AsyncMock
    ) -> None:
        mock_client.get.side_effect = PolarionNotFoundError(
            "not found", status_code=404
        )

        with pytest.raises(ValueError, match="Source work item 'MCPT-1' not found"):
            await delete_work_item_links(
                mock_ctx,
                project_id="MyProj",
                work_item_id="MCPT-1",
                links=[WorkItemLinkRef(role="parent", target_work_item_id="MCPT-2")],
                dry_run=False,
            )
        mock_client.delete.assert_not_called()

    async def test_preread_401_raises_permission_error(
        self, mock_ctx: MagicMock, mock_client: AsyncMock
    ) -> None:
        mock_client.get.side_effect = PolarionAuthError("auth", status_code=401)

        with pytest.raises(PermissionError):
            await delete_work_item_links(
                mock_ctx,
                project_id="MyProj",
                work_item_id="MCPT-1",
                links=[WorkItemLinkRef(role="parent", target_work_item_id="MCPT-2")],
                dry_run=False,
            )
        mock_client.delete.assert_not_called()

    async def test_preread_unreachable_blocks_before_delete(
        self, mock_ctx: MagicMock, mock_client: AsyncMock
    ) -> None:
        """A 5xx pre-read fails closed -- the delete is never attempted."""
        mock_client.get.side_effect = PolarionError("server error", status_code=500)

        with pytest.raises(RuntimeError, match="Refusing the write"):
            await delete_work_item_links(
                mock_ctx,
                project_id="MyProj",
                work_item_id="MCPT-1",
                links=[WorkItemLinkRef(role="parent", target_work_item_id="MCPT-2")],
                dry_run=False,
            )
        mock_client.delete.assert_not_called()


class TestDeleteWorkItemLinksFieldValidation:
    """Verify ``min_length=1`` constraints on the required parameters."""

    @staticmethod
    def _adapter_for(param_name: str) -> TypeAdapter[object]:
        hints = get_type_hints(delete_work_item_links)
        sig = inspect.signature(delete_work_item_links)
        field_info = sig.parameters[param_name].default
        return TypeAdapter(Annotated[hints[param_name], field_info])

    def test_project_id_rejects_empty(self) -> None:
        with pytest.raises(ValidationError):
            self._adapter_for("project_id").validate_python("")

    def test_work_item_id_rejects_empty(self) -> None:
        with pytest.raises(ValidationError):
            self._adapter_for("work_item_id").validate_python("")

    def test_links_rejects_empty_list(self) -> None:
        with pytest.raises(ValidationError):
            self._adapter_for("links").validate_python([])

    def test_over_cap_links_rejected(self) -> None:
        too_many = [
            {"role": "parent", "target_work_item_id": "MCPT-2"} for _ in range(51)
        ]
        with pytest.raises(ValidationError):
            self._adapter_for("links").validate_python(too_many)

    def test_cap_boundary_accepted(self) -> None:
        exactly_50 = [
            {"role": "parent", "target_work_item_id": "MCPT-2"} for _ in range(50)
        ]
        result = cast(
            list[object], self._adapter_for("links").validate_python(exactly_50)
        )
        assert len(result) == 50

    def test_ref_role_rejects_empty(self) -> None:
        with pytest.raises(ValidationError):
            WorkItemLinkRef(role="", target_work_item_id="MCPT-2")

    def test_ref_target_work_item_id_rejects_empty(self) -> None:
        with pytest.raises(ValidationError):
            WorkItemLinkRef(role="parent", target_work_item_id="")

    def test_required_string_fields_accept_non_empty(self) -> None:
        for name in ("project_id", "work_item_id"):
            assert self._adapter_for(name).validate_python("x") == "x"


class TestBuildUpdateLinkPayload:
    """Tests for the private ``_build_update_link_payload`` helper."""

    def test_composite_id_same_project(self) -> None:
        link_id, path, payload = _build_update_link_payload(
            source_project_id="MyProj",
            source_work_item_id="MCPT-1",
            spec=WorkItemLinkUpdateSpec(
                role="parent", target_work_item_id="MCPT-2", suspect=False
            ),
        )

        assert link_id == "MyProj/MCPT-1/parent/MyProj/MCPT-2"
        assert path == (
            "/projects/MyProj/workitems/MCPT-1/linkedworkitems/parent/MyProj/MCPT-2"
        )
        data = cast(dict[str, object], payload["data"])
        assert data["type"] == "linkedworkitems"
        assert data["id"] == link_id

    def test_cross_project_composite_id(self) -> None:
        link_id, path, _payload = _build_update_link_payload(
            source_project_id="MyProj",
            source_work_item_id="MCPT-1",
            spec=WorkItemLinkUpdateSpec(
                role="verifies",
                target_work_item_id="MCPT-9",
                target_project_id="OtherProj",
                suspect=True,
            ),
        )

        assert link_id == "MyProj/MCPT-1/verifies/OtherProj/MCPT-9"
        assert path == (
            "/projects/MyProj/workitems/MCPT-1/linkedworkitems/verifies/OtherProj/MCPT-9"
        )

    def test_suspect_only_omits_revision(self) -> None:
        _link_id, _path, payload = _build_update_link_payload(
            source_project_id="MyProj",
            source_work_item_id="MCPT-1",
            spec=WorkItemLinkUpdateSpec(
                role="parent", target_work_item_id="MCPT-2", suspect=True
            ),
        )
        attributes = cast(
            dict[str, object], cast(dict[str, object], payload["data"])["attributes"]
        )
        assert attributes == {"suspect": True}

    def test_revision_only_omits_suspect(self) -> None:
        _link_id, _path, payload = _build_update_link_payload(
            source_project_id="MyProj",
            source_work_item_id="MCPT-1",
            spec=WorkItemLinkUpdateSpec(
                role="parent", target_work_item_id="MCPT-2", revision="1234"
            ),
        )
        attributes = cast(
            dict[str, object], cast(dict[str, object], payload["data"])["attributes"]
        )
        assert attributes == {"revision": "1234"}

    def test_both_attributes_present(self) -> None:
        _link_id, _path, payload = _build_update_link_payload(
            source_project_id="MyProj",
            source_work_item_id="MCPT-1",
            spec=WorkItemLinkUpdateSpec(
                role="parent",
                target_work_item_id="MCPT-2",
                suspect=False,
                revision="HEAD",
            ),
        )
        attributes = cast(
            dict[str, object], cast(dict[str, object], payload["data"])["attributes"]
        )
        assert attributes == {"revision": "HEAD", "suspect": False}

    def test_suspect_false_is_emitted(self) -> None:
        """``suspect=False`` is a real value (clearing the flag), not omitted."""
        _link_id, _path, payload = _build_update_link_payload(
            source_project_id="MyProj",
            source_work_item_id="MCPT-1",
            spec=WorkItemLinkUpdateSpec(
                role="parent", target_work_item_id="MCPT-2", suspect=False
            ),
        )
        attributes = cast(
            dict[str, object], cast(dict[str, object], payload["data"])["attributes"]
        )
        assert attributes == {"suspect": False}


class TestUpdateWorkItemLinkDryRun:
    """Tests for ``update_work_item_link`` with ``dry_run=True``."""

    async def test_dry_run_returns_preview_without_calling_patch(
        self, mock_ctx: MagicMock, mock_client: AsyncMock
    ) -> None:
        result = await update_work_item_link(
            mock_ctx,
            project_id="MyProj",
            work_item_id="MCPT-1",
            role="parent",
            target_work_item_id="MCPT-2",
            target_project_id=None,
            suspect=True,
            revision=None,
            dry_run=True,
        )

        mock_client.patch.assert_not_called()
        assert isinstance(result, WorkItemLinkUpdateResult)
        assert result.dry_run is True
        assert result.updated is False
        assert result.link_id == "MyProj/MCPT-1/parent/MyProj/MCPT-2"
        assert result.payload_preview is not None
        data = cast(dict[str, object], result.payload_preview["data"])
        assert data["id"] == "MyProj/MCPT-1/parent/MyProj/MCPT-2"


class TestUpdateWorkItemLinkHappyPath:
    """Tests for a successful ``update_work_item_link`` call."""

    async def test_returns_updated_true_and_link_id(
        self, mock_ctx: MagicMock, mock_client: AsyncMock
    ) -> None:
        mock_client.patch.return_value = {}

        result = await update_work_item_link(
            mock_ctx,
            project_id="MyProj",
            work_item_id="MCPT-1",
            role="parent",
            target_work_item_id="MCPT-2",
            target_project_id=None,
            suspect=False,
            revision=None,
            dry_run=False,
        )

        assert isinstance(result, WorkItemLinkUpdateResult)
        assert result.updated is True
        assert result.dry_run is False
        assert result.link_id == "MyProj/MCPT-1/parent/MyProj/MCPT-2"
        assert result.payload_preview is None

    async def test_patch_called_with_correct_path_and_body(
        self, mock_ctx: MagicMock, mock_client: AsyncMock
    ) -> None:
        mock_client.patch.return_value = {}

        await update_work_item_link(
            mock_ctx,
            project_id="MyProj",
            work_item_id="MCPT-1",
            role="verifies",
            target_work_item_id="MCPT-3",
            target_project_id="OtherProj",
            suspect=None,
            revision="42",
            dry_run=False,
        )

        mock_client.patch.assert_called_once()
        args, kwargs = mock_client.patch.call_args
        assert args == (
            "/projects/MyProj/workitems/MCPT-1/linkedworkitems/verifies/OtherProj/MCPT-3",
        )
        data = cast(dict[str, object], kwargs["json"]["data"])
        assert data["id"] == "MyProj/MCPT-1/verifies/OtherProj/MCPT-3"
        assert data["attributes"] == {"revision": "42"}


class TestUpdateWorkItemLinkErrors:
    """Domain exceptions are raised, not returned in the result."""

    async def test_not_found_raises_value_error(
        self, mock_ctx: MagicMock, mock_client: AsyncMock
    ) -> None:
        mock_client.patch.side_effect = PolarionNotFoundError(
            "no such link", status_code=404
        )

        with pytest.raises(ValueError, match="404"):
            await update_work_item_link(
                mock_ctx,
                project_id="MyProj",
                work_item_id="MCPT-1",
                role="nonexistent_role",
                target_work_item_id="MCPT-2",
                target_project_id=None,
                suspect=True,
                revision=None,
                dry_run=False,
            )

    async def test_polarion_error_raises_runtime_error(
        self, mock_ctx: MagicMock, mock_client: AsyncMock
    ) -> None:
        mock_client.patch.side_effect = PolarionError("bad revision", status_code=400)

        with pytest.raises(RuntimeError, match="400"):
            await update_work_item_link(
                mock_ctx,
                project_id="MyProj",
                work_item_id="MCPT-1",
                role="parent",
                target_work_item_id="MCPT-2",
                target_project_id=None,
                suspect=None,
                revision="not-a-revision",
                dry_run=False,
            )


class TestUpdateWorkItemLinkAuthError:
    """Auth errors raise PermissionError."""

    async def test_auth_error_raises_permission_error(
        self, mock_ctx: MagicMock, mock_client: AsyncMock
    ) -> None:
        mock_client.patch.side_effect = PolarionAuthError("auth", status_code=401)

        with pytest.raises(PermissionError):
            await update_work_item_link(
                mock_ctx,
                project_id="MyProj",
                work_item_id="MCPT-1",
                role="parent",
                target_work_item_id="MCPT-2",
                target_project_id=None,
                suspect=True,
                revision=None,
                dry_run=False,
            )


class TestUpdateWorkItemLinkFieldValidation:
    """Verify ``min_length=1`` constraints and at-least-one-attribute check."""

    @staticmethod
    def _adapter_for(param_name: str) -> TypeAdapter[object]:
        hints = get_type_hints(update_work_item_link)
        sig = inspect.signature(update_work_item_link)
        field_info = sig.parameters[param_name].default
        return TypeAdapter(Annotated[hints[param_name], field_info])

    def test_project_id_rejects_empty(self) -> None:
        with pytest.raises(ValidationError):
            self._adapter_for("project_id").validate_python("")

    def test_work_item_id_rejects_empty(self) -> None:
        with pytest.raises(ValidationError):
            self._adapter_for("work_item_id").validate_python("")

    def test_role_rejects_empty(self) -> None:
        with pytest.raises(ValidationError):
            self._adapter_for("role").validate_python("")

    def test_target_work_item_id_rejects_empty(self) -> None:
        with pytest.raises(ValidationError):
            self._adapter_for("target_work_item_id").validate_python("")

    async def test_both_attributes_none_raises_value_error(
        self, mock_ctx: MagicMock, mock_client: AsyncMock
    ) -> None:
        """suspect=None and revision=None must be rejected before any PATCH."""
        with pytest.raises(ValueError, match="at least one"):
            await update_work_item_link(
                mock_ctx,
                project_id="MyProj",
                work_item_id="MCPT-1",
                role="parent",
                target_work_item_id="MCPT-2",
                target_project_id=None,
                suspect=None,
                revision=None,
            )
        mock_client.patch.assert_not_called()

    async def test_suspect_only_accepted(
        self, mock_ctx: MagicMock, mock_client: AsyncMock
    ) -> None:
        mock_client.patch.return_value = {}
        result = await update_work_item_link(
            mock_ctx,
            project_id="P",
            work_item_id="P-1",
            role="r",
            target_work_item_id="P-2",
            target_project_id=None,
            suspect=True,
            revision=None,
            dry_run=False,
        )
        assert result.updated is True

    async def test_revision_only_accepted(
        self, mock_ctx: MagicMock, mock_client: AsyncMock
    ) -> None:
        mock_client.patch.return_value = {}
        result = await update_work_item_link(
            mock_ctx,
            project_id="P",
            work_item_id="P-1",
            role="r",
            target_work_item_id="P-2",
            target_project_id=None,
            suspect=None,
            revision="HEAD",
            dry_run=False,
        )
        assert result.updated is True


class TestBuildDocumentCommentsPayload:
    """Unit tests for the private ``_build_document_comments_payload`` helper."""

    def test_single_plain_text_spec(self) -> None:
        payload = _build_document_comments_payload(
            specs=[DocumentCommentSpec(text="hello")],
            project_id="Proj",
            space_id="Space",
            document_name="Doc",
        )
        data = payload["data"]
        assert isinstance(data, list)
        assert len(data) == 1
        item = data[0]
        assert isinstance(item, dict)
        assert item["type"] == "document_comments"
        attrs = item["attributes"]
        assert isinstance(attrs, dict)
        assert attrs["text"] == {"type": "text/plain", "value": "hello"}
        assert "resolved" not in attrs
        assert "relationships" not in item

    def test_multiple_specs_produce_multiple_items(self) -> None:
        payload = _build_document_comments_payload(
            specs=[
                DocumentCommentSpec(text="first"),
                DocumentCommentSpec(text="second"),
            ],
            project_id="P",
            space_id="S",
            document_name="D",
        )
        assert len(payload["data"]) == 2  # type: ignore[arg-type]

    def test_resolved_true_in_attributes(self) -> None:
        payload = _build_document_comments_payload(
            specs=[DocumentCommentSpec(text="t", resolved=True)],
            project_id="P",
            space_id="S",
            document_name="D",
        )
        attrs = payload["data"][0]["attributes"]  # type: ignore[index]
        assert attrs["resolved"] is True  # type: ignore[index]

    def test_resolved_false_in_attributes(self) -> None:
        """Explicit False must be sent, not silently omitted like None."""
        payload = _build_document_comments_payload(
            specs=[DocumentCommentSpec(text="t", resolved=False)],
            project_id="P",
            space_id="S",
            document_name="D",
        )
        attrs = payload["data"][0]["attributes"]  # type: ignore[index]
        assert attrs["resolved"] is False  # type: ignore[index]

    def test_resolved_none_omits_key(self) -> None:
        payload = _build_document_comments_payload(
            specs=[DocumentCommentSpec(text="t", resolved=None)],
            project_id="P",
            space_id="S",
            document_name="D",
        )
        attrs = payload["data"][0]["attributes"]  # type: ignore[index]
        assert "resolved" not in attrs  # type: ignore[operator]

    def test_author_relationship(self) -> None:
        payload = _build_document_comments_payload(
            specs=[DocumentCommentSpec(text="t", author_id="alice")],
            project_id="P",
            space_id="S",
            document_name="D",
        )
        item = payload["data"][0]  # type: ignore[index]
        assert isinstance(item, dict)
        assert item["relationships"]["author"] == {  # type: ignore[index]
            "data": {"id": "alice", "type": "users"}
        }

    def test_parent_comment_full_path_composed(self) -> None:
        """Short parent_comment_id is expanded to the full 4-segment path."""
        payload = _build_document_comments_payload(
            specs=[DocumentCommentSpec(text="t", parent_comment_id="c1")],
            project_id="Proj",
            space_id="Space",
            document_name="Doc",
        )
        item = payload["data"][0]  # type: ignore[index]
        assert isinstance(item, dict)
        rel = item["relationships"]["parentComment"]  # type: ignore[index]
        assert rel == {"data": {"id": "Proj/Space/Doc/c1", "type": "document_comments"}}

    def test_both_relationships_present(self) -> None:
        payload = _build_document_comments_payload(
            specs=[
                DocumentCommentSpec(text="t", author_id="bob", parent_comment_id="c5")
            ],
            project_id="P",
            space_id="S",
            document_name="D",
        )
        item = payload["data"][0]  # type: ignore[index]
        assert isinstance(item, dict)
        rels = item["relationships"]
        assert "author" in rels  # type: ignore[operator]
        assert "parentComment" in rels  # type: ignore[operator]

    def test_html_format_preserved(self) -> None:
        payload = _build_document_comments_payload(
            specs=[DocumentCommentSpec(text="<b>bold</b>", text_format="text/html")],
            project_id="P",
            space_id="S",
            document_name="D",
        )
        text_field = payload["data"][0]["attributes"]["text"]  # type: ignore[index]
        assert text_field["type"] == "text/html"  # type: ignore[index]

    def test_payload_wrapped_in_array(self) -> None:
        payload = _build_document_comments_payload(
            specs=[DocumentCommentSpec(text="t")],
            project_id="P",
            space_id="S",
            document_name="D",
        )
        assert isinstance(payload["data"], list)
        assert len(payload["data"]) == 1  # type: ignore[arg-type]


class TestCreateDocumentCommentsDryRun:
    """Verify dry_run returns preview without calling Polarion."""

    async def test_dry_run_no_post_call(
        self, mock_ctx: MagicMock, mock_client: AsyncMock
    ) -> None:
        result = await create_document_comments(
            mock_ctx,
            project_id="P",
            space_id="S",
            document_name="D",
            comments=[DocumentCommentSpec(text="hello")],
            dry_run=True,
        )
        mock_client.post.assert_not_called()
        assert result.dry_run is True
        assert result.created is False
        assert result.comment_ids == []

    async def test_dry_run_payload_preview_populated(
        self, mock_ctx: MagicMock, mock_client: AsyncMock
    ) -> None:
        result = await create_document_comments(
            mock_ctx,
            project_id="P",
            space_id="S",
            document_name="D",
            comments=[
                DocumentCommentSpec(text="first"),
                DocumentCommentSpec(text="second"),
            ],
            dry_run=True,
        )
        assert result.payload_preview is not None
        assert isinstance(result.payload_preview["data"], list)
        assert len(result.payload_preview["data"]) == 2  # type: ignore[arg-type]

    async def test_dry_run_with_relationships(
        self, mock_ctx: MagicMock, mock_client: AsyncMock
    ) -> None:
        result = await create_document_comments(
            mock_ctx,
            project_id="P",
            space_id="S",
            document_name="D",
            comments=[
                DocumentCommentSpec(
                    text="reply", author_id="bob", parent_comment_id="c5"
                )
            ],
            dry_run=True,
        )
        assert result.payload_preview is not None
        item = result.payload_preview["data"][0]  # type: ignore[index]
        assert isinstance(item, dict)
        assert "author" in item["relationships"]  # type: ignore[index]
        assert "parentComment" in item["relationships"]  # type: ignore[index]


class TestCreateDocumentCommentsHappyPath:
    """Verify successful creation extracts and returns short comment IDs."""

    async def test_returns_short_comment_ids(
        self, mock_ctx: MagicMock, mock_client: AsyncMock
    ) -> None:
        mock_client.post.return_value = {
            "data": [
                {"type": "document_comments", "id": "p/s/d/c42"},
                {"type": "document_comments", "id": "p/s/d/c43"},
            ]
        }
        result = await create_document_comments(
            mock_ctx,
            project_id="p",
            space_id="s",
            document_name="d",
            comments=[
                DocumentCommentSpec(text="first"),
                DocumentCommentSpec(text="second"),
            ],
            dry_run=False,
        )
        assert result.created is True
        assert result.dry_run is False
        assert result.comment_ids == ["c42", "c43"]
        assert result.payload_preview is None

    async def test_post_called_with_correct_path(
        self, mock_ctx: MagicMock, mock_client: AsyncMock
    ) -> None:
        mock_client.post.return_value = {
            "data": [{"type": "document_comments", "id": "Proj/_default/Doc/c1"}]
        }
        await create_document_comments(
            mock_ctx,
            project_id="Proj",
            space_id="_default",
            document_name="Doc",
            comments=[DocumentCommentSpec(text="hello")],
            dry_run=False,
        )
        call_args = mock_client.post.call_args
        expected_path = "/projects/Proj/spaces/_default/documents/Doc/comments"
        assert call_args[0][0] == expected_path

    async def test_path_url_encodes_spaces(
        self, mock_ctx: MagicMock, mock_client: AsyncMock
    ) -> None:
        mock_client.post.return_value = {
            "data": [{"type": "document_comments", "id": "P/My%20Space/My%20Doc/c1"}]
        }
        await create_document_comments(
            mock_ctx,
            project_id="P",
            space_id="My Space",
            document_name="My Doc",
            comments=[DocumentCommentSpec(text="hi")],
            dry_run=False,
        )
        call_args = mock_client.post.call_args
        assert "My%20Space" in call_args[0][0]
        assert "My%20Doc" in call_args[0][0]

    async def test_post_body_multiple_items(
        self, mock_ctx: MagicMock, mock_client: AsyncMock
    ) -> None:
        mock_client.post.return_value = {
            "data": [
                {"type": "document_comments", "id": "P/S/D/c1"},
                {"type": "document_comments", "id": "P/S/D/c2"},
            ]
        }
        await create_document_comments(
            mock_ctx,
            project_id="P",
            space_id="S",
            document_name="D",
            comments=[
                DocumentCommentSpec(text="one"),
                DocumentCommentSpec(text="two"),
            ],
            dry_run=False,
        )
        body = mock_client.post.call_args[1]["json"]  # type: ignore[index]
        assert len(body["data"]) == 2  # type: ignore[index]

    async def test_resolved_true_sent_in_payload(
        self, mock_ctx: MagicMock, mock_client: AsyncMock
    ) -> None:
        mock_client.post.return_value = {
            "data": [{"type": "document_comments", "id": "P/S/D/c1"}]
        }
        await create_document_comments(
            mock_ctx,
            project_id="P",
            space_id="S",
            document_name="D",
            comments=[DocumentCommentSpec(text="done", resolved=True)],
            dry_run=False,
        )
        body = mock_client.post.call_args[1]["json"]
        assert body["data"][0]["attributes"]["resolved"] is True


class TestCreateDocumentCommentsErrors:
    """Verify domain exceptions map to the correct public exceptions."""

    async def test_auth_error_raises_permission_error(
        self, mock_ctx: MagicMock, mock_client: AsyncMock
    ) -> None:
        mock_client.post.side_effect = PolarionAuthError("auth", status_code=401)
        with pytest.raises(PermissionError, match="POLARION_TOKEN"):
            await create_document_comments(
                mock_ctx,
                project_id="P",
                space_id="S",
                document_name="D",
                comments=[DocumentCommentSpec(text="hi")],
                dry_run=False,
            )

    async def test_not_found_raises_value_error(
        self, mock_ctx: MagicMock, mock_client: AsyncMock
    ) -> None:
        mock_client.post.side_effect = PolarionNotFoundError(
            "not found", status_code=404
        )
        with pytest.raises(ValueError, match="list_documents"):
            await create_document_comments(
                mock_ctx,
                project_id="P",
                space_id="S",
                document_name="D",
                comments=[DocumentCommentSpec(text="hi")],
                dry_run=False,
            )

    async def test_other_polarion_error_raises_runtime_error(
        self, mock_ctx: MagicMock, mock_client: AsyncMock
    ) -> None:
        mock_client.post.side_effect = PolarionError("boom", status_code=500)
        with pytest.raises(RuntimeError, match="boom"):
            await create_document_comments(
                mock_ctx,
                project_id="P",
                space_id="S",
                document_name="D",
                comments=[DocumentCommentSpec(text="hi")],
                dry_run=False,
            )

    async def test_empty_response_raises_runtime_error(
        self, mock_ctx: MagicMock, mock_client: AsyncMock
    ) -> None:
        """201 with no IDs must raise rather than silently return created=True."""
        mock_client.post.return_value = {}
        with pytest.raises(RuntimeError, match="no comment IDs"):
            await create_document_comments(
                mock_ctx,
                project_id="P",
                space_id="S",
                document_name="D",
                comments=[DocumentCommentSpec(text="hi")],
                dry_run=False,
            )


class TestCreateDocumentCommentsFieldValidation:
    """Verify Field constraints on create_document_comments and DocumentCommentSpec.

    FastMCP enforces ``min_length`` via JSON Schema at the MCP protocol
    layer before the tool function is invoked; calling the function
    directly bypasses that gate.  We rebuild a ``TypeAdapter`` from each
    parameter's annotation + ``FieldInfo`` to prove the constraint is
    wired correctly at the schema layer.
    """

    @staticmethod
    def _adapter_for(param_name: str) -> TypeAdapter[object]:
        hints = get_type_hints(create_document_comments)
        sig = inspect.signature(create_document_comments)
        field_info = sig.parameters[param_name].default
        return TypeAdapter(Annotated[hints[param_name], field_info])

    def test_space_id_rejects_empty(self) -> None:
        with pytest.raises(ValidationError):
            self._adapter_for("space_id").validate_python("")

    def test_space_id_accepts_non_empty(self) -> None:
        assert self._adapter_for("space_id").validate_python("_default") == "_default"

    def test_document_name_rejects_empty(self) -> None:
        with pytest.raises(ValidationError):
            self._adapter_for("document_name").validate_python("")

    def test_document_name_accepts_non_empty(self) -> None:
        assert self._adapter_for("document_name").validate_python("MySRS") == "MySRS"

    def test_spec_text_rejects_empty(self) -> None:
        with pytest.raises(ValidationError):
            DocumentCommentSpec(text="")

    def test_spec_text_accepts_non_empty(self) -> None:
        spec = DocumentCommentSpec(text="hello")
        assert spec.text == "hello"

    def test_spec_default_text_format_is_plain(self) -> None:
        spec = DocumentCommentSpec(text="hello")
        assert spec.text_format == "text/plain"

    def test_comments_rejects_empty_list(self) -> None:
        with pytest.raises(ValidationError):
            self._adapter_for("comments").validate_python([])

    def test_comments_accepts_non_empty_list(self) -> None:
        specs = [{"text": "hello"}]
        result = self._adapter_for("comments").validate_python(specs)
        assert isinstance(result, list)
        assert len(result) == 1


class TestBuildDocumentCommentUpdatePayload:
    """Unit tests for _build_document_comment_update_payload (no I/O)."""

    def _build(
        self,
        *,
        project_id: str = "Proj",
        space_id: str = "Space",
        document_name: str = "Doc",
        comment_id: str = "c42",
        resolved: bool = True,
    ) -> dict:  # type: ignore[type-arg]
        return _build_document_comment_update_payload(
            project_id=project_id,
            space_id=space_id,
            document_name=document_name,
            comment_id=comment_id,
            resolved=resolved,
        )

    def test_payload_is_dict_not_list(self) -> None:
        payload = self._build()
        assert isinstance(payload["data"], dict)
        assert not isinstance(payload["data"], list)

    def test_type_is_document_comments(self) -> None:
        payload = self._build()
        assert payload["data"]["type"] == "document_comments"  # type: ignore[index]

    def test_resolved_true_included(self) -> None:
        payload = self._build(resolved=True)
        assert payload["data"]["attributes"]["resolved"] is True  # type: ignore[index]

    def test_resolved_false_included(self) -> None:
        payload = self._build(resolved=False)
        assert payload["data"]["attributes"]["resolved"] is False  # type: ignore[index]

    def test_full_id_composed_from_four_segments(self) -> None:
        payload = self._build(
            project_id="P",
            space_id="S",
            document_name="D",
            comment_id="c42",
        )
        assert payload["data"]["id"] == "P/S/D/c42"  # type: ignore[index]

    def test_space_default_value_in_id(self) -> None:
        payload = self._build(space_id="_default")
        assert "_default" in str(payload["data"]["id"])  # type: ignore[index]


class TestUpdateDocumentCommentDryRun:
    """Dry-run path must not call client.patch."""

    async def test_dry_run_skips_patch(
        self, mock_ctx: MagicMock, mock_client: AsyncMock
    ) -> None:
        await update_document_comment(
            mock_ctx,
            project_id="Proj",
            space_id="Space",
            document_name="Doc",
            comment_id="c42",
            resolved=True,
            dry_run=True,
        )
        mock_client.patch.assert_not_called()

    async def test_dry_run_result_flags(
        self, mock_ctx: MagicMock, mock_client: AsyncMock
    ) -> None:
        result = await update_document_comment(
            mock_ctx,
            project_id="Proj",
            space_id="Space",
            document_name="Doc",
            comment_id="c42",
            resolved=True,
            dry_run=True,
        )
        assert isinstance(result, DocumentCommentUpdateResult)
        assert result.dry_run is True
        assert result.updated is False

    async def test_dry_run_comment_id_is_none(
        self, mock_ctx: MagicMock, mock_client: AsyncMock
    ) -> None:
        result = await update_document_comment(
            mock_ctx,
            project_id="Proj",
            space_id="Space",
            document_name="Doc",
            comment_id="c42",
            resolved=True,
            dry_run=True,
        )
        assert result.comment_id is None

    async def test_dry_run_payload_preview_populated(
        self, mock_ctx: MagicMock, mock_client: AsyncMock
    ) -> None:
        result = await update_document_comment(
            mock_ctx,
            project_id="Proj",
            space_id="Space",
            document_name="Doc",
            comment_id="c42",
            resolved=True,
            dry_run=True,
        )
        assert result.payload_preview is not None
        data = result.payload_preview["data"]
        assert data["type"] == "document_comments"  # type: ignore[index]
        assert data["attributes"]["resolved"] is True  # type: ignore[index]
        assert data["id"] == "Proj/Space/Doc/c42"  # type: ignore[index]

    async def test_dry_run_resolved_echoed_true(
        self, mock_ctx: MagicMock, mock_client: AsyncMock
    ) -> None:
        result = await update_document_comment(
            mock_ctx,
            project_id="Proj",
            space_id="Space",
            document_name="Doc",
            comment_id="c42",
            resolved=True,
            dry_run=True,
        )
        assert result.resolved is True

    async def test_dry_run_resolved_echoed_false(
        self, mock_ctx: MagicMock, mock_client: AsyncMock
    ) -> None:
        result = await update_document_comment(
            mock_ctx,
            project_id="Proj",
            space_id="Space",
            document_name="Doc",
            comment_id="c42",
            resolved=False,
            dry_run=True,
        )
        assert result.resolved is False


class TestUpdateDocumentCommentHappyPath:
    """Successful PATCH path (204 No Content)."""

    async def test_patch_called_with_correct_path(
        self, mock_ctx: MagicMock, mock_client: AsyncMock
    ) -> None:
        mock_client.patch.return_value = {}
        await update_document_comment(
            mock_ctx,
            project_id="Proj",
            space_id="Space",
            document_name="Doc",
            comment_id="c42",
            resolved=True,
            dry_run=False,
        )
        path = mock_client.patch.call_args[0][0]
        assert path == "/projects/Proj/spaces/Space/documents/Doc/comments/c42"

    async def test_patch_body_resolved_true(
        self, mock_ctx: MagicMock, mock_client: AsyncMock
    ) -> None:
        mock_client.patch.return_value = {}
        await update_document_comment(
            mock_ctx,
            project_id="Proj",
            space_id="Space",
            document_name="Doc",
            comment_id="c42",
            resolved=True,
            dry_run=False,
        )
        body = mock_client.patch.call_args[1]["json"]
        assert body["data"]["attributes"]["resolved"] is True

    async def test_patch_body_resolved_false(
        self, mock_ctx: MagicMock, mock_client: AsyncMock
    ) -> None:
        mock_client.patch.return_value = {}
        await update_document_comment(
            mock_ctx,
            project_id="Proj",
            space_id="Space",
            document_name="Doc",
            comment_id="c42",
            resolved=False,
            dry_run=False,
        )
        body = mock_client.patch.call_args[1]["json"]
        assert body["data"]["attributes"]["resolved"] is False

    async def test_returns_updated_true(
        self, mock_ctx: MagicMock, mock_client: AsyncMock
    ) -> None:
        mock_client.patch.return_value = {}
        result = await update_document_comment(
            mock_ctx,
            project_id="Proj",
            space_id="Space",
            document_name="Doc",
            comment_id="c42",
            resolved=True,
            dry_run=False,
        )
        assert isinstance(result, DocumentCommentUpdateResult)
        assert result.updated is True
        assert result.dry_run is False
        assert result.comment_id == "c42"
        assert result.resolved is True
        assert result.payload_preview is None

    async def test_path_url_encodes_segments(
        self, mock_ctx: MagicMock, mock_client: AsyncMock
    ) -> None:
        mock_client.patch.return_value = {}
        await update_document_comment(
            mock_ctx,
            project_id="My Proj",
            space_id="My Space",
            document_name="My Doc",
            comment_id="c 1",
            resolved=True,
            dry_run=False,
        )
        path = mock_client.patch.call_args[0][0]
        assert "My%20Proj" in path
        assert "My%20Space" in path
        assert "My%20Doc" in path
        assert "c%201" in path


class TestUpdateDocumentCommentErrors:
    """Exception mapping for PATCH failures."""

    async def test_auth_error_raises_permission_error(
        self, mock_ctx: MagicMock, mock_client: AsyncMock
    ) -> None:
        mock_client.patch.side_effect = PolarionAuthError("auth", status_code=401)
        with pytest.raises(PermissionError, match="POLARION_TOKEN"):
            await update_document_comment(
                mock_ctx,
                project_id="Proj",
                space_id="Space",
                document_name="Doc",
                comment_id="c42",
                resolved=True,
                dry_run=False,
            )

    async def test_not_found_raises_value_error(
        self, mock_ctx: MagicMock, mock_client: AsyncMock
    ) -> None:
        mock_client.patch.side_effect = PolarionNotFoundError(
            "not found", status_code=404
        )
        with pytest.raises(ValueError, match="list_document_comments"):
            await update_document_comment(
                mock_ctx,
                project_id="Proj",
                space_id="Space",
                document_name="Doc",
                comment_id="c42",
                resolved=True,
                dry_run=False,
            )

    async def test_other_error_raises_runtime_error(
        self, mock_ctx: MagicMock, mock_client: AsyncMock
    ) -> None:
        mock_client.patch.side_effect = PolarionError("boom", status_code=500)
        with pytest.raises(RuntimeError, match="boom"):
            await update_document_comment(
                mock_ctx,
                project_id="Proj",
                space_id="Space",
                document_name="Doc",
                comment_id="c42",
                resolved=True,
                dry_run=False,
            )


def _enum_get_response(ids: list[str]) -> dict[str, object]:
    """Shape a ``getAvailableOptions`` reply for the guard tests."""
    return {
        "data": [{"id": i, "name": i} for i in ids],
        "meta": {"totalCount": len(ids)},
    }


@pytest.fixture
def reset_enum_guard_caches() -> None:
    """Drop guard caches between integration tests so each scenario starts cold."""
    _clear_guard_caches()


async def _call_create_wi(mock_ctx: MagicMock, **overrides: object) -> object:
    """Invoke ``create_work_items`` with a single-spec default batch.

    ``project_id`` / ``dry_run`` are top-level tool params; all other
    overrides are per-item and fold into one ``WorkItemCreateSpec``.
    """
    project_id = cast(str, overrides.pop("project_id", "MyProj"))
    dry_run = cast(bool, overrides.pop("dry_run", False))
    spec_fields: dict[str, object] = {"title": "t", "type": "task"}
    spec_fields.update(overrides)
    spec = WorkItemCreateSpec(**spec_fields)  # type: ignore[arg-type]
    return await create_work_items(
        mock_ctx, project_id=project_id, items=[spec], dry_run=dry_run
    )


async def _call_create_doc(mock_ctx: MagicMock, **overrides: object) -> object:
    """Invoke ``create_document`` with explicit defaults for every Field."""
    defaults: dict[str, object] = {
        "project_id": "MyProj",
        "space_id": "_default",
        "module_name": "Doc",
        "title": "x",
        "type": "systemRequirementSpecification",
        "status": None,
        "home_page_content": None,
        "custom_fields": None,
        "dry_run": False,
    }
    defaults.update(overrides)
    return await create_document(mock_ctx, **defaults)  # type: ignore[arg-type]


async def _call_update_doc(mock_ctx: MagicMock, **overrides: object) -> object:
    """Invoke ``update_document`` with explicit defaults for every Field."""
    defaults: dict[str, object] = {
        "project_id": "MyProj",
        "space_id": "_default",
        "document_name": "Doc",
        "title": None,
        "status": None,
        "type": None,
        "home_page_content_html": None,
        "custom_fields": None,
        "workflow_action": None,
        "dry_run": False,
    }
    defaults.update(overrides)
    return await update_document(mock_ctx, **defaults)  # type: ignore[arg-type]


class TestEnumGuardCreateWorkItem:
    """Integration: ``create_work_items`` rejects ghost enum ids before POST."""

    async def test_unlisted_severity_raises_before_post(
        self,
        mock_ctx: MagicMock,
        mock_client: AsyncMock,
        reset_enum_guard_caches: None,
    ) -> None:
        # ``task`` makes the prior type-axis check pass; severity then trips.
        mock_client.get.return_value = _enum_get_response(
            ["task", "must_have", "should_have"]
        )

        with pytest.raises(ValueError, match="severity='ghost'"):
            await _call_create_wi(mock_ctx, severity="ghost")
        mock_client.post.assert_not_called()

    async def test_bad_enum_on_later_item_aborts_whole_batch(
        self,
        mock_ctx: MagicMock,
        mock_client: AsyncMock,
        reset_enum_guard_caches: None,
    ) -> None:
        # The guard loop runs per item before any POST, so a ghost severity
        # on the SECOND item must reject the whole batch with nothing sent.
        mock_client.get.return_value = _enum_get_response(
            ["task", "must_have", "should_have"]
        )

        with pytest.raises(ValueError, match="severity='ghost'"):
            await create_work_items(
                mock_ctx,
                project_id="MyProj",
                items=[
                    WorkItemCreateSpec(title="ok", type="task", severity="must_have"),
                    WorkItemCreateSpec(title="bad", type="task", severity="ghost"),
                ],
                dry_run=False,
            )
        mock_client.post.assert_not_called()

    async def test_listed_severity_reaches_post(
        self,
        mock_ctx: MagicMock,
        mock_client: AsyncMock,
        reset_enum_guard_caches: None,
    ) -> None:
        # Single response shape works for any number of guard probes plus
        # the final create — the guard ignores ``data`` keys it does not
        # expect (no ``id`` field on the create response is fine).
        mock_client.get.return_value = _enum_get_response(
            ["task", "must_have", "open", "50.0"]
        )
        mock_client.post.return_value = {"data": [{"id": "MyProj/MCPT-9"}]}

        result = await _call_create_wi(mock_ctx, severity="must_have")
        assert result.work_item_ids == ["MCPT-9"]  # type: ignore[attr-defined]
        mock_client.post.assert_awaited_once()

    async def test_guard_runs_on_dry_run_too(
        self,
        mock_ctx: MagicMock,
        mock_client: AsyncMock,
        reset_enum_guard_caches: None,
    ) -> None:
        mock_client.get.return_value = _enum_get_response(["task"])

        with pytest.raises(ValueError, match="type='unknown'"):
            await _call_create_wi(mock_ctx, type="unknown", dry_run=True)
        mock_client.post.assert_not_called()

    async def test_custom_fields_on_create_logs_warning_but_proceeds(
        self,
        mock_ctx: MagicMock,
        mock_client: AsyncMock,
        reset_enum_guard_caches: None,
        caplog: pytest.LogCaptureFixture,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        # No schema for project custom-field keys → guard cannot validate
        # on create; surface the gap as a warning rather than a hard fail.
        # See test_polarion_error_blocks_write_and_logs in test_guard.py
        # for why we re-enable propagation here.
        import logging  # noqa: PLC0415 -- fixture-local import is intentional

        monkeypatch.setattr(logging.getLogger("mcp_server_polarion"), "propagate", True)
        mock_client.get.return_value = _enum_get_response(["task"])
        mock_client.post.return_value = {"data": [{"id": "MyProj/MCPT-1"}]}
        caplog.set_level("WARNING", logger="mcp_server_polarion.tools.write")

        await _call_create_wi(mock_ctx, custom_fields={"risk_score": 5})
        assert any("cannot be schema-validated" in r.message for r in caplog.records)


class TestEnumGuardUpdateWorkItem:
    """Integration: ``update_work_item`` pre-fetches type then guards."""

    async def test_unlisted_priority_raises_after_prefetch(
        self,
        mock_ctx: MagicMock,
        mock_client: AsyncMock,
        reset_enum_guard_caches: None,
    ) -> None:
        # First GET call (pre-fetch): the work item itself.
        # Second GET call (guard): the priority options.
        mock_client.get.side_effect = [
            _make_get_response(),
            _enum_get_response(["90.0", "50.0", "10.0"]),
        ]
        with pytest.raises(ValueError, match="priority='999'"):
            await _call_update(mock_ctx, priority="999")
        mock_client.patch.assert_not_called()

    async def test_unknown_custom_field_key_raises_after_prefetch(
        self,
        mock_ctx: MagicMock,
        mock_client: AsyncMock,
        reset_enum_guard_caches: None,
    ) -> None:
        # Pre-fetch surfaces the existing customs; the unknown key does not
        # appear there so the guard rejects.
        mock_client.get.return_value = _make_get_response(
            custom_fields={"risk_score": 5}
        )
        with pytest.raises(ValueError, match="release_train_id"):
            await _call_update(
                mock_ctx,
                custom_fields={"release_train_id": "RT-42"},
            )
        mock_client.patch.assert_not_called()

    async def test_unlisted_resolution_raises_after_prefetch(
        self,
        mock_ctx: MagicMock,
        mock_client: AsyncMock,
        reset_enum_guard_caches: None,
    ) -> None:
        # ``resolution`` is a ghost-prone enum like type/status/severity;
        # an unlisted id must be rejected, not written verbatim.
        mock_client.get.side_effect = [
            _make_get_response(),
            _enum_get_response(["done", "wontfix", "duplicate"]),
        ]
        with pytest.raises(ValueError, match="resolution='ghost_resolution'"):
            await _call_update(mock_ctx, resolution="ghost_resolution")
        mock_client.patch.assert_not_called()

    async def test_status_scoped_by_target_type_on_change_type_to(
        self,
        mock_ctx: MagicMock,
        mock_client: AsyncMock,
        reset_enum_guard_caches: None,
    ) -> None:
        # Pre-fetch returns a ``task``; ``change_type_to='requirement'``
        # means status must be validated against the target type's options,
        # so the guard's status lookup must use ``type='requirement'``.
        # GETs in order: work-item pre-fetch, type options (``~`` axis),
        # status options (target-type axis).
        mock_client.get.side_effect = [
            _make_get_response(),
            _enum_get_response(["requirement"]),
            _enum_get_response(["draft", "approved"]),
        ]
        result = await _call_update(
            mock_ctx, change_type_to="requirement", status="draft", dry_run=True
        )
        assert result.dry_run is True  # type: ignore[attr-defined]
        status_calls = [
            c
            for c in mock_client.get.call_args_list
            if "fields/status/actions/getAvailableOptions" in c.args[0]
        ]
        assert status_calls, "guard must probe status options"
        assert status_calls[0].kwargs["params"]["type"] == "requirement"


class TestEnumGuardCreateDocument:
    """Integration: ``create_document`` rejects ghost document types."""

    async def test_unlisted_type_raises_before_post(
        self,
        mock_ctx: MagicMock,
        mock_client: AsyncMock,
        reset_enum_guard_caches: None,
    ) -> None:
        mock_client.get.return_value = _enum_get_response(
            ["systemRequirementSpecification", "softwareRequirementSpecification"]
        )

        with pytest.raises(ValueError, match="type='productRequirementSpecification'"):
            await _call_create_doc(
                mock_ctx,
                module_name="NewSpec",
                type="productRequirementSpecification",
            )
        mock_client.post.assert_not_called()


class TestEnumGuardUpdateDocument:
    """Integration: ``update_document`` rejects ghost type / status."""

    async def test_unlisted_status_raises_before_patch(
        self,
        mock_ctx: MagicMock,
        mock_client: AsyncMock,
        reset_enum_guard_caches: None,
    ) -> None:
        mock_client.get.return_value = _enum_get_response(["draft", "approved"])

        with pytest.raises(ValueError, match="status='ghost'"):
            await _call_update_doc(mock_ctx, status="ghost")
        mock_client.patch.assert_not_called()

    async def test_unknown_custom_field_key_raises_via_priming_get(
        self,
        mock_ctx: MagicMock,
        mock_client: AsyncMock,
        reset_enum_guard_caches: None,
    ) -> None:
        # Cache miss → guard does one inline get_document-shaped read; the
        # unknown key is absent from the document's customs, so it rejects.
        mock_client.get.return_value = {
            "data": {"attributes": {"title": "x", "doc_risk": 3}}
        }

        with pytest.raises(ValueError, match="ghost_key"):
            await _call_update_doc(mock_ctx, custom_fields={"ghost_key": 1})
        mock_client.patch.assert_not_called()

    async def test_known_custom_field_key_passes_guard(
        self,
        mock_ctx: MagicMock,
        mock_client: AsyncMock,
        reset_enum_guard_caches: None,
    ) -> None:
        mock_client.get.return_value = {
            "data": {"attributes": {"title": "x", "doc_risk": 3}}
        }

        result = await _call_update_doc(
            mock_ctx, custom_fields={"doc_risk": 9}, dry_run=True
        )
        assert result.dry_run is True  # type: ignore[attr-defined]


class TestUpdateDocumentAnchorlessGuard:
    """Integration: ``update_document`` rejects anchorless body blocks."""

    async def test_anchorless_paragraph_raises(
        self,
        mock_ctx: MagicMock,
        mock_client: AsyncMock,
        reset_enum_guard_caches: None,
    ) -> None:
        with pytest.raises(ValueError, match="anchorless <p>"):
            await _call_update_doc(
                mock_ctx, home_page_content_html="<p>Note</p>", dry_run=True
            )
        mock_client.patch.assert_not_called()

    async def test_stamped_paragraph_passes(
        self,
        mock_ctx: MagicMock,
        mock_client: AsyncMock,
        reset_enum_guard_caches: None,
    ) -> None:
        result = await _call_update_doc(
            mock_ctx,
            home_page_content_html='<p id="polarion_mcp_1">Note</p>',
            dry_run=True,
        )
        assert result.dry_run is True  # type: ignore[attr-defined]

    async def test_heading_only_passes(
        self,
        mock_ctx: MagicMock,
        mock_client: AsyncMock,
        reset_enum_guard_caches: None,
    ) -> None:
        result = await _call_update_doc(
            mock_ctx, home_page_content_html="<h1>Title</h1>", dry_run=True
        )
        assert result.dry_run is True  # type: ignore[attr-defined]
