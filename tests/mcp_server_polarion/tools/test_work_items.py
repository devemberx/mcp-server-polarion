"""Work item query/create/update tool tests."""

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
    Hyperlink,
    PaginatedResult,
    WorkItemCreateSpec,
    WorkItemDetail,
    WorkItemRead,
    WorkItemsCreateResult,
    WorkItemsUpdateResult,
    WorkItemUpdateSpec,
)
from mcp_server_polarion.tools._shared import cache as _cache_mod
from mcp_server_polarion.tools._shared.cache import store_work_item_custom_keys
from mcp_server_polarion.tools._shared.fields import MAX_BULK_ITEMS
from mcp_server_polarion.tools.work_items import (
    _build_create_work_items_payload,
    _build_update_work_item_resource,
    _build_update_work_items_payload,
    _build_work_item_resource,
    create_work_items,
    get_work_item,
    list_work_items,
    read_work_item,
    update_work_items,
)
from tests.mcp_server_polarion.tools._shared.guard._builders import (
    attachments_response,
)


def _project_enum_get_response(enum_name: str, ids: list[str]) -> dict[str, object]:
    """Single-enumeration response: ``data`` = dict, options nested under."""
    return {
        "data": {
            "type": "enumerations",
            "id": enum_name,
            "attributes": {"options": [{"id": i, "name": i} for i in ids]},
        }
    }


def _spec(**overrides: object) -> WorkItemUpdateSpec:
    """Update spec with default id; tests override as needed."""
    fields: dict[str, object] = {"work_item_id": "MCPT-1"}
    fields.update(overrides)
    return WorkItemUpdateSpec(**fields)  # type: ignore[arg-type]


async def _call_update(
    mock_ctx: MagicMock, **overrides: object
) -> WorkItemsUpdateResult:
    """Call update_work_items, all params explicit.

    Field(...) defaults stay FieldInfo outside FastMCP — must pass every
    param; tests override as needed.
    """
    defaults: dict[str, object] = {
        "project_id": "MyProj",
        "items": [_spec(title="t")],
        "workflow_action": None,
        "change_type_to": None,
        "dry_run": False,
    }
    defaults.update(overrides)
    return await update_work_items(mock_ctx, **defaults)


def _existence_response(*pairs: tuple[str, str]) -> dict[str, object]:
    """Reply to batched ``id:(...)`` existence/type query."""
    return {
        "data": [
            {
                "type": "workitems",
                "id": f"MyProj/{short_id}",
                "attributes": {"type": type_id},
            }
            for short_id, type_id in pairs
        ]
    }


def _enum_get_response(ids: list[str]) -> dict[str, object]:
    """``getAvailableOptions`` reply for guard tests."""
    return {
        "data": [{"id": i, "name": i} for i in ids],
        "meta": {"totalCount": len(ids)},
    }


def _wi_sample_response(*custom_dicts: dict[str, object]) -> dict[str, object]:
    """MIN-per-key list reply: representative items, inline customs."""
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
    """Invoke ``create_work_items`` with single-spec default batch.

    ``project_id``/``dry_run`` = top-level tool params; other overrides
    fold into one ``WorkItemCreateSpec``.
    """
    project_id = cast(str, overrides.pop("project_id", "MyProj"))
    dry_run = cast(bool, overrides.pop("dry_run", False))
    spec_fields: dict[str, object] = {"title": "t", "type": "task"}
    spec_fields.update(overrides)
    spec = WorkItemCreateSpec(**spec_fields)  # type: ignore[arg-type]
    return await create_work_items(
        mock_ctx, project_id=project_id, items=[spec], dry_run=dry_run
    )


@pytest.fixture
def reset_enum_guard_caches() -> None:
    """Drop guard caches between tests — each scenario start cold."""
    _cache_mod._enum_option_cache.clear()
    _cache_mod._project_enum_cache.clear()
    _cache_mod._work_item_custom_key_cache.clear()
    _cache_mod._document_type_custom_key_cache.clear()


class TestBuildWorkItemResource:
    """Private ``_build_work_item_resource`` helper (one resource)."""

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
        # Customs land flat; Polarion drop customFields container.
        assert "customFields" not in attributes

    def test_custom_fields_collision_with_standard_attr_raises(self) -> None:
        # Custom key match standard attr = silent shadow.
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
    """Bulk ``_build_create_work_items_payload`` wrapper."""

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
        # zip(strict=True) guard spec/html pairing.
        with pytest.raises(ValueError):
            _build_create_work_items_payload(
                specs=[WorkItemCreateSpec(title="a", type="task")],
                descriptions_html=[],
            )


class TestCreateWorkItemsDryRun:
    """``create_work_items`` with ``dry_run=True``."""

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
        # Plain dict, no Pydantic leak.
        assert isinstance(result.payload_preview, dict)
        item = cast(list[dict[str, object]], result.payload_preview["data"])[0]
        attributes = cast(dict[str, object], item["attributes"])
        assert attributes == {"title": "Dry test", "type": "task"}


class TestCreateWorkItemsHyperlinkRoleGuard:
    """``create_work_items`` validate each hyperlink role before write."""

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
    """Successful ``create_work_items`` call."""

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
        # Single POST create whole batch.
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
        # Safe https link survive sanitize.
        assert 'href="https://example.com"' in desc["value"]

    async def test_description_strips_dangerous_link_schemes(
        self, mock_ctx: MagicMock, mock_client: AsyncMock
    ) -> None:
        """No javascript: anchor reach payload.

        markdown-it leave it unrendered; sanitize_html strip it as
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

    async def test_markdown_table_and_caption_polarionified(
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
                    description=(
                        "| a | b |\n| --- | --- |\n| 1 | 2 |\n\nTable: 캡션\n"
                    ),
                )
            ],
            dry_run=False,
        )

        _, kwargs = mock_client.post.call_args
        desc_html = kwargs["json"]["data"][0]["attributes"]["description"]["value"]
        assert 'class="polarion-Document-table"' in desc_html
        assert 'data-sequence="Table"' in desc_html
        assert "캡션" in desc_html

    async def test_rich_text_custom_field_value_not_polarionified(
        self, mock_ctx: MagicMock, mock_client: AsyncMock
    ) -> None:
        # polarionify apply to description only, never custom_fields.
        store_work_item_custom_keys(
            "MyProj", "task", frozenset({"verification_criteria"})
        )
        mock_client.post.return_value = {
            "data": [{"type": "workitems", "id": "MyProj/MCPT-1"}]
        }
        rich = {"type": "text/html", "value": "<table><tr><td>x</td></tr></table>"}

        await create_work_items(
            mock_ctx,
            project_id="MyProj",
            items=[
                WorkItemCreateSpec(
                    title="t",
                    type="task",
                    custom_fields={"verification_criteria": rich},
                )
            ],
            dry_run=False,
        )

        _, kwargs = mock_client.post.call_args
        attributes = kwargs["json"]["data"][0]["attributes"]
        assert attributes["verification_criteria"] == rich


class TestCreateWorkItemsErrorMapping:
    """Domain exceptions mapped at tool layer."""

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
    """Unexpected / partial 2xx response shapes from Polarion."""

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
    """Constraints on ``items`` + ``WorkItemCreateSpec`` — direct calls bypass
    JSON Schema gate; collection bounds proven via ``TypeAdapter`` rebuild.
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
        """``max_length`` on spec defend against runaway Markdown."""
        WorkItemCreateSpec(title="t", type="task", description="hello")
        with pytest.raises(ValidationError):
            WorkItemCreateSpec(
                title="t", type="task", description="x" * (2_000_000 + 1)
            )


class TestBuildUpdateWorkItemsPayload:
    """Private bulk-update payload builders."""

    def test_minimal_resource_with_only_title(self) -> None:
        resource = _build_update_work_item_resource(
            project_id="MyProj", spec=_spec(title="New title")
        )

        assert resource == {
            "type": "workitems",
            "id": "MyProj/MCPT-1",
            "attributes": {"title": "New title"},
        }

    def test_payload_wraps_data_as_list_in_input_order(self) -> None:
        payload = _build_update_work_items_payload(
            project_id="MyProj",
            specs=[_spec(title="a"), _spec(work_item_id="MCPT-2", title="b")],
        )

        data = cast(list[dict[str, object]], payload["data"])
        assert [item["id"] for item in data] == ["MyProj/MCPT-1", "MyProj/MCPT-2"]

    def test_includes_description_block(self) -> None:
        resource = _build_update_work_item_resource(
            project_id="MyProj", spec=_spec(description_html="<p>hi</p>")
        )

        attributes = cast(dict[str, object], resource["attributes"])
        assert attributes["description"] == {
            "type": "text/html",
            "value": "<p>hi</p>",
        }

    def test_assignee_ids_become_to_many_users_relationship(self) -> None:
        resource = _build_update_work_item_resource(
            project_id="MyProj", spec=_spec(assignee_ids=["alice", "bob"])
        )

        # Relationship-only change: no attributes block.
        assert "attributes" not in resource
        relationships = cast(dict[str, object], resource["relationships"])
        assert relationships["assignee"] == {
            "data": [
                {"type": "users", "id": "alice"},
                {"type": "users", "id": "bob"},
            ]
        }

    def test_hyperlinks_serialise_role_title_uri(self) -> None:
        resource = _build_update_work_item_resource(
            project_id="MyProj",
            spec=_spec(
                hyperlinks=[
                    Hyperlink(role="ref_ext", title="Spec", uri="https://example.com")
                ]
            ),
        )

        attributes = cast(dict[str, object], resource["attributes"])
        assert attributes["hyperlinks"] == [
            {"role": "ref_ext", "title": "Spec", "uri": "https://example.com"},
        ]

    def test_all_optional_attrs_included_when_set(self) -> None:
        resource = _build_update_work_item_resource(
            project_id="MyProj",
            spec=_spec(
                title="t",
                status="open",
                priority="50.0",
                severity="major",
                due_date="2026-05-31",
                initial_estimate="5 1/2d",
                resolution="fixed",
            ),
        )

        attributes = cast(dict[str, object], resource["attributes"])
        assert attributes == {
            "title": "t",
            "status": "open",
            "priority": "50.0",
            "severity": "major",
            "dueDate": "2026-05-31",
            "initialEstimate": "5 1/2d",
            "resolution": "fixed",
        }

    def test_custom_fields_inlined_in_attributes(self) -> None:
        rich = {"type": "text/html", "value": "<p>note</p>"}
        resource = _build_update_work_item_resource(
            project_id="MyProj",
            spec=_spec(custom_fields={"riskLevel": "low", "reviewerNote": rich}),
        )

        attributes = cast(dict[str, object], resource["attributes"])
        assert attributes == {"riskLevel": "low", "reviewerNote": rich}

    def test_none_valued_custom_entries_dropped(self) -> None:
        # Polarion read explicit None as "clear default" — write contract
        # skip them; spec validator already ensure >=1 survive.
        resource = _build_update_work_item_resource(
            project_id="MyProj",
            spec=_spec(custom_fields={"cleared": None, "kept": "v"}),
        )

        attributes = cast(dict[str, object], resource["attributes"])
        assert attributes == {"kept": "v"}

    def test_custom_fields_collision_raises(self) -> None:
        with pytest.raises(ValueError, match="custom_fields keys collide"):
            _build_update_work_item_resource(
                project_id="MyProj",
                spec=_spec(custom_fields={"status": "open"}),
            )


class TestUpdateWorkItemsValidation:
    """Batch-level validation before Polarion round-trip."""

    async def test_duplicate_ids_rejected_before_any_request(
        self, mock_ctx: MagicMock, mock_client: AsyncMock
    ) -> None:
        with pytest.raises(ValueError, match="Duplicate work_item_id"):
            await _call_update(
                mock_ctx,
                items=[_spec(title="a"), _spec(title="b")],
            )
        mock_client.get.assert_not_called()
        mock_client.patch.assert_not_called()

    async def test_lucene_unsafe_id_rejected_before_any_request(
        self, mock_ctx: MagicMock, mock_client: AsyncMock
    ) -> None:
        # Project-qualified ids ('P/MCPT-1') rejected too: '/' outside
        # charset embedded in id:(...) existence query.
        with pytest.raises(ValueError, match="outside"):
            await _call_update(
                mock_ctx, items=[_spec(work_item_id="MyProj/MCPT-1", title="t")]
            )
        mock_client.get.assert_not_called()

    async def test_missing_ids_named_before_patch(
        self, mock_ctx: MagicMock, mock_client: AsyncMock
    ) -> None:
        mock_client.get.return_value = _existence_response(("MCPT-1", "task"))

        with pytest.raises(ValueError, match="MCPT-9") as exc:
            await _call_update(
                mock_ctx,
                items=[
                    _spec(title="a"),
                    _spec(work_item_id="MCPT-9", title="b"),
                ],
            )

        assert "list_work_items" in str(exc.value)
        mock_client.patch.assert_not_called()

    async def test_missing_ids_checked_on_dry_run_too(
        self, mock_ctx: MagicMock, mock_client: AsyncMock
    ) -> None:
        # Preview must raise same errors as real write.
        mock_client.get.return_value = _existence_response()

        with pytest.raises(ValueError, match="MCPT-1"):
            await _call_update(mock_ctx, items=[_spec(title="t")], dry_run=True)

    async def test_collision_raises_before_any_request(
        self, mock_ctx: MagicMock, mock_client: AsyncMock
    ) -> None:
        # Custom key match standard attr = shadow; payload built before
        # guard round-trip — fail fast.
        with pytest.raises(ValueError, match="custom_fields keys collide"):
            await _call_update(
                mock_ctx,
                items=[_spec(title="x", custom_fields={"title": "y"})],
            )
        mock_client.get.assert_not_called()
        mock_client.patch.assert_not_called()


class TestUpdateWorkItemsHyperlinkRoleGuard:
    """``update_work_items`` validate hyperlink roles before PATCH."""

    async def test_unknown_hyperlink_role_raises_without_patch(
        self, mock_ctx: MagicMock, mock_client: AsyncMock
    ) -> None:
        mock_client.get.side_effect = [
            _existence_response(("MCPT-1", "task")),
            _project_enum_get_response("hyperlink-role", ["ref_int", "ref_ext"]),
        ]

        with pytest.raises(ValueError, match="ghost") as exc:
            await _call_update(
                mock_ctx,
                items=[
                    _spec(hyperlinks=[Hyperlink(role="ghost", uri="https://e.com")])
                ],
                dry_run=True,
            )

        assert "ref_ext" in str(exc.value)
        mock_client.patch.assert_not_called()

    async def test_bad_role_names_offending_item_via_cache(
        self, mock_ctx: MagicMock, mock_client: AsyncMock
    ) -> None:
        # Item 0 role pass; item 1 reuse cached options (no second enum
        # GET), rejected with batch position and id.
        mock_client.get.side_effect = [
            _existence_response(("MCPT-1", "task"), ("MCPT-2", "task")),
            _project_enum_get_response("hyperlink-role", ["ref_int", "ref_ext"]),
        ]

        with pytest.raises(ValueError, match=r"items\[1\] \('MCPT-2'\)"):
            await _call_update(
                mock_ctx,
                items=[
                    _spec(hyperlinks=[Hyperlink(role="ref_ext", uri="https://a.com")]),
                    _spec(
                        work_item_id="MCPT-2",
                        hyperlinks=[Hyperlink(role="ghost", uri="https://b.com")],
                    ),
                ],
                dry_run=True,
            )

        assert mock_client.get.await_count == 2
        mock_client.patch.assert_not_called()


class TestUpdateWorkItemsDryRun:
    """``update_work_items`` with ``dry_run=True``."""

    async def test_dry_run_skips_patch_but_validates_existence(
        self, mock_ctx: MagicMock, mock_client: AsyncMock
    ) -> None:
        mock_client.get.return_value = _existence_response(("MCPT-1", "task"))

        result = await _call_update(
            mock_ctx, items=[_spec(title="New title")], dry_run=True
        )

        mock_client.patch.assert_not_called()
        mock_client.get.assert_awaited_once()
        assert isinstance(result, WorkItemsUpdateResult)
        assert result.updated is False
        assert result.dry_run is True
        assert result.work_item_ids == []
        assert result.payload_preview is not None
        data = cast(list[dict[str, object]], result.payload_preview["data"])
        assert data[0]["id"] == "MyProj/MCPT-1"
        attributes = cast(dict[str, object], data[0]["attributes"])
        assert attributes == {"title": "New title"}

    async def test_dry_run_preview_carries_query_params(
        self, mock_ctx: MagicMock, mock_client: AsyncMock
    ) -> None:
        mock_client.get.return_value = _existence_response(("MCPT-1", "task"))

        result = await _call_update(
            mock_ctx,
            items=[_spec(title="t")],
            workflow_action="close",
            dry_run=True,
        )

        assert result.payload_preview is not None
        assert result.payload_preview["query_params"] == {"workflowAction": "close"}

    async def test_dry_run_preview_keeps_raw_html_verbatim(
        self, mock_ctx: MagicMock, mock_client: AsyncMock
    ) -> None:
        # Round-trip guarantee: no sanitize, no markdownify.
        mock_client.get.return_value = _existence_response(("MCPT-1", "task"))
        raw = (
            '<p>See <span class="polarion-rte-link" '
            'data-item-id="MCPT-7" data-scope="MyProj">MCPT-7</span></p>'
        )

        result = await _call_update(
            mock_ctx, items=[_spec(description_html=raw)], dry_run=True
        )

        assert result.payload_preview is not None
        data = cast(list[dict[str, object]], result.payload_preview["data"])
        attributes = cast(dict[str, object], data[0]["attributes"])
        assert attributes["description"] == {"type": "text/html", "value": raw}


class TestUpdateWorkItemsHappyPath:
    """Successful ``update_work_items`` call."""

    async def test_returns_ids_in_input_order(
        self, mock_ctx: MagicMock, mock_client: AsyncMock
    ) -> None:
        mock_client.get.return_value = _existence_response(
            ("MCPT-1", "task"), ("MCPT-2", "task")
        )
        mock_client.patch.return_value = {}

        result = await _call_update(
            mock_ctx,
            items=[_spec(title="a"), _spec(work_item_id="MCPT-2", title="b")],
        )

        assert isinstance(result, WorkItemsUpdateResult)
        assert result.updated is True
        assert result.dry_run is False
        assert result.work_item_ids == ["MCPT-1", "MCPT-2"]
        assert result.payload_preview is None

    async def test_patch_called_with_collection_path_and_list_body(
        self, mock_ctx: MagicMock, mock_client: AsyncMock
    ) -> None:
        mock_client.get.return_value = _existence_response(("MCPT-42", "task"))
        mock_client.patch.return_value = {}

        await _call_update(
            mock_ctx,
            items=[_spec(work_item_id="MCPT-42", title="t", assignee_ids=["alice"])],
        )

        args, kwargs = mock_client.patch.call_args
        assert args == ("/projects/MyProj/workitems",)
        data = kwargs["json"]["data"]
        assert isinstance(data, list)
        item = data[0]
        assert item["type"] == "workitems"
        assert item["id"] == "MyProj/MCPT-42"
        assert item["attributes"]["title"] == "t"
        assert item["relationships"]["assignee"]["data"] == [
            {"type": "users", "id": "alice"}
        ]

    async def test_no_followup_get_after_patch(
        self, mock_ctx: MagicMock, mock_client: AsyncMock
    ) -> None:
        # ids-only result: per-item re-GET gone (50 items = 50 extra
        # requests at 3 req/s cap).
        mock_client.get.return_value = _existence_response(("MCPT-1", "task"))
        mock_client.patch.return_value = {}

        await _call_update(mock_ctx, items=[_spec(title="t")])

        assert mock_client.get.await_count == 1

    async def test_workflow_action_and_change_type_to_query_params(
        self, mock_ctx: MagicMock, mock_client: AsyncMock
    ) -> None:
        mock_client.get.side_effect = [
            _existence_response(("MCPT-1", "task")),
            _enum_get_response(["defect"]),
        ]
        mock_client.patch.return_value = {}

        await _call_update(
            mock_ctx,
            items=[_spec(title="t")],
            workflow_action="close",
            change_type_to="defect",
        )

        patch_path = mock_client.patch.call_args.args[0]
        assert patch_path == (
            "/projects/MyProj/workitems?workflowAction=close&changeTypeTo=defect"
        )

    async def test_description_html_is_sent_verbatim(
        self, mock_ctx: MagicMock, mock_client: AsyncMock
    ) -> None:
        mock_client.get.return_value = _existence_response(("MCPT-1", "task"))
        mock_client.patch.return_value = {}
        raw = (
            '<p>See <span class="polarion-rte-link" '
            'data-item-id="MCPT-7" data-scope="MyProj">MCPT-7</span></p>'
        )

        await _call_update(mock_ctx, items=[_spec(description_html=raw)])

        data = mock_client.patch.call_args.kwargs["json"]["data"]
        assert data[0]["attributes"]["description"] == {
            "type": "text/html",
            "value": raw,
        }

    async def test_custom_fields_inlined_into_patch_body(
        self, mock_ctx: MagicMock, mock_client: AsyncMock
    ) -> None:
        # Customs ride top-level in attributes; key pre-cached, enum-value
        # probe 404 (not enum field, defer to Polarion).
        _cache_mod.store_work_item_custom_keys(
            "MyProj", "task", frozenset({"riskLevel"})
        )
        mock_client.get.side_effect = [
            _existence_response(("MCPT-1", "task")),
            PolarionNotFoundError("not an Enumeration field", status_code=404),
        ]
        mock_client.patch.return_value = {}

        await _call_update(mock_ctx, items=[_spec(custom_fields={"riskLevel": "low"})])

        data = mock_client.patch.call_args.kwargs["json"]["data"]
        attributes = data[0]["attributes"]
        assert attributes["riskLevel"] == "low"
        assert "customFields" not in attributes

    async def test_path_url_encodes_special_chars(
        self, mock_ctx: MagicMock, mock_client: AsyncMock
    ) -> None:
        mock_client.get.return_value = _existence_response(("MCPT-1", "task"))
        mock_client.patch.return_value = {}

        await _call_update(mock_ctx, project_id="My Proj", items=[_spec(title="t")])

        assert mock_client.patch.call_args.args[0] == "/projects/My%20Proj/workitems"


class TestUpdateWorkItemsErrorMapping:
    """Domain exceptions mapped at tool layer."""

    async def test_patch_401_raises_permission_error(
        self, mock_ctx: MagicMock, mock_client: AsyncMock
    ) -> None:
        mock_client.get.return_value = _existence_response(("MCPT-1", "task"))
        mock_client.patch.side_effect = PolarionAuthError("auth", status_code=401)

        with pytest.raises(PermissionError):
            await _call_update(mock_ctx, items=[_spec(title="t")])

    async def test_patch_404_raises_value_error(
        self, mock_ctx: MagicMock, mock_client: AsyncMock
    ) -> None:
        # Race fallback: existence passed above, then batch changed.
        mock_client.get.return_value = _existence_response(("MCPT-1", "task"))
        mock_client.patch.side_effect = PolarionNotFoundError(
            "not found", status_code=404
        )

        with pytest.raises(ValueError, match="list_work_items"):
            await _call_update(mock_ctx, items=[_spec(title="t")])

    async def test_patch_other_error_raises_runtime_error(
        self, mock_ctx: MagicMock, mock_client: AsyncMock
    ) -> None:
        mock_client.get.return_value = _existence_response(("MCPT-1", "task"))
        mock_client.patch.side_effect = PolarionError("boom", status_code=500)

        with pytest.raises(RuntimeError, match="boom"):
            await _call_update(mock_ctx, items=[_spec(title="t")])


class TestUpdateWorkItemsFieldValidation:
    """Field constraints on tool parameters."""

    @staticmethod
    def _adapter_for(param_name: str) -> TypeAdapter[object]:
        hints = get_type_hints(update_work_items)
        sig = inspect.signature(update_work_items)
        field_info = sig.parameters[param_name].default
        return TypeAdapter(Annotated[hints[param_name], field_info])

    def test_project_id_rejects_empty_string(self) -> None:
        with pytest.raises(ValidationError):
            self._adapter_for("project_id").validate_python("")

    def test_project_id_accepts_non_empty(self) -> None:
        assert self._adapter_for("project_id").validate_python("p") == "p"

    def test_items_rejects_empty_list(self) -> None:
        with pytest.raises(ValidationError):
            self._adapter_for("items").validate_python([])

    def test_items_rejects_more_than_max_bulk(self) -> None:
        items = [
            {"work_item_id": f"MCPT-{i}", "title": "t"}
            for i in range(MAX_BULK_ITEMS + 1)
        ]
        with pytest.raises(ValidationError):
            self._adapter_for("items").validate_python(items)

    def test_items_accepts_max_bulk(self) -> None:
        items = [
            {"work_item_id": f"MCPT-{i}", "title": "t"} for i in range(MAX_BULK_ITEMS)
        ]
        validated = self._adapter_for("items").validate_python(items)
        assert len(cast(list[object], validated)) == MAX_BULK_ITEMS

    def test_typo_key_in_one_item_rejects_batch_naming_index(self) -> None:
        # extra='forbid' smoke: without it 'descrition' persist verbatim in
        # Polarion as ghost custom field; batch rejected at parse, naming index.
        items: list[dict[str, object]] = [
            {"work_item_id": f"MCPT-{i}", "title": "t"} for i in range(1, 6)
        ]
        items[3] = {"work_item_id": "MCPT-4", "descrition": "<p>x</p>"}

        with pytest.raises(ValidationError, match=r"3\.descrition") as exc:
            self._adapter_for("items").validate_python(items)

        assert exc.value.errors()[0]["type"] == "extra_forbidden"


class TestUpdateWorkItemsDocstringGuidance:
    """Lock read-before-update + REPLACE steers into tool docs."""

    def test_docstring_directs_get_before_update(self) -> None:
        document = update_work_items.__doc__ or ""
        assert "get_work_item" in document, (
            "update_work_items docstring must direct callers to read each "
            "work item before patching it"
        )
        assert "BEFORE" in document, (
            "update_work_items docstring must state the read happens BEFORE the update"
        )

    def test_replace_steer_leads_docstring_and_items_field(self) -> None:
        # Per-item fields nest one deeper than old flat tool — REPLACE
        # steer must stay in highest-salience spots.
        document = update_work_items.__doc__ or ""
        first_paragraph = document.split("\n\n")[0]
        assert "REPLACE" in first_paragraph
        items_field = inspect.signature(update_work_items).parameters["items"].default
        assert "REPLACE" in (items_field.description or "")

    def test_table_steer_keeps_eval_proven_phrasing(self) -> None:
        # EFF-TABLE-RECIPE-SOURCED: reword "To add a table" to "For a new
        # table" drop get_html_recipes triggering 3/3 to 0/3 — task-verb
        # phrasing load-bearing, keep byte-close.
        document = " ".join((update_work_items.__doc__ or "").split())
        assert "To add a table" in document
        assert "call get_html_recipes first" in document


class TestEnumGuardCreateWorkItem:
    """Integration: ``create_work_items`` reject ghost enum ids before POST."""

    async def test_unlisted_severity_raises_before_post(
        self,
        mock_ctx: MagicMock,
        mock_client: AsyncMock,
        reset_enum_guard_caches: None,
    ) -> None:
        # task pass type-axis check; severity then trip.
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
        # Per-item guard run before POST — bad later item abort batch.
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
        # One response shape serve every guard probe plus create.
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
        # GETs: type options, MIN-per-key type sample, enum-value probe
        # (404 = not enum field, defer).
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
        # GETs: type options, type sample (know asil), enum-value probe —
        # '9' not among field options.
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
        # Sample know only risk_score; ghost key rejected after retry.
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
        # No item of this type populate custom fields -> can't infer schema.
        mock_client.get.side_effect = [
            _enum_get_response(["task"]),
            {"data": []},
            {"data": []},
        ]

        with pytest.raises(RuntimeError, match="Refusing the write"):
            await _call_create_wi(mock_ctx, custom_fields={"risk_score": 5})
        mock_client.post.assert_not_called()


class TestEnumGuardUpdateWorkItems:
    """Integration: ``update_work_items`` resolve types then guard per item."""

    async def test_unlisted_priority_raises_after_type_resolution(
        self,
        mock_ctx: MagicMock,
        mock_client: AsyncMock,
        reset_enum_guard_caches: None,
    ) -> None:
        # GETs: batched existence/type query, then priority options.
        mock_client.get.side_effect = [
            _existence_response(("MCPT-1", "task")),
            _enum_get_response(["90.0", "50.0", "10.0"]),
        ]

        with pytest.raises(ValueError, match="priority='999'"):
            await _call_update(mock_ctx, items=[_spec(priority="999")])

        mock_client.patch.assert_not_called()
        probes = [
            c
            for c in mock_client.get.call_args_list
            if "fields/priority/actions/getAvailableOptions" in c.args[0]
        ]
        # Enum options scoped to type resolved by batched query.
        assert probes[0].kwargs["params"]["type"] == "task"

    async def test_guard_error_names_offending_item(
        self,
        mock_ctx: MagicMock,
        mock_client: AsyncMock,
        reset_enum_guard_caches: None,
    ) -> None:
        # Item 1 pass; item 2 reuse cached options — rejected with batch
        # position and id.
        mock_client.get.side_effect = [
            _existence_response(("MCPT-1", "task"), ("MCPT-2", "task")),
            _enum_get_response(["50.0"]),
        ]

        with pytest.raises(ValueError, match=r"items\[1\] \('MCPT-2'\)"):
            await _call_update(
                mock_ctx,
                items=[
                    _spec(priority="50.0"),
                    _spec(work_item_id="MCPT-2", priority="999"),
                ],
            )

        mock_client.patch.assert_not_called()

    async def test_unknown_custom_field_key_raises(
        self,
        mock_ctx: MagicMock,
        mock_client: AsyncMock,
        reset_enum_guard_caches: None,
    ) -> None:
        # GETs: existence, then type sample (+ bypass-retry sample).
        mock_client.get.side_effect = [
            _existence_response(("MCPT-1", "task")),
            _wi_sample_response({"risk_score": 5}),
            _wi_sample_response({"risk_score": 5}),
        ]

        with pytest.raises(ValueError, match="release_train_id"):
            await _call_update(
                mock_ctx,
                items=[_spec(custom_fields={"release_train_id": "RT-42"})],
            )

        mock_client.patch.assert_not_called()

    async def test_type_key_unset_on_item_passes_via_sample(
        self,
        mock_ctx: MagicMock,
        mock_client: AsyncMock,
        reset_enum_guard_caches: None,
    ) -> None:
        # Regression: custom key valid for type but unset on THIS item must
        # pass — type sample know it even when item does not.
        mock_client.get.side_effect = [
            _existence_response(("MCPT-1", "task")),
            _wi_sample_response({"release_train_id": "RT-1"}),
            PolarionNotFoundError("not an Enumeration field", status_code=404),
        ]

        result = await _call_update(
            mock_ctx,
            items=[_spec(custom_fields={"release_train_id": "RT-42"})],
            dry_run=True,
        )

        assert result.dry_run is True
        mock_client.patch.assert_not_called()

    async def test_custom_field_enum_value_rejected(
        self,
        mock_ctx: MagicMock,
        mock_client: AsyncMock,
        reset_enum_guard_caches: None,
    ) -> None:
        # GETs: existence, type sample (know asil), enum probe — '9' not in options.
        mock_client.get.side_effect = [
            _existence_response(("MCPT-1", "task")),
            _wi_sample_response({"asil": "1"}),
            _enum_get_response(["1", "2", "3", "4"]),
        ]

        with pytest.raises(ValueError, match=r"'asil'.*'9'"):
            await _call_update(mock_ctx, items=[_spec(custom_fields={"asil": "9"})])

        mock_client.patch.assert_not_called()

    async def test_custom_fields_scoped_to_change_type_to(
        self,
        mock_ctx: MagicMock,
        mock_client: AsyncMock,
        reset_enum_guard_caches: None,
    ) -> None:
        # change_type_to retype items in same PATCH — custom_fields validated
        # against NEW type schema, not current.
        # GETs: existence, type options (change_type_to axis), custom sample,
        # enum-value probe (404 = not enum).
        mock_client.get.side_effect = [
            _existence_response(("MCPT-1", "task")),
            _enum_get_response(["requirement"]),
            _wi_sample_response({"release_train_id": "RT-1"}),
            PolarionNotFoundError("not an Enumeration field", status_code=404),
        ]

        result = await _call_update(
            mock_ctx,
            items=[_spec(custom_fields={"release_train_id": "RT-42"})],
            change_type_to="requirement",
            dry_run=True,
        )

        assert result.dry_run is True
        sample_calls = [
            c
            for c in mock_client.get.call_args_list
            if "SQL:" in str(c.kwargs.get("params", {}).get("query", ""))
        ]
        assert sample_calls, "guard must sample the type schema via SQL"
        query = sample_calls[0].kwargs["params"]["query"]
        assert "c_type = 'requirement'" in query
        assert "c_type = 'task'" not in query

    async def test_unlisted_resolution_raises(
        self,
        mock_ctx: MagicMock,
        mock_client: AsyncMock,
        reset_enum_guard_caches: None,
    ) -> None:
        mock_client.get.side_effect = [
            _existence_response(("MCPT-1", "task")),
            _enum_get_response(["done", "wontfix", "duplicate"]),
        ]

        with pytest.raises(ValueError, match="resolution='ghost_resolution'"):
            await _call_update(mock_ctx, items=[_spec(resolution="ghost_resolution")])

        mock_client.patch.assert_not_called()

    async def test_status_scoped_by_target_type_on_change_type_to(
        self,
        mock_ctx: MagicMock,
        mock_client: AsyncMock,
        reset_enum_guard_caches: None,
    ) -> None:
        # GETs: existence, type options (change_type_to axis), status options
        # scoped to target type.
        mock_client.get.side_effect = [
            _existence_response(("MCPT-1", "task")),
            _enum_get_response(["requirement"]),
            _enum_get_response(["draft", "approved"]),
        ]

        result = await _call_update(
            mock_ctx,
            items=[_spec(status="draft")],
            change_type_to="requirement",
            dry_run=True,
        )

        assert result.dry_run is True
        status_calls = [
            c
            for c in mock_client.get.call_args_list
            if "fields/status/actions/getAvailableOptions" in c.args[0]
        ]
        assert status_calls, "guard must probe status options"
        assert status_calls[0].kwargs["params"]["type"] == "requirement"

    async def test_heterogeneous_batch_probes_each_type(
        self,
        mock_ctx: MagicMock,
        mock_client: AsyncMock,
        reset_enum_guard_caches: None,
    ) -> None:
        # Two items, different types: status options probed once per
        # resolved type (option cache keyed by type).
        mock_client.get.side_effect = [
            _existence_response(("MCPT-1", "task"), ("MCPT-2", "requirement")),
            _enum_get_response(["draft", "open"]),
            _enum_get_response(["draft", "approved"]),
        ]

        result = await _call_update(
            mock_ctx,
            items=[
                _spec(status="draft"),
                _spec(work_item_id="MCPT-2", status="draft"),
            ],
            dry_run=True,
        )

        assert result.dry_run is True
        status_calls = [
            c
            for c in mock_client.get.call_args_list
            if "fields/status/actions/getAvailableOptions" in c.args[0]
        ]
        assert [c.kwargs["params"]["type"] for c in status_calls] == [
            "task",
            "requirement",
        ]

    async def test_guard_runs_on_dry_run_too(
        self,
        mock_ctx: MagicMock,
        mock_client: AsyncMock,
        reset_enum_guard_caches: None,
    ) -> None:
        mock_client.get.side_effect = [
            _existence_response(("MCPT-1", "task")),
            _enum_get_response(["done"]),
        ]

        with pytest.raises(ValueError, match="resolution='ghost'"):
            await _call_update(
                mock_ctx, items=[_spec(resolution="ghost")], dry_run=True
            )

        mock_client.patch.assert_not_called()


class TestCreateWorkItemsAttachmentRefGuard:
    """Greenfield create -- any scheme ref in converted description block
    write outright, item can't own attachments before it exists.
    """

    async def test_clean_markdown_description_is_unaffected(
        self, mock_ctx: MagicMock, mock_client: AsyncMock
    ) -> None:
        mock_client.post.return_value = {
            "data": [{"type": "workitems", "id": "MyProj/MCPT-1"}]
        }

        result = await _call_create_wi(mock_ctx, description="Plain paragraph.")

        assert result.created is True  # type: ignore[attr-defined]
        mock_client.post.assert_awaited_once()

    async def test_markdown_image_ref_rejected_before_create(
        self, mock_ctx: MagicMock, mock_client: AsyncMock
    ) -> None:
        """Guard runs on markdown_to_html output PRE-sanitize_html -- same
        conversion pipeline as create_document, same finding.
        """
        with pytest.raises(ValueError, match="attachments cannot exist"):
            await _call_create_wi(mock_ctx, description="![x](workitemimg:ghost.png)")
        mock_client.post.assert_not_called()


class TestUpdateWorkItemsAttachmentRefGuard:
    """``update_work_items`` verify each item's ``description_html``
    attachment refs against its live attachment list before PATCH.
    """

    async def test_dangling_ref_in_second_item_names_batch_position(
        self, mock_ctx: MagicMock, mock_client: AsyncMock
    ) -> None:
        mock_client.get.side_effect = [
            _existence_response(("MCPT-1", "task"), ("MCPT-2", "task")),
            attachments_response(["1-real.png"], meta=False),
            attachments_response(["1-real.png"], meta=False),
        ]

        with pytest.raises(ValueError) as exc:
            await _call_update(
                mock_ctx,
                items=[
                    _spec(
                        work_item_id="MCPT-1",
                        description_html='<img src="workitemimg:1-real.png"/>',
                    ),
                    _spec(
                        work_item_id="MCPT-2",
                        description_html='<img src="workitemimg:ghost.png"/>',
                    ),
                ],
            )

        message = str(exc.value)
        assert "items[1] ('MCPT-2')" in message
        assert "list_work_item_attachments" in message
        mock_client.patch.assert_not_called()

    async def test_valid_ref_item_passes(
        self, mock_ctx: MagicMock, mock_client: AsyncMock
    ) -> None:
        mock_client.get.side_effect = [
            _existence_response(("MCPT-1", "task")),
            attachments_response(["1-real.png"], meta=False),
        ]
        mock_client.patch.return_value = {}

        result = await _call_update(
            mock_ctx,
            items=[_spec(description_html='<img src="workitemimg:1-real.png"/>')],
        )

        assert result.updated is True
        mock_client.patch.assert_awaited_once()

    async def test_spec_without_description_html_adds_no_attachments_get(
        self, mock_ctx: MagicMock, mock_client: AsyncMock
    ) -> None:
        mock_client.get.return_value = _existence_response(("MCPT-1", "task"))
        mock_client.patch.return_value = {}

        await _call_update(mock_ctx, items=[_spec(title="t")])

        assert mock_client.get.await_count == 1


class TestUpdateWorkItemsAttachmentRefDocstringClause:
    """Lock attachment-ref validation clause into public docstring."""

    def test_docstring_names_list_work_item_attachments(self) -> None:
        document = update_work_items.__doc__ or ""
        assert "list_work_item_attachments" in document


class TestListWorkItems:
    """``list_work_items`` tool."""

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
                        "author": {"data": {"type": "users", "id": "proj1/alice"}},
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
                    },
                },
            ],
            "included": [
                {
                    "type": "users",
                    "id": "proj1/alice",
                    "attributes": {"name": "Alice A"},
                }
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
        assert first.author_name == "Alice A"

        second = result.items[1]
        assert second.id == "MCPT-002"
        assert second.priority == ""
        assert second.updated == ""
        assert second.space_id == ""
        assert second.document_name == ""
        assert second.author_name == ""

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
        assert kwargs["params"]["include"] == "author"
        assert kwargs["params"]["fields[users]"] == "name"

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
        """Polarion omit totalCount (return 0) → item count as floor."""
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

        # Floor = returned item count.
        assert result.total_count >= 2


class TestGetWorkItem:
    """``get_work_item`` tool."""

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
            "included": [
                {"type": "users", "id": "proj1/bob", "attributes": {"name": "Bob B"}},
                {
                    "type": "users",
                    "id": "proj1/alice",
                    "attributes": {"name": "Alice A"},
                },
            ],
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
        assert result.assignee_names == ["Alice A"]
        assert result.author_id == "bob"
        assert result.author_name == "Bob B"
        # Entry without uri skipped.
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
        """include_description_html=False blank field — body still travel
        (``@all`` for customs); passed explicit, direct calls bypass defaults.
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
        """Polarion spans / data-* attrs survive read.

        Round-trip guarantee for update_work_items description_html.
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
        # Defaults for detail-only fields.
        assert result.author_id == ""
        assert result.author_name == ""
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
        """Inline non-standard attrs flow through as ``custom_fields``."""
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
        """``fields[workitems]=@all`` = only token that surface customs."""
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
        assert kwargs["params"]["include"] == "assignee,author"
        assert kwargs["params"]["fields[users]"] == "name"


class TestReadWorkItem:
    """``read_work_item`` tool.

    Delegate fetch + error mapping to ``get_work_item``; convert raw HTML
    body to Markdown via ``html_to_markdown()``.
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
            "included": [
                {"type": "users", "id": "proj1/bob", "attributes": {"name": "Bob B"}}
            ],
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
        assert result.author_name == "Bob B"
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
        """Read-only contract: WorkItemRead.description NOT round-trip shape —
        feed it back lose polarion-rte-link span.
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
        # Customs stay raw — dict round-trip through update_work_items.
        assert result.custom_fields == {
            "riskLevel": "high",
            "reviewerNote": rich_value,
        }

    async def test_no_description_html_field_on_model(
        self, mock_ctx: MagicMock, mock_client: AsyncMock
    ) -> None:
        """WorkItemRead expose no description_html — read-only contract."""
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
