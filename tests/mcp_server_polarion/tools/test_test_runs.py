"""Tests for the test run tools — list parsing/param forwarding, bulk create
payloads and guards, error mapping, and field bounds.
"""

from __future__ import annotations

import inspect
from typing import Annotated, get_type_hints
from unittest.mock import AsyncMock, MagicMock

import pytest
from pydantic import TypeAdapter, ValidationError

from mcp_server_polarion.core.exceptions import (
    PolarionAuthError,
    PolarionError,
    PolarionNotFoundError,
)
from mcp_server_polarion.models import PaginatedResult, TestRunCreateSpec
from mcp_server_polarion.tools.test_runs import (
    _build_create_test_runs_payload,
    create_test_runs,
    list_test_runs,
)


def _template_response(run_id: str) -> dict[str, object]:
    """Single-testrun GET body the template guard accepts."""
    return {
        "data": {
            "type": "testruns",
            "id": f"proj1/{run_id}",
            "attributes": {"id": run_id, "isTemplate": True},
        }
    }


class TestListTestRuns:
    """Tests for the ``list_test_runs`` tool."""

    async def test_returns_test_runs(
        self, mock_ctx: MagicMock, mock_client: AsyncMock
    ) -> None:
        mock_client.get.return_value = {
            "data": [
                {
                    "type": "testruns",
                    "id": "proj1/TR-001",
                    "attributes": {
                        "title": "Sprint 1 Regression",
                        "type": "manual",
                        "status": "open",
                        "finishedOn": "2026-05-02T11:00:00Z",
                        "updated": "2026-05-01T09:00:00Z",
                        "isTemplate": False,
                    },
                    "relationships": {
                        "author": {"data": {"type": "users", "id": "proj1/devemberx"}}
                    },
                },
                {
                    "type": "testruns",
                    "id": "proj1/TR-002",
                    "attributes": {
                        "title": "Smoke Template",
                        "type": "automated",
                        "status": "finished",
                        "isTemplate": True,
                    },
                    "relationships": {"author": {"data": None}},
                },
            ],
            "included": [
                {
                    "type": "users",
                    "id": "proj1/devemberx",
                    "attributes": {"name": "Devember X"},
                }
            ],
            "meta": {"totalCount": 2},
        }

        result = await list_test_runs(
            mock_ctx,
            project_id="proj1",
            query=None,
            templates=False,
            page_size=100,
            page_number=1,
        )

        assert isinstance(result, PaginatedResult)
        assert len(result.items) == 2
        assert result.total_count == 2

        first = result.items[0]
        assert first.id == "TR-001"
        assert first.title == "Sprint 1 Regression"
        assert first.type == "manual"
        assert first.status == "open"
        assert first.finished_on == "2026-05-02T11:00:00Z"
        assert first.updated == "2026-05-01T09:00:00Z"
        assert first.author_name == "Devember X"
        assert first.is_template is False

        second = result.items[1]
        assert second.id == "TR-002"
        assert second.finished_on == ""
        assert second.updated == ""
        assert second.author_name == ""
        assert second.is_template is True

    async def test_missing_author_yields_empty_name(
        self, mock_ctx: MagicMock, mock_client: AsyncMock
    ) -> None:
        mock_client.get.return_value = {
            "data": [
                {
                    "id": "proj1/TR-003",
                    "attributes": {
                        "title": "No Author",
                        "type": "manual",
                        "status": "open",
                    },
                    "relationships": {
                        "author": {"data": {"type": "users", "id": "proj1/ghost"}}
                    },
                }
            ],
            "meta": {"totalCount": 1},
        }

        result = await list_test_runs(
            mock_ctx,
            project_id="proj1",
            query=None,
            templates=False,
            page_size=100,
            page_number=1,
        )

        assert result.items[0].author_name == ""

    async def test_sparse_fieldset_and_includes_requested(
        self, mock_ctx: MagicMock, mock_client: AsyncMock
    ) -> None:
        mock_client.get.return_value = {"data": [], "meta": {"totalCount": 0}}

        await list_test_runs(
            mock_ctx,
            project_id="proj1",
            query=None,
            templates=False,
            page_size=100,
            page_number=1,
        )

        _, kwargs = mock_client.get.call_args
        params = kwargs["params"]
        assert "fields[testruns]" in params
        assert params["include"] == "author"
        assert params["fields[users]"] == "name"

    async def test_strips_project_prefix_from_id(
        self, mock_ctx: MagicMock, mock_client: AsyncMock
    ) -> None:
        mock_client.get.return_value = {
            "data": [
                {
                    "id": "myproject/TR-100",
                    "attributes": {
                        "title": "Run",
                        "type": "manual",
                        "status": "open",
                    },
                }
            ],
            "meta": {"totalCount": 1},
        }

        result = await list_test_runs(
            mock_ctx,
            project_id="myproject",
            query=None,
            templates=False,
            page_size=100,
            page_number=1,
        )

        assert result.items[0].id == "TR-100"

    async def test_query_param_forwarded(
        self, mock_ctx: MagicMock, mock_client: AsyncMock
    ) -> None:
        mock_client.get.return_value = {"data": [], "meta": {"totalCount": 0}}

        await list_test_runs(
            mock_ctx,
            project_id="proj1",
            query="author.id:devemberx",
            templates=False,
            page_size=100,
            page_number=1,
        )

        _, kwargs = mock_client.get.call_args
        assert kwargs["params"]["query"] == "author.id:devemberx"

    async def test_query_none_omits_param(
        self, mock_ctx: MagicMock, mock_client: AsyncMock
    ) -> None:
        mock_client.get.return_value = {"data": [], "meta": {"totalCount": 0}}

        await list_test_runs(
            mock_ctx,
            project_id="proj1",
            query=None,
            templates=False,
            page_size=100,
            page_number=1,
        )

        _, kwargs = mock_client.get.call_args
        assert "query" not in kwargs["params"]

    async def test_sql_prefix_query_passed_verbatim(
        self, mock_ctx: MagicMock, mock_client: AsyncMock
    ) -> None:
        sql_query = (
            "SQL:(SELECT tr.* FROM POLARION.TESTRUN tr WHERE tr.C_STATUS = 'open')"
        )
        mock_client.get.return_value = {"data": [], "meta": {"totalCount": 0}}

        await list_test_runs(
            mock_ctx,
            project_id="proj1",
            query=sql_query,
            templates=False,
            page_size=100,
            page_number=1,
        )

        _, kwargs = mock_client.get.call_args
        assert kwargs["params"]["query"] == sql_query

    async def test_templates_true_adds_param(
        self, mock_ctx: MagicMock, mock_client: AsyncMock
    ) -> None:
        mock_client.get.return_value = {"data": [], "meta": {"totalCount": 0}}

        await list_test_runs(
            mock_ctx,
            project_id="proj1",
            query=None,
            templates=True,
            page_size=100,
            page_number=1,
        )

        _, kwargs = mock_client.get.call_args
        assert kwargs["params"]["templates"] == "true"

    async def test_templates_false_omits_param(
        self, mock_ctx: MagicMock, mock_client: AsyncMock
    ) -> None:
        mock_client.get.return_value = {"data": [], "meta": {"totalCount": 0}}

        await list_test_runs(
            mock_ctx,
            project_id="proj1",
            query=None,
            templates=False,
            page_size=100,
            page_number=1,
        )

        _, kwargs = mock_client.get.call_args
        assert "templates" not in kwargs["params"]

    async def test_project_not_found_raises_value_error(
        self, mock_ctx: MagicMock, mock_client: AsyncMock
    ) -> None:
        mock_client.get.side_effect = PolarionNotFoundError(
            "Not found", status_code=404
        )

        with pytest.raises(ValueError, match="not found"):
            await list_test_runs(
                mock_ctx,
                project_id="missing",
                query=None,
                templates=False,
                page_size=100,
                page_number=1,
            )

    async def test_auth_error_raises_permission_error(
        self, mock_ctx: MagicMock, mock_client: AsyncMock
    ) -> None:
        mock_client.get.side_effect = PolarionAuthError("auth", status_code=401)

        with pytest.raises(PermissionError):
            await list_test_runs(
                mock_ctx,
                project_id="proj1",
                query=None,
                templates=False,
                page_size=100,
                page_number=1,
            )

    async def test_other_error_raises_runtime_error(
        self, mock_ctx: MagicMock, mock_client: AsyncMock
    ) -> None:
        mock_client.get.side_effect = PolarionError("boom", status_code=500)

        with pytest.raises(RuntimeError, match="boom"):
            await list_test_runs(
                mock_ctx,
                project_id="proj1",
                query=None,
                templates=False,
                page_size=100,
                page_number=1,
            )


class TestBuildCreateTestRunsPayload:
    """Direct payload-builder unit tests."""

    def test_full_spec_builds_attributes_and_template(self) -> None:
        payload = _build_create_test_runs_payload(
            project_id="proj1",
            specs=[
                TestRunCreateSpec(
                    id="TR-100",
                    title="Sprint 9 Regression",
                    type="manual",
                    status="open",
                    template_id="Empty",
                    custom_fields={"goal": "verify release"},
                )
            ],
        )

        assert payload == {
            "data": [
                {
                    "type": "testruns",
                    "attributes": {
                        "id": "TR-100",
                        "title": "Sprint 9 Regression",
                        "type": "manual",
                        "status": "open",
                        "goal": "verify release",
                    },
                    "relationships": {
                        "template": {"data": {"type": "testruns", "id": "proj1/Empty"}}
                    },
                }
            ]
        }

    def test_minimal_spec_sends_only_id(self) -> None:
        payload = _build_create_test_runs_payload(
            project_id="proj1", specs=[TestRunCreateSpec(id="TR-1")]
        )

        data = payload["data"]
        assert isinstance(data, list)
        resource = data[0]
        assert isinstance(resource, dict)
        assert resource["attributes"] == {"id": "TR-1"}
        assert "relationships" not in resource

    def test_multiple_specs_keep_order(self) -> None:
        payload = _build_create_test_runs_payload(
            project_id="proj1",
            specs=[TestRunCreateSpec(id="TR-1"), TestRunCreateSpec(id="TR-2")],
        )

        data = payload["data"]
        assert isinstance(data, list)
        ids = [
            r["attributes"]["id"]  # type: ignore[call-overload, index]
            for r in data
        ]
        assert ids == ["TR-1", "TR-2"]

    def test_custom_field_shadowing_standard_attribute_raises(self) -> None:
        with pytest.raises(ValueError, match="standard Polarion attributes"):
            _build_create_test_runs_payload(
                project_id="proj1",
                specs=[TestRunCreateSpec(id="TR-1", custom_fields={"title": "x"})],
            )


class TestCreateTestRuns:
    """Tests for the ``create_test_runs`` tool."""

    async def test_minimal_create_posts_and_returns_ids(
        self, mock_ctx: MagicMock, mock_client: AsyncMock
    ) -> None:
        mock_client.post.return_value = {
            "data": [{"type": "testruns", "id": "proj1/TR-1"}]
        }

        result = await create_test_runs(
            mock_ctx,
            project_id="proj1",
            items=[TestRunCreateSpec(id="TR-1")],
            dry_run=False,
        )

        assert result.created is True
        assert result.dry_run is False
        assert result.test_run_ids == ["TR-1"]
        assert result.payload_preview is None
        # No enums / template / custom fields supplied -> no guard traffic.
        mock_client.get.assert_not_awaited()
        path = mock_client.post.await_args.args[0]
        assert path == "/projects/proj1/testruns"

    async def test_duplicate_ids_rejected_before_any_request(
        self, mock_ctx: MagicMock, mock_client: AsyncMock
    ) -> None:
        # POST requires an explicit id per item; two items sharing one id
        # cannot both be created.
        with pytest.raises(ValueError, match="Duplicate id"):
            await create_test_runs(
                mock_ctx,
                project_id="proj1",
                items=[TestRunCreateSpec(id="TR-1"), TestRunCreateSpec(id="TR-1")],
                dry_run=False,
            )
        mock_client.get.assert_not_awaited()
        mock_client.post.assert_not_awaited()

    async def test_unknown_extra_key_rejected(self) -> None:
        # Unknown keys would otherwise vanish silently (Polarion drops them).
        with pytest.raises(ValidationError):
            TestRunCreateSpec(id="TR-1", tittle="typo")  # type: ignore[call-arg]

    async def test_create_with_template_resolves_it_first(
        self, mock_ctx: MagicMock, mock_client: AsyncMock
    ) -> None:
        mock_client.get.return_value = _template_response("Empty")
        mock_client.post.return_value = {
            "data": [{"type": "testruns", "id": "proj1/TR-2"}]
        }

        result = await create_test_runs(
            mock_ctx,
            project_id="proj1",
            items=[TestRunCreateSpec(id="TR-2", template_id="Empty")],
            dry_run=False,
        )

        assert result.test_run_ids == ["TR-2"]
        assert mock_client.get.await_args.args[0] == "/projects/proj1/testruns/Empty"
        payload = mock_client.post.await_args.kwargs["json"]
        assert payload["data"][0]["relationships"]["template"]["data"] == {
            "type": "testruns",
            "id": "proj1/Empty",
        }

    async def test_dry_run_returns_payload_without_posting(
        self, mock_ctx: MagicMock, mock_client: AsyncMock
    ) -> None:
        result = await create_test_runs(
            mock_ctx,
            project_id="proj1",
            items=[TestRunCreateSpec(id="TR-3", title="Preview")],
            dry_run=True,
        )

        assert result.created is False
        assert result.dry_run is True
        assert result.test_run_ids == []
        assert result.payload_preview is not None
        data = result.payload_preview["data"]
        assert isinstance(data, list)
        mock_client.post.assert_not_awaited()

    async def test_unknown_type_blocks_before_post(
        self, mock_ctx: MagicMock, mock_client: AsyncMock
    ) -> None:
        mock_client.get.return_value = {
            "data": {
                "type": "enumerations",
                "id": "testrun-type",
                "attributes": {"options": [{"id": "manual"}, {"id": "automated"}]},
            }
        }

        with pytest.raises(ValueError, match="test run type"):
            await create_test_runs(
                mock_ctx,
                project_id="proj1",
                items=[TestRunCreateSpec(id="TR-4", type="ghost")],
                dry_run=False,
            )

        mock_client.post.assert_not_awaited()

    async def test_custom_fields_guarded_against_sample(
        self, mock_ctx: MagicMock, mock_client: AsyncMock
    ) -> None:
        mock_client.get.side_effect = [
            {
                "data": [
                    {
                        "type": "testruns",
                        "id": "proj1/R1",
                        "attributes": {"id": "R1", "goal": "g"},
                    }
                ]
            },
            {"data": []},
        ]
        mock_client.post.return_value = {
            "data": [{"type": "testruns", "id": "proj1/TR-5"}]
        }

        result = await create_test_runs(
            mock_ctx,
            project_id="proj1",
            items=[TestRunCreateSpec(id="TR-5", custom_fields={"goal": "x"})],
            dry_run=False,
        )

        assert result.test_run_ids == ["TR-5"]
        # Sampled instances then templates before the POST.
        assert mock_client.get.await_count == 2

    async def test_project_not_found_raises_value_error(
        self, mock_ctx: MagicMock, mock_client: AsyncMock
    ) -> None:
        mock_client.post.side_effect = PolarionNotFoundError(
            "Not found", status_code=404
        )

        with pytest.raises(ValueError, match="not found"):
            await create_test_runs(
                mock_ctx,
                project_id="missing",
                items=[TestRunCreateSpec(id="TR-6")],
                dry_run=False,
            )

    async def test_auth_error_raises_permission_error(
        self, mock_ctx: MagicMock, mock_client: AsyncMock
    ) -> None:
        mock_client.post.side_effect = PolarionAuthError("auth", status_code=401)

        with pytest.raises(PermissionError):
            await create_test_runs(
                mock_ctx,
                project_id="proj1",
                items=[TestRunCreateSpec(id="TR-7")],
                dry_run=False,
            )

    async def test_other_error_raises_runtime_error(
        self, mock_ctx: MagicMock, mock_client: AsyncMock
    ) -> None:
        mock_client.post.side_effect = PolarionError("boom", status_code=500)

        with pytest.raises(RuntimeError, match="boom"):
            await create_test_runs(
                mock_ctx,
                project_id="proj1",
                items=[TestRunCreateSpec(id="TR-8")],
                dry_run=False,
            )

    async def test_id_count_mismatch_raises(
        self, mock_ctx: MagicMock, mock_client: AsyncMock
    ) -> None:
        mock_client.post.return_value = {"data": []}

        with pytest.raises(RuntimeError, match="list_test_runs"):
            await create_test_runs(
                mock_ctx,
                project_id="proj1",
                items=[TestRunCreateSpec(id="TR-9")],
                dry_run=False,
            )


class TestCreateTestRunsFieldValidation:
    """Bulk bounds + spec constraints via ``TypeAdapter`` rebuild."""

    @staticmethod
    def _adapter(param_name: str) -> TypeAdapter[object]:
        hints = get_type_hints(create_test_runs)
        sig = inspect.signature(create_test_runs)
        field_info = sig.parameters[param_name].default
        return TypeAdapter(Annotated[hints[param_name], field_info])

    def test_empty_items_rejected(self) -> None:
        with pytest.raises(ValidationError):
            self._adapter("items").validate_python([])

    def test_items_above_max_rejected(self) -> None:
        specs = [{"id": f"TR-{i}"} for i in range(51)]
        with pytest.raises(ValidationError):
            self._adapter("items").validate_python(specs)

    def test_items_at_max_accepted(self) -> None:
        specs = [{"id": f"TR-{i}"} for i in range(50)]
        validated = self._adapter("items").validate_python(specs)
        assert isinstance(validated, list)
        assert len(validated) == 50

    def test_empty_project_id_rejected(self) -> None:
        with pytest.raises(ValidationError):
            self._adapter("project_id").validate_python("")

    def test_spec_requires_non_empty_id(self) -> None:
        with pytest.raises(ValidationError):
            TestRunCreateSpec(id="")


class TestListTestRunsFieldValidation:
    """``page_size`` bounds — direct calls bypass JSON Schema; proven via
    ``TypeAdapter`` rebuild from the signature.
    """

    @staticmethod
    def _adapter(param_name: str) -> TypeAdapter[object]:
        hints = get_type_hints(list_test_runs)
        sig = inspect.signature(list_test_runs)
        field_info = sig.parameters[param_name].default
        return TypeAdapter(Annotated[hints[param_name], field_info])

    def test_page_size_boundaries_accepted(self) -> None:
        adapter = self._adapter("page_size")
        assert adapter.validate_python(1) == 1
        assert adapter.validate_python(100) == 100

    def test_page_size_below_min_rejected(self) -> None:
        with pytest.raises(ValidationError):
            self._adapter("page_size").validate_python(0)

    def test_page_size_above_max_rejected(self) -> None:
        with pytest.raises(ValidationError):
            self._adapter("page_size").validate_python(101)

    def test_page_number_below_min_rejected(self) -> None:
        with pytest.raises(ValidationError):
            self._adapter("page_number").validate_python(0)
