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
    """Call update_work_item with all params explicit.

    Field(...) defaults stay FieldInfo objects outside FastMCP, so every
    param must be passed; tests override only what they care about.
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
        # Inline (no customFields container); primes the pre-fetch guard cache.
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


def _wi_sample_response(*custom_dicts: dict[str, object]) -> dict[str, object]:
    """Shape a MIN-per-key list reply: representative items with inline customs."""
    return {
        "data": [
            {
                "type": "workitems",
                "id": f"MyProj/MCPT-{i}",
                "attributes": {"title": "t", "type": "task", **customs},
            }
            for i, customs in enumerate(custom_dicts)
        ]
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
    _cache_mod._document_type_custom_key_cache.clear()


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
        # Customs land flat; Polarion drops a customFields container.
        assert "customFields" not in attributes

    def test_custom_fields_collision_with_standard_attr_raises(self) -> None:
        # A custom key matching a standard attr would silently shadow it.
        with pytest.raises(ValueError, match="custom_fields keys collide"):
            _build_work_item_resource(
                spec=WorkItemCreateSpec(
                    title="x", type="task", custom_fields={"title": "y"}
                ),
                description_html="",
            )

    def test_custom_fields_skips_none_values_inside_dict(self) -> None:
        # None custom values drop; falsy non-None (e.g. 0) pass through.
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
        # zip(strict=True) guards the spec/html pairing.
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
        # Plain dict, no Pydantic objects leaked.
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
        assert "<strong>bold</strong>" in desc["value"]
        # Safe https link survives sanitize.
        assert 'href="https://example.com"' in desc["value"]

    async def test_description_strips_dangerous_link_schemes(
        self, mock_ctx: MagicMock, mock_client: AsyncMock
    ) -> None:
        """No javascript: anchor reaches the payload.

        markdown-it leaves it unrendered; sanitize_html strips it as a
        second layer.
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
        # No usable javascript: href in either quote style.
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
    """Constraints on ``items`` + ``WorkItemCreateSpec`` — direct calls bypass the
    JSON Schema gate; collection-level bounds proven via ``TypeAdapter`` rebuild.
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
        # custom_fields alone must still emit an attributes block, else PATCH 400s.
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
        """``description_html=''`` = leave unchanged (never PATCHes) — asymmetric vs
        update_document, where '' RAISES: a wiped document body orphans every heading,
        a cleared description is recoverable.
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
        # Empty description_html drops from both changes and the wire payload.
        assert result.changes == {"title": "new title"}
        assert result.payload_preview is not None
        item = cast(dict[str, object], result.payload_preview["data"])
        attributes = cast(dict[str, object], item["attributes"])
        assert "description" not in attributes
        assert attributes == {"title": "new title"}

    async def test_custom_fields_alone_satisfies_at_least_one_check(
        self, mock_ctx: MagicMock, mock_client: AsyncMock
    ) -> None:
        # custom_fields counts as a body field.
        _cache_mod.store_work_item_custom_keys(
            "MyProj", "task", frozenset({"riskLevel"})
        )
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
        # A custom key matching a standard param would shadow it.
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
        # Polarion 400s on attribute-less PATCH, so an action needs a body field.
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
        # Dry-run is rejected too: the would-be payload is invalid.
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
        # changes holds raw caller values; {type,value} wrapping is preview-only.
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
        # Preview wraps the same HTML verbatim, no sanitize/convert.
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
        """Default blanks current.description_html — body still travels (``@all`` needed
        for customs); tool strips it to spare LLM context.
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
        # Bare @all so inline customs surface; narrowing would drop them.
        assert params["fields[workitems]"] == "@all"

    async def test_current_carries_custom_fields_from_post_patch_get(
        self, mock_ctx: MagicMock, mock_client: AsyncMock
    ) -> None:
        # Inlined customs from the post-PATCH GET surface on current.custom_fields.
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
        # Customs ride top-level in attributes; a customFields container is dropped.
        mock_client.patch.return_value = {}
        rich = {"type": "text/html", "value": "<p>note</p>"}
        _cache_mod.store_work_item_custom_keys(
            "MyProj", "task", frozenset({"riskLevel", "reviewerNote"})
        )
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
        # changes mirrors what was sent so callers can confirm intent.
        mock_client.patch.return_value = {}
        _cache_mod.store_work_item_custom_keys(
            "MyProj", "task", frozenset({"riskLevel"})
        )
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
        # Post-call mutation of the caller's dict must not bleed into changes.
        mock_client.patch.return_value = {}
        _cache_mod.store_work_item_custom_keys(
            "MyProj", "task", frozenset({"reviewerNote", "riskLevel"})
        )
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
        # Dry-run echoes merged standard+custom attrs.
        _cache_mod.store_work_item_custom_keys(
            "MyProj", "task", frozenset({"riskLevel"})
        )
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
        # Read custom_fields must write back unchanged: symmetric read/write shapes.
        read_customs: dict[str, object] = {
            "riskLevel": "high",
            "effortHours": 8.0,
            "reviewerNote": {"type": "text/html", "value": "<p>x</p>"},
        }
        mock_client.patch.return_value = {}
        _cache_mod.store_work_item_custom_keys(
            "MyProj", "task", frozenset({"riskLevel", "effortHours", "reviewerNote"})
        )
        mock_client.get.return_value = _make_get_response(custom_fields=read_customs)

        result = await _call_update(mock_ctx, custom_fields=read_customs)

        _, kwargs = mock_client.patch.call_args
        attributes = kwargs["json"]["data"]["attributes"]
        for key, value in read_customs.items():
            assert attributes[key] == value
        assert result.updated is True

    async def test_workflow_action_appended_as_query_param(
        self, mock_ctx: MagicMock, mock_client: AsyncMock
    ) -> None:
        # workflow_action needs a paired body field, so add a title.
        mock_client.patch.return_value = {}
        mock_client.get.return_value = _make_get_response()

        await _call_update(mock_ctx, workflow_action="close", title="t")

        patch_path = mock_client.patch.call_args.args[0]
        assert patch_path == "/projects/MyProj/workitems/MCPT-1?workflowAction=close"
        # Follow-up GET drops the query to read the canonical detail.
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
        """Core round-trip guarantee: description_html PATCHed verbatim — no sanitize,
        no markdownify.
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
        """max_length=MAX_BODY_HTML_LEN caps runaway HTML.

        Re-proven here so a docstring rewrite cannot silently drop it.
        """
        adapter = self._adapter_for("description_html")
        assert adapter.validate_python("<p>ok</p>") == "<p>ok</p>"
        # 2 MiB + 1 char is rejected.
        with pytest.raises(ValidationError):
            adapter.validate_python("x" * (2_000_000 + 1))


class TestUpdateWorkItemDocstringGuidance:
    """Lock the read-before-update steer into the public docstring."""

    def test_docstring_directs_get_before_update(self) -> None:
        document = update_work_item.__doc__ or ""
        assert "get_work_item" in document, (
            "update_work_item docstring must direct callers to read the "
            "work item before patching it"
        )
        assert "BEFORE" in document, (
            "update_work_item docstring must state the read happens BEFORE the update"
        )


class TestEnumGuardCreateWorkItem:
    """Integration: ``create_work_items`` rejects ghost enum ids before POST."""

    async def test_unlisted_severity_raises_before_post(
        self,
        mock_ctx: MagicMock,
        mock_client: AsyncMock,
        reset_enum_guard_caches: None,
    ) -> None:
        # task passes the type-axis check; severity then trips.
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
        # Per-item guard runs before any POST, so a bad later item aborts the batch.
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
        # One response shape serves every guard probe plus the create.
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

    async def test_custom_fields_on_create_pass_when_in_type_sample(
        self,
        mock_ctx: MagicMock,
        mock_client: AsyncMock,
        reset_enum_guard_caches: None,
    ) -> None:
        # GETs: type options, the MIN-per-key type sample, then the enum-value
        # probe for the key (404 = not an enum field, defers).
        mock_client.get.side_effect = [
            _enum_get_response(["task"]),
            _wi_sample_response({"risk_score": 1}),
            PolarionNotFoundError("not an Enumeration field", status_code=404),
        ]
        mock_client.post.return_value = {"data": [{"id": "MyProj/MCPT-1"}]}

        result = await _call_create_wi(mock_ctx, custom_fields={"risk_score": 5})

        assert result.created is True  # type: ignore[attr-defined]
        mock_client.post.assert_awaited_once()

    async def test_custom_field_enum_value_rejected_on_create(
        self,
        mock_ctx: MagicMock,
        mock_client: AsyncMock,
        reset_enum_guard_caches: None,
    ) -> None:
        # GETs: type options, type sample (knows asil), then the enum-value
        # probe -- '9' is not among the field's options.
        mock_client.get.side_effect = [
            _enum_get_response(["task"]),
            _wi_sample_response({"asil": "1"}),
            _enum_get_response(["1", "2", "3", "4"]),
        ]

        with pytest.raises(ValueError, match=r"'asil'.*'9'"):
            await _call_create_wi(mock_ctx, custom_fields={"asil": "9"})
        mock_client.post.assert_not_called()

    async def test_custom_fields_on_create_reject_ghost_key(
        self,
        mock_ctx: MagicMock,
        mock_client: AsyncMock,
        reset_enum_guard_caches: None,
    ) -> None:
        # Sample knows only risk_score; the ghost key is rejected after a retry.
        mock_client.get.side_effect = [
            _enum_get_response(["task"]),
            _wi_sample_response({"risk_score": 1}),
            _wi_sample_response({"risk_score": 1}),
        ]

        with pytest.raises(ValueError, match="newGhostField"):
            await _call_create_wi(mock_ctx, custom_fields={"newGhostField": "x"})
        mock_client.post.assert_not_called()

    async def test_custom_fields_on_create_fail_closed_on_empty_sample(
        self,
        mock_ctx: MagicMock,
        mock_client: AsyncMock,
        reset_enum_guard_caches: None,
    ) -> None:
        # No item of this type populates any custom field -> can't infer schema.
        mock_client.get.side_effect = [
            _enum_get_response(["task"]),
            {"data": []},
            {"data": []},
        ]

        with pytest.raises(RuntimeError, match="Refusing the write"):
            await _call_create_wi(mock_ctx, custom_fields={"risk_score": 5})
        mock_client.post.assert_not_called()


class TestEnumGuardUpdateWorkItem:
    """Integration: ``update_work_item`` pre-fetches type then guards."""

    async def test_unlisted_priority_raises_after_prefetch(
        self,
        mock_ctx: MagicMock,
        mock_client: AsyncMock,
        reset_enum_guard_caches: None,
    ) -> None:
        # GETs: work-item pre-fetch, then priority options.
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
        # GETs: prefetch (for type), then the type sample (+ bypass-retry sample).
        # The sample knows only risk_score, so release_train_id is rejected.
        mock_client.get.side_effect = [
            _make_get_response(),
            _wi_sample_response({"risk_score": 5}),
            _wi_sample_response({"risk_score": 5}),
        ]
        with pytest.raises(ValueError, match="release_train_id"):
            await _call_update(
                mock_ctx,
                custom_fields={"release_train_id": "RT-42"},
            )
        mock_client.patch.assert_not_called()

    async def test_type_key_unset_on_item_passes_via_sample(
        self,
        mock_ctx: MagicMock,
        mock_client: AsyncMock,
        reset_enum_guard_caches: None,
    ) -> None:
        # Regression: a custom key valid for the type but unset on THIS item was
        # falsely rejected by the old single-item prime. The type sample knows it.
        mock_client.get.side_effect = [
            _make_get_response(),  # prefetch: no customs on the edited item
            _wi_sample_response({"release_train_id": "RT-1"}),  # type sample knows it
            PolarionNotFoundError("not an Enumeration field", status_code=404),
        ]
        result = await _call_update(
            mock_ctx, custom_fields={"release_train_id": "RT-42"}, dry_run=True
        )

        assert result.dry_run is True  # type: ignore[attr-defined]
        mock_client.patch.assert_not_called()

    async def test_custom_field_enum_value_rejected_on_update(
        self,
        mock_ctx: MagicMock,
        mock_client: AsyncMock,
        reset_enum_guard_caches: None,
    ) -> None:
        # GETs: prefetch (for type), type sample (knows asil), enum probe.
        mock_client.get.side_effect = [
            _make_get_response(),
            _wi_sample_response({"asil": "1"}),
            _enum_get_response(["1", "2", "3", "4"]),
        ]

        with pytest.raises(ValueError, match=r"'asil'.*'9'"):
            await _call_update(mock_ctx, custom_fields={"asil": "9"})
        mock_client.patch.assert_not_called()

    async def test_custom_fields_scoped_to_change_type_to(
        self,
        mock_ctx: MagicMock,
        mock_client: AsyncMock,
        reset_enum_guard_caches: None,
    ) -> None:
        # change_type_to retypes the item in the same PATCH, so custom_fields are
        # validated against the NEW type's schema, not the current ("task").
        # GETs: prefetch, type options (~ axis for change_type_to), custom
        # sample, then the enum-value probe for the key (404 = not enum).
        mock_client.get.side_effect = [
            _make_get_response(),
            _enum_get_response(["requirement"]),
            _wi_sample_response({"release_train_id": "RT-1"}),
            PolarionNotFoundError("not an Enumeration field", status_code=404),
        ]
        result = await _call_update(
            mock_ctx,
            change_type_to="requirement",
            custom_fields={"release_train_id": "RT-42"},
            dry_run=True,
        )
        assert result.dry_run is True  # type: ignore[attr-defined]
        sample_calls = [
            c
            for c in mock_client.get.call_args_list
            if "SQL:" in str(c.kwargs.get("params", {}).get("query", ""))
        ]
        assert sample_calls, "guard must sample the type schema via SQL"
        query = sample_calls[0].kwargs["params"]["query"]
        assert "c_type = 'requirement'" in query
        assert "c_type = 'task'" not in query

    async def test_unlisted_resolution_raises_after_prefetch(
        self,
        mock_ctx: MagicMock,
        mock_client: AsyncMock,
        reset_enum_guard_caches: None,
    ) -> None:
        # resolution is ghost-prone; an unlisted id must be rejected.
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
        # change_type_to scopes the status lookup to the target type.
        # GETs: work-item pre-fetch, type options (~ axis), status options (target).
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
        """include_description_html=False blanks the field — body still travels
        (``@all`` for customs); passed explicitly since direct calls bypass defaults.
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
        """Polarion spans / data-* attributes survive on read.

        Round-trip guarantee for update_work_item(description_html=).
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
                    # Standard attrs: excluded from custom_fields.
                    "title": "work item with customs",
                    "type": "softwarerequirement",
                    "status": "approved",
                    "priority": "50.0",
                    # Inline customs: top-level, not nested.
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

        # Raw passthrough: rich-text dicts not converted to Markdown.
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
        """Read-only contract: WorkItemRead.description is NOT round-trip shape —
        feeding it back loses the polarion-rte-link span.
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
        # Customs stay raw so the dict round-trips through update_work_item.
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
