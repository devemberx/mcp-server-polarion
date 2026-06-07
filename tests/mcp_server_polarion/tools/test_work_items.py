"""Tests for the work item query/create/update tools."""

from __future__ import annotations

import inspect
from typing import Annotated, cast, get_type_hints
from unittest.mock import AsyncMock, MagicMock

import pytest
from pydantic import TypeAdapter, ValidationError

from mcp_server_polarion.core.exceptions import (
    PolarionAuthError,
    PolarionError,
    PolarionNotFoundError,
)
from mcp_server_polarion.models import (
    EnumOption,
    Hyperlink,
    PaginatedResult,
    WorkItemCreateSpec,
    WorkItemDetail,
    WorkItemRead,
    WorkItemsCreateResult,
    WorkItemUpdateResult,
)
from mcp_server_polarion.tools._shared import cache as _cache_mod
from mcp_server_polarion.tools.work_items import (
    _build_create_work_items_payload,
    _build_update_work_item_payload,
    _build_work_item_resource,
    _extract_created_work_item_ids,
    create_work_items,
    get_work_item,
    list_work_item_enum_options,
    list_work_items,
    read_work_item,
    update_work_item,
)


def _project_enum_get_response(enum_name: str, ids: list[str]) -> dict[str, object]:
    """Single-enumeration response: ``data`` is a dict, options nested under it."""
    return {
        "data": {
            "type": "enumerations",
            "id": enum_name,
            "attributes": {"options": [{"id": i, "name": i} for i in ids]},
        }
    }


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


def _enum_get_response(ids: list[str]) -> dict[str, object]:
    """Shape a ``getAvailableOptions`` reply for the guard tests."""
    return {
        "data": [{"id": i, "name": i} for i in ids],
        "meta": {"totalCount": len(ids)},
    }


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


_STATUS_DATA: list[dict[str, object]] = [
    {
        "id": "draft",
        "name": "Draft",
        "description": "Initial state",
        "default": True,
        "hidden": False,
        "terminal": False,
    },
    {
        "id": "inreview",
        "name": "In Review",
        "default": False,
        "hidden": False,
        "terminal": False,
    },
    {
        "id": "approved",
        "name": "Approved",
        "default": False,
        "hidden": False,
        "terminal": True,
    },
]


@pytest.fixture
def reset_enum_guard_caches() -> None:
    """Drop guard caches between integration tests so each scenario starts cold."""
    _cache_mod._enum_option_cache.clear()
    _cache_mod._project_enum_cache.clear()
    _cache_mod._work_item_custom_key_cache.clear()
    _cache_mod._document_custom_key_cache.clear()


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


class TestCreateWorkItemsHyperlinkRoleGuard:
    """``create_work_items`` validates each hyperlink role before writing."""

    async def test_unknown_hyperlink_role_raises_without_post(
        self, mock_ctx: MagicMock, mock_client: AsyncMock
    ) -> None:
        mock_client.get.return_value = _project_enum_get_response(
            "hyperlink-role", ["ref_int", "ref_ext"]
        )

        with pytest.raises(ValueError, match="ghost") as exc:
            await create_work_items(
                mock_ctx,
                project_id="MyProj",
                items=[
                    WorkItemCreateSpec(
                        title="t",
                        type="task",
                        hyperlinks=[Hyperlink(role="ghost", uri="https://e.com")],
                    )
                ],
                dry_run=True,
            )

        assert "ref_ext" in str(exc.value)
        mock_client.post.assert_not_called()

    async def test_valid_hyperlink_role_proceeds_to_post(
        self, mock_ctx: MagicMock, mock_client: AsyncMock
    ) -> None:
        mock_client.get.return_value = _project_enum_get_response(
            "hyperlink-role", ["ref_int", "ref_ext"]
        )
        mock_client.post.return_value = {
            "data": [{"type": "workitems", "id": "MyProj/MCPT-1"}]
        }

        result = await create_work_items(
            mock_ctx,
            project_id="MyProj",
            items=[
                WorkItemCreateSpec(
                    title="t",
                    type="task",
                    hyperlinks=[Hyperlink(role="ref_ext", uri="https://e.com")],
                )
            ],
            dry_run=False,
        )

        assert result.created is True
        mock_client.post.assert_called_once()


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


class TestUpdateWorkItemHyperlinkRoleGuard:
    """``update_work_item`` validates hyperlink roles before the PATCH."""

    async def test_unknown_hyperlink_role_raises_without_patch(
        self, mock_ctx: MagicMock, mock_client: AsyncMock
    ) -> None:
        mock_client.get.return_value = _project_enum_get_response(
            "hyperlink-role", ["ref_int", "ref_ext"]
        )

        with pytest.raises(ValueError, match="ghost") as exc:
            await _call_update(
                mock_ctx,
                hyperlinks=[Hyperlink(role="ghost", uri="https://e.com")],
                dry_run=True,
            )

        assert "ref_ext" in str(exc.value)
        mock_client.patch.assert_not_called()


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
        # Polarion inlines customs as top-level attributes; the post-PATCH GET
        # reuses ``parse_work_item_detail``, so populated customs (riskLevel,
        # effortHours) must land on ``result.current.custom_fields`` automatically.
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


class TestListWorkItemEnumOptions:
    """Tests for the ``list_work_item_enum_options`` tool."""

    async def test_returns_enum_options(
        self, mock_ctx: MagicMock, mock_client: AsyncMock
    ) -> None:
        mock_client.get.return_value = {
            "data": _STATUS_DATA,
            "meta": {"totalCount": 3},
        }

        result = await list_work_item_enum_options(
            mock_ctx,
            project_id="MCP_Test_Project",
            field_id="status",
            work_item_type="task",
            page_size=100,
            page_number=1,
        )

        assert isinstance(result, PaginatedResult)
        assert len(result.items) == 3
        assert result.total_count == 3
        assert result.has_more is False
        first = result.items[0]
        assert isinstance(first, EnumOption)
        assert first.id == "draft"
        assert first.name == "Draft"
        assert first.description == "Initial state"
        assert first.default is True
        assert first.terminal is False
        assert result.items[2].terminal is True

    async def test_request_path_and_params(
        self, mock_ctx: MagicMock, mock_client: AsyncMock
    ) -> None:
        mock_client.get.return_value = {
            "data": [],
            "meta": {"totalCount": 0},
        }

        await list_work_item_enum_options(
            mock_ctx,
            project_id="MCP_Test_Project",
            field_id="status",
            work_item_type="task",
            page_size=50,
            page_number=2,
        )

        args, kwargs = mock_client.get.call_args
        assert args[0] == (
            "/projects/MCP_Test_Project"
            "/workitems/fields/status"
            "/actions/getAvailableOptions"
        )
        params = kwargs["params"]
        assert params["type"] == "task"
        assert params["page[size]"] == 50
        assert params["page[number]"] == 2

    async def test_type_agnostic_tilde_passes_through(
        self, mock_ctx: MagicMock, mock_client: AsyncMock
    ) -> None:
        mock_client.get.return_value = {
            "data": [],
            "meta": {"totalCount": 0},
        }

        await list_work_item_enum_options(
            mock_ctx,
            project_id="MCP_Test_Project",
            field_id="type",
            work_item_type="~",
            page_size=100,
            page_number=1,
        )

        _, kwargs = mock_client.get.call_args
        assert kwargs["params"]["type"] == "~"

    async def test_missing_optional_fields_default(
        self, mock_ctx: MagicMock, mock_client: AsyncMock
    ) -> None:
        mock_client.get.return_value = {
            "data": [{"id": "open", "name": "Open"}],
            "meta": {"totalCount": 1},
        }

        result = await list_work_item_enum_options(
            mock_ctx,
            project_id="MCP_Test_Project",
            field_id="status",
            work_item_type="task",
            page_size=100,
            page_number=1,
        )

        opt = result.items[0]
        assert opt.id == "open"
        assert opt.description == ""
        assert opt.default is False
        assert opt.hidden is False
        assert opt.terminal is False

    async def test_non_bool_flag_falls_back_to_false(
        self, mock_ctx: MagicMock, mock_client: AsyncMock
    ) -> None:
        mock_client.get.return_value = {
            "data": [
                {
                    "id": "weird",
                    "name": "Weird",
                    "default": "true",
                    "hidden": 1,
                    "terminal": None,
                }
            ],
            "meta": {"totalCount": 1},
        }

        result = await list_work_item_enum_options(
            mock_ctx,
            project_id="MCP_Test_Project",
            field_id="status",
            work_item_type="task",
            page_size=100,
            page_number=1,
        )

        opt = result.items[0]
        assert opt.default is False
        assert opt.hidden is False
        assert opt.terminal is False

    async def test_pagination_has_more(
        self, mock_ctx: MagicMock, mock_client: AsyncMock
    ) -> None:
        mock_client.get.return_value = {
            "data": _STATUS_DATA * 34,
            "meta": {"totalCount": 150},
        }

        result = await list_work_item_enum_options(
            mock_ctx,
            project_id="MCP_Test_Project",
            field_id="status",
            work_item_type="task",
            page_size=100,
            page_number=1,
        )

        assert result.total_count == 150
        assert result.has_more is True

    async def test_not_found_raises_value_error(
        self, mock_ctx: MagicMock, mock_client: AsyncMock
    ) -> None:
        mock_client.get.side_effect = PolarionNotFoundError(
            "Not found",
            status_code=404,
        )

        with pytest.raises(ValueError, match="No enum options"):
            await list_work_item_enum_options(
                mock_ctx,
                project_id="MCP_Test_Project",
                field_id="nope",
                work_item_type="task",
                page_size=100,
                page_number=1,
            )

    async def test_auth_error_raises_permission_error(
        self, mock_ctx: MagicMock, mock_client: AsyncMock
    ) -> None:
        mock_client.get.side_effect = PolarionAuthError(
            "Unauthorized",
            status_code=401,
        )

        with pytest.raises(PermissionError, match="POLARION_TOKEN"):
            await list_work_item_enum_options(
                mock_ctx,
                project_id="MCP_Test_Project",
                field_id="status",
                work_item_type="task",
                page_size=100,
                page_number=1,
            )

    async def test_generic_error_raises_runtime_error(
        self, mock_ctx: MagicMock, mock_client: AsyncMock
    ) -> None:
        mock_client.get.side_effect = PolarionError(
            "Server error",
            status_code=500,
        )

        with pytest.raises(RuntimeError, match="Failed to list enum options"):
            await list_work_item_enum_options(
                mock_ctx,
                project_id="MCP_Test_Project",
                field_id="status",
                work_item_type="task",
                page_size=100,
                page_number=1,
            )


class TestListWorkItemEnumOptionsFieldValidation:
    """Verify Field constraints on ``list_work_item_enum_options`` parameters."""

    @staticmethod
    def _adapter_for(param_name: str) -> TypeAdapter[object]:
        hints = get_type_hints(list_work_item_enum_options)
        sig = inspect.signature(list_work_item_enum_options)
        field_info = sig.parameters[param_name].default
        return TypeAdapter(Annotated[hints[param_name], field_info])

    def test_page_size_rejects_above_max(self) -> None:
        with pytest.raises(ValidationError):
            self._adapter_for("page_size").validate_python(101)

    def test_page_size_accepts_max(self) -> None:
        assert self._adapter_for("page_size").validate_python(100) == 100

    def test_page_size_rejects_zero(self) -> None:
        with pytest.raises(ValidationError):
            self._adapter_for("page_size").validate_python(0)

    def test_page_number_rejects_zero(self) -> None:
        with pytest.raises(ValidationError):
            self._adapter_for("page_number").validate_python(0)


class TestListWorkItems:
    """Tests for the ``list_work_items`` tool."""

    async def test_returns_work_items(
        self, mock_ctx: MagicMock, mock_client: AsyncMock
    ) -> None:
        mock_client.get.return_value = {
            "data": [
                {
                    "type": "workitems",
                    "id": "proj1/MCPT-001",
                    "attributes": {
                        "title": "Login Feature",
                        "type": "requirement",
                        "status": "draft",
                        "priority": "90.0",
                        "updated": "2026-04-29T10:23:00Z",
                    },
                    "relationships": {
                        "module": {
                            "data": {
                                "type": "documents",
                                "id": "proj1/Design/Software Requirement Specification",
                            }
                        },
                        "assignee": {
                            "data": [
                                {"type": "users", "id": "proj1/alice"},
                                {"type": "users", "id": "proj1/bob"},
                            ]
                        },
                    },
                },
                {
                    "type": "workitems",
                    "id": "proj1/MCPT-002",
                    "attributes": {
                        "title": "Logout Feature",
                        "type": "requirement",
                        "status": "approved",
                    },
                    "relationships": {
                        "module": {"data": None},
                        "assignee": {"data": []},
                    },
                },
            ],
            "meta": {"totalCount": 2},
        }

        result = await list_work_items(
            mock_ctx,
            project_id="proj1",
            query=None,
            page_size=100,
            page_number=1,
        )

        assert isinstance(result, PaginatedResult)
        assert len(result.items) == 2
        assert result.total_count == 2

        first = result.items[0]
        assert first.id == "MCPT-001"
        assert first.title == "Login Feature"
        assert first.priority == "90.0"
        assert first.updated == "2026-04-29T10:23:00Z"
        assert first.space_id == "Design"
        assert first.document_name == "Software Requirement Specification"
        assert first.assignee_ids == ["alice", "bob"]

        second = result.items[1]
        assert second.id == "MCPT-002"
        assert second.priority == ""
        assert second.updated == ""
        assert second.space_id == ""
        assert second.document_name == ""
        assert second.assignee_ids == []

    async def test_sparse_fieldset_requested(
        self, mock_ctx: MagicMock, mock_client: AsyncMock
    ) -> None:
        mock_client.get.return_value = {
            "data": [],
            "meta": {"totalCount": 0},
        }

        await list_work_items(
            mock_ctx,
            project_id="proj1",
            query=None,
            page_size=100,
            page_number=1,
        )

        _, kwargs = mock_client.get.call_args
        assert "fields[workitems]" in kwargs["params"]

    async def test_project_not_found(
        self, mock_ctx: MagicMock, mock_client: AsyncMock
    ) -> None:
        mock_client.get.side_effect = PolarionNotFoundError(
            "Not found",
            status_code=404,
        )

        with pytest.raises(ValueError, match="not found"):
            await list_work_items(
                mock_ctx,
                project_id="missing",
                query=None,
                page_size=100,
                page_number=1,
            )

    async def test_strips_project_prefix_from_id(
        self, mock_ctx: MagicMock, mock_client: AsyncMock
    ) -> None:
        mock_client.get.return_value = {
            "data": [
                {
                    "id": "myproject/WI-100",
                    "attributes": {
                        "title": "Test",
                        "type": "task",
                        "status": "open",
                    },
                },
            ],
            "meta": {"totalCount": 1},
        }

        result = await list_work_items(
            mock_ctx,
            project_id="myproject",
            query=None,
            page_size=100,
            page_number=1,
        )

        assert result.items[0].id == "WI-100"

    async def test_query_param_forwarded(
        self, mock_ctx: MagicMock, mock_client: AsyncMock
    ) -> None:
        mock_client.get.return_value = {
            "data": [],
            "meta": {"totalCount": 0},
        }

        await list_work_items(
            mock_ctx,
            project_id="proj1",
            query="type:testCase",
            page_size=100,
            page_number=1,
        )

        _, kwargs = mock_client.get.call_args
        assert kwargs["params"]["query"] == "type:testCase"

    async def test_query_none_omits_param(
        self, mock_ctx: MagicMock, mock_client: AsyncMock
    ) -> None:
        mock_client.get.return_value = {
            "data": [],
            "meta": {"totalCount": 0},
        }

        await list_work_items(
            mock_ctx,
            project_id="proj1",
            query=None,
            page_size=100,
            page_number=1,
        )

        _, kwargs = mock_client.get.call_args
        assert "query" not in kwargs["params"]

    async def test_sql_prefix_query_passed_verbatim(
        self, mock_ctx: MagicMock, mock_client: AsyncMock
    ) -> None:
        sql_query = (
            "SQL:(SELECT item.* FROM POLARION.WORKITEM item "
            "WHERE item.C_TYPE = 'requirement')"
        )
        mock_client.get.return_value = {
            "data": [],
            "meta": {"totalCount": 0},
        }

        await list_work_items(
            mock_ctx,
            project_id="proj1",
            query=sql_query,
            page_size=100,
            page_number=1,
        )

        _, kwargs = mock_client.get.call_args
        assert kwargs["params"]["query"] == sql_query

    async def test_query_returns_matching_items(
        self, mock_ctx: MagicMock, mock_client: AsyncMock
    ) -> None:
        mock_client.get.return_value = {
            "data": [
                {
                    "id": "proj1/MCPT-001",
                    "attributes": {
                        "title": "Login Feature",
                        "type": "requirement",
                        "status": "approved",
                    },
                },
            ],
            "meta": {"totalCount": 1},
        }

        result = await list_work_items(
            mock_ctx,
            project_id="proj1",
            query="type:requirement AND status:approved",
            page_size=100,
            page_number=1,
        )

        assert isinstance(result, PaginatedResult)
        assert len(result.items) == 1
        assert result.items[0].id == "MCPT-001"

    async def test_total_count_floor_when_api_returns_zero(
        self, mock_ctx: MagicMock, mock_client: AsyncMock
    ) -> None:
        """When Polarion omits totalCount (returns 0), use item count as minimum."""
        mock_client.get.return_value = {
            "data": [
                {
                    "id": "proj1/MCPT-001",
                    "attributes": {
                        "title": "A",
                        "type": "requirement",
                        "status": "open",
                    },
                },
                {
                    "id": "proj1/MCPT-002",
                    "attributes": {
                        "title": "B",
                        "type": "requirement",
                        "status": "open",
                    },
                },
            ],
            "meta": {"totalCount": 0},  # Polarion quirk: 0 even when items exist
        }

        result = await list_work_items(
            mock_ctx,
            project_id="proj1",
            query="type:requirement",
            page_size=100,
            page_number=1,
        )

        # total_count should be at least 2 (the number of returned items)
        assert result.total_count >= 2


class TestGetWorkItem:
    """Tests for the ``get_work_item`` tool."""

    async def test_returns_work_item_detail(
        self, mock_ctx: MagicMock, mock_client: AsyncMock
    ) -> None:
        mock_client.get.return_value = {
            "data": {
                "type": "workitems",
                "id": "proj1/MCPT-001",
                "attributes": {
                    "title": "Login Feature",
                    "type": "requirement",
                    "status": "draft",
                    "priority": "75.0",
                    "updated": "2026-04-29T10:23:00Z",
                    "created": "2026-04-01T09:00:00Z",
                    "outlineNumber": "1.2.3",
                    "hyperlinks": [
                        {
                            "role": "ref_ext",
                            "title": "Spec",
                            "uri": "https://example.com/spec",
                        },
                        {"role": "impl", "title": "", "uri": ""},
                    ],
                    "description": {
                        "type": "text/html",
                        "value": (
                            "<p>User must be able to <strong>log in</strong>.</p>"
                        ),
                    },
                },
                "relationships": {
                    "module": {
                        "data": {
                            "type": "documents",
                            "id": "proj1/Design/SRS",
                        }
                    },
                    "assignee": {"data": [{"type": "users", "id": "proj1/alice"}]},
                    "author": {"data": {"type": "users", "id": "proj1/bob"}},
                },
            },
        }

        result = await get_work_item(
            mock_ctx,
            project_id="proj1",
            work_item_id="MCPT-001",
            include_description_html=True,
        )

        assert isinstance(result, WorkItemDetail)
        assert result.id == "MCPT-001"
        assert result.title == "Login Feature"
        assert result.type == "requirement"
        assert result.status == "draft"
        assert result.priority == "75.0"
        assert result.updated == "2026-04-29T10:23:00Z"
        assert result.created == "2026-04-01T09:00:00Z"
        assert result.outline_number == "1.2.3"
        assert result.space_id == "Design"
        assert result.document_name == "SRS"
        assert result.assignee_ids == ["alice"]
        assert result.author_id == "bob"
        # Entry without uri is skipped.
        assert len(result.hyperlinks) == 1
        assert result.hyperlinks[0].role == "ref_ext"
        assert result.hyperlinks[0].uri == "https://example.com/spec"
        # Raw HTML passthrough — <p>/<strong> survive verbatim, no markdownify.
        assert result.description_html == (
            "<p>User must be able to <strong>log in</strong>.</p>"
        )
        assert result.project_id == "proj1"
        assert result.custom_fields == {}

    async def test_include_description_html_false_blanks_field(
        self, mock_ctx: MagicMock, mock_client: AsyncMock
    ) -> None:
        """include_description_html=False → description_html blanked.

        ``@all`` is the only sparse-fieldset that surfaces custom fields,
        so the body still travels over the wire; the tool layer is
        responsible for stripping it from the response to save LLM
        context tokens. The default at the FastMCP layer is False; here
        we pass it explicitly because direct-call tests bypass the
        FastMCP Field default unwrap.
        """
        mock_client.get.return_value = {
            "data": {
                "id": "proj1/MCPT-007",
                "attributes": {
                    "title": "work item",
                    "type": "task",
                    "status": "draft",
                    "description": {
                        "type": "text/html",
                        "value": "<p>should be hidden</p>",
                    },
                },
            },
        }

        result = await get_work_item(
            mock_ctx,
            project_id="proj1",
            work_item_id="MCPT-007",
            include_description_html=False,
        )

        assert result.description_html == ""
        # Other metadata still populated.
        assert result.title == "work item"

    async def test_polarion_specific_markup_round_trips(
        self, mock_ctx: MagicMock, mock_client: AsyncMock
    ) -> None:
        """Polarion-specific spans / data-* attributes must survive on read.

        Core round-trip guarantee for update_work_item(description_html=).
        """
        raw = (
            '<p>Refs <span class="polarion-rte-link" '
            'data-item-id="MCPT-9" data-scope="proj1">MCPT-9</span></p>'
        )
        mock_client.get.return_value = {
            "data": {
                "id": "proj1/MCPT-008",
                "attributes": {
                    "title": "RT",
                    "type": "task",
                    "status": "draft",
                    "description": {"type": "text/html", "value": raw},
                },
            },
        }

        result = await get_work_item(
            mock_ctx,
            project_id="proj1",
            work_item_id="MCPT-008",
            include_description_html=True,
        )

        assert result.description_html == raw

    async def test_defect_severity_and_open_resolution(
        self, mock_ctx: MagicMock, mock_client: AsyncMock
    ) -> None:
        mock_client.get.return_value = {
            "data": {
                "id": "proj1/MCPT-500",
                "attributes": {
                    "title": "Login crashes",
                    "type": "defect",
                    "status": "open",
                    "severity": "blocker",
                },
            },
        }

        result = await get_work_item(
            mock_ctx,
            project_id="proj1",
            work_item_id="MCPT-500",
        )

        assert result.severity == "blocker"
        assert result.resolution == ""

    async def test_no_description(
        self, mock_ctx: MagicMock, mock_client: AsyncMock
    ) -> None:
        mock_client.get.return_value = {
            "data": {
                "id": "proj1/MCPT-002",
                "attributes": {
                    "title": "Minimal",
                    "type": "task",
                    "status": "open",
                },
            },
        }

        result = await get_work_item(
            mock_ctx,
            project_id="proj1",
            work_item_id="MCPT-002",
            include_description_html=True,
        )

        assert result.description_html == ""
        # Default values for new detail-only fields.
        assert result.author_id == ""
        assert result.created == ""
        assert result.severity == ""
        assert result.resolution == ""
        assert result.outline_number == ""
        assert result.hyperlinks == []

    async def test_not_found_raises_value_error(
        self, mock_ctx: MagicMock, mock_client: AsyncMock
    ) -> None:
        mock_client.get.side_effect = PolarionNotFoundError(
            "Not found",
            status_code=404,
        )

        with pytest.raises(ValueError, match="not found"):
            await get_work_item(
                mock_ctx,
                project_id="proj1",
                work_item_id="MCPT-999",
            )

    async def test_api_path_includes_work_item_id(
        self, mock_ctx: MagicMock, mock_client: AsyncMock
    ) -> None:
        mock_client.get.return_value = {
            "data": {
                "id": "proj1/MCPT-010",
                "attributes": {
                    "title": "Test",
                    "type": "task",
                    "status": "open",
                },
            },
        }

        await get_work_item(
            mock_ctx,
            project_id="proj1",
            work_item_id="MCPT-010",
        )

        call_path = mock_client.get.call_args[0][0]
        expected = "/projects/proj1/workitems/MCPT-010"
        assert call_path == expected

    async def test_custom_fields_populated_from_response(
        self, mock_ctx: MagicMock, mock_client: AsyncMock
    ) -> None:
        """Inline non-standard attributes flow through as ``custom_fields``."""
        rich_value = {"type": "text/html", "value": "<p>note</p>"}
        mock_client.get.return_value = {
            "data": {
                "id": "proj1/MCPT-999",
                "attributes": {
                    # Standard attributes — present but excluded from custom_fields.
                    "title": "work item with customs",
                    "type": "softwarerequirement",
                    "status": "approved",
                    "priority": "50.0",
                    # Inline custom attributes — top-level keys, not nested.
                    "riskLevel": "high",
                    "category": "user",
                    "effortHours": 12.0,
                    "reviewerNote": rich_value,
                },
            },
        }

        result = await get_work_item(
            mock_ctx,
            project_id="proj1",
            work_item_id="MCPT-999",
        )

        # Raw passthrough: rich-text values stay as the original
        # {type, value} dict — they are NOT converted to Markdown.
        assert result.custom_fields == {
            "riskLevel": "high",
            "category": "user",
            "effortHours": 12.0,
            "reviewerNote": rich_value,
        }

    async def test_custom_fields_empty_when_only_standard_attrs(
        self, mock_ctx: MagicMock, mock_client: AsyncMock
    ) -> None:
        """All-standard attributes → empty custom_fields dict."""
        mock_client.get.return_value = {
            "data": {
                "id": "proj1/MCPT-100",
                "attributes": {
                    "title": "No customs",
                    "type": "task",
                    "status": "open",
                },
            },
        }

        result = await get_work_item(
            mock_ctx,
            project_id="proj1",
            work_item_id="MCPT-100",
        )

        assert result.custom_fields == {}

    async def test_sparse_fieldset_uses_all_token(
        self, mock_ctx: MagicMock, mock_client: AsyncMock
    ) -> None:
        """``fields[workitems]=@all`` is the only token that surfaces customs."""
        mock_client.get.return_value = {
            "data": {
                "id": "proj1/MCPT-1",
                "attributes": {"title": "x", "type": "task", "status": "open"},
            },
        }

        await get_work_item(
            mock_ctx,
            project_id="proj1",
            work_item_id="MCPT-1",
        )

        _, kwargs = mock_client.get.call_args
        assert kwargs["params"]["fields[workitems]"] == "@all"


class TestReadWorkItem:
    """Tests for the ``read_work_item`` tool.

    Delegates the fetch + error mapping to ``get_work_item`` and converts
    the raw HTML body to Markdown via ``html_to_markdown()``.
    """

    async def test_html_body_converted_to_markdown(
        self, mock_ctx: MagicMock, mock_client: AsyncMock
    ) -> None:
        mock_client.get.return_value = {
            "data": {
                "type": "workitems",
                "id": "proj1/MCPT-001",
                "attributes": {
                    "title": "Login Feature",
                    "type": "requirement",
                    "status": "draft",
                    "priority": "75.0",
                    "outlineNumber": "1.2.3",
                    "description": {
                        "type": "text/html",
                        "value": (
                            "<p>User must be able to <strong>log in</strong>.</p>"
                        ),
                    },
                },
                "relationships": {
                    "module": {
                        "data": {"type": "documents", "id": "proj1/Design/SRS"},
                    },
                    "author": {"data": {"type": "users", "id": "proj1/bob"}},
                },
            },
        }

        result = await read_work_item(
            mock_ctx,
            project_id="proj1",
            work_item_id="MCPT-001",
        )

        assert isinstance(result, WorkItemRead)
        assert result.id == "MCPT-001"
        assert result.title == "Login Feature"
        assert "**log in**" in result.description
        assert "<p>" not in result.description
        assert "<strong>" not in result.description
        assert result.outline_number == "1.2.3"
        assert result.space_id == "Design"
        assert result.document_name == "SRS"
        assert result.author_id == "bob"
        assert result.project_id == "proj1"

    async def test_empty_description_yields_empty_markdown(
        self, mock_ctx: MagicMock, mock_client: AsyncMock
    ) -> None:
        mock_client.get.return_value = {
            "data": {
                "id": "proj1/MCPT-002",
                "attributes": {
                    "title": "Minimal",
                    "type": "task",
                    "status": "open",
                },
            },
        }

        result = await read_work_item(
            mock_ctx,
            project_id="proj1",
            work_item_id="MCPT-002",
        )

        assert result.description == ""

    async def test_polarion_specific_markup_collapses_to_text(
        self, mock_ctx: MagicMock, mock_client: AsyncMock
    ) -> None:
        """Polarion span/data-* attributes get stripped by html_to_markdown.

        Marks the read-only contract: WorkItemRead.description is NOT a
        round-trip shape — feeding it back to update_work_item would lose
        the polarion-rte-link span. The round-trip pair lives on
        get_work_item / update_work_item.
        """
        raw = (
            '<p>Refs <span class="polarion-rte-link" '
            'data-item-id="MCPT-9" data-scope="proj1">MCPT-9</span></p>'
        )
        mock_client.get.return_value = {
            "data": {
                "id": "proj1/MCPT-008",
                "attributes": {
                    "title": "RT",
                    "type": "task",
                    "status": "draft",
                    "description": {"type": "text/html", "value": raw},
                },
            },
        }

        result = await read_work_item(
            mock_ctx,
            project_id="proj1",
            work_item_id="MCPT-008",
        )

        assert "MCPT-9" in result.description
        assert "polarion-rte-link" not in result.description
        assert "data-item-id" not in result.description

    async def test_not_found_raises_value_error(
        self, mock_ctx: MagicMock, mock_client: AsyncMock
    ) -> None:
        mock_client.get.side_effect = PolarionNotFoundError(
            "Not found",
            status_code=404,
        )

        with pytest.raises(ValueError, match="not found"):
            await read_work_item(
                mock_ctx,
                project_id="proj1",
                work_item_id="MCPT-999",
            )

    async def test_auth_error_raises_permission_error(
        self, mock_ctx: MagicMock, mock_client: AsyncMock
    ) -> None:
        mock_client.get.side_effect = PolarionAuthError(
            "Unauthorized",
            status_code=401,
        )

        with pytest.raises(PermissionError):
            await read_work_item(
                mock_ctx,
                project_id="proj1",
                work_item_id="MCPT-001",
            )

    async def test_generic_error_raises_runtime_error(
        self, mock_ctx: MagicMock, mock_client: AsyncMock
    ) -> None:
        mock_client.get.side_effect = PolarionError(
            "boom",
            status_code=500,
        )

        with pytest.raises(RuntimeError, match="boom"):
            await read_work_item(
                mock_ctx,
                project_id="proj1",
                work_item_id="MCPT-001",
            )

    async def test_metadata_fields_carry_through(
        self, mock_ctx: MagicMock, mock_client: AsyncMock
    ) -> None:
        """Defect-specific fields (severity, resolution) and customs survive."""
        rich_value = {"type": "text/html", "value": "<p>note</p>"}
        mock_client.get.return_value = {
            "data": {
                "id": "proj1/MCPT-500",
                "attributes": {
                    "title": "Login crashes",
                    "type": "defect",
                    "status": "closed",
                    "severity": "blocker",
                    "resolution": "fixed",
                    "hyperlinks": [
                        {
                            "role": "ref_ext",
                            "title": "Spec",
                            "uri": "https://example.com/spec",
                        },
                    ],
                    "riskLevel": "high",
                    "reviewerNote": rich_value,
                },
            },
        }

        result = await read_work_item(
            mock_ctx,
            project_id="proj1",
            work_item_id="MCPT-500",
        )

        assert result.severity == "blocker"
        assert result.resolution == "fixed"
        assert len(result.hyperlinks) == 1
        assert result.hyperlinks[0].uri == "https://example.com/spec"
        # Custom fields stay raw — rich-text dicts are NOT converted to Markdown
        # because the same dict shape round-trips through update_work_item.
        assert result.custom_fields == {
            "riskLevel": "high",
            "reviewerNote": rich_value,
        }

    async def test_no_description_html_field_on_model(
        self, mock_ctx: MagicMock, mock_client: AsyncMock
    ) -> None:
        """WorkItemRead does not expose description_html — read-only contract."""
        mock_client.get.return_value = {
            "data": {
                "id": "proj1/MCPT-1",
                "attributes": {"title": "x", "type": "task", "status": "open"},
            },
        }

        result = await read_work_item(
            mock_ctx,
            project_id="proj1",
            work_item_id="MCPT-1",
        )

        assert not hasattr(result, "description_html")
