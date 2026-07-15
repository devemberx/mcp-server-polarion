"""Test run tools — list parsing/param forwarding, bulk create payloads and
guards, error mapping, field bounds.
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
from mcp_server_polarion.models import (
    PaginatedResult,
    TestRunCreateSpec,
    TestRunDetail,
    TestRunUpdateSpec,
)
from mcp_server_polarion.tools.test_runs import (
    _build_create_test_runs_payload,
    _build_update_test_runs_payload,
    create_test_runs,
    get_test_run,
    list_test_runs,
    update_test_runs,
)


def _template_response(run_id: str) -> dict[str, object]:
    """Single-testrun GET body template guard accept."""
    return {
        "data": {
            "type": "testruns",
            "id": f"proj1/{run_id}",
            "attributes": {"id": run_id, "isTemplate": True},
        }
    }


class TestListTestRuns:
    """``list_test_runs`` tool."""

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
                        "groupId": "Release-2.5",
                    },
                    "relationships": {
                        "author": {"data": {"type": "users", "id": "proj1/devemberx"}},
                        "template": {
                            "data": {"type": "testruns", "id": "proj1/TR-tmpl"}
                        },
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
        assert first.group_id == "Release-2.5"
        assert first.template_id == "TR-tmpl"

        second = result.items[1]
        assert second.id == "TR-002"
        assert second.finished_on == ""
        assert second.updated == ""
        assert second.author_name == ""
        assert second.is_template is True
        # No groupId attr / template relationship -> blanks.
        assert second.group_id == ""
        assert second.template_id == ""

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

        # Unresolvable author id → name stay blank.
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
        # groupId attr + template relationship kept under sparse fieldset.
        assert "groupId" in params["fields[testruns]"]
        assert "template" in params["fields[testruns]"]
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
            query='author.name:"Jane Doe"',
            templates=False,
            page_size=100,
            page_number=1,
        )

        _, kwargs = mock_client.get.call_args
        assert kwargs["params"]["query"] == 'author.name:"Jane Doe"'

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

    async def test_has_value_query_passed_verbatim(
        self, mock_ctx: MagicMock, mock_client: AsyncMock
    ) -> None:
        has_value_query = "HAS_VALUE:goal"
        mock_client.get.return_value = {"data": [], "meta": {"totalCount": 0}}

        await list_test_runs(
            mock_ctx,
            project_id="proj1",
            query=has_value_query,
            templates=False,
            page_size=100,
            page_number=1,
        )

        _, kwargs = mock_client.get.call_args
        assert kwargs["params"]["query"] == has_value_query

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


class TestListTestRunsQueryDocumentation:
    """Lock query docs to Lucene-only — ``SQL:(...)`` on ``/testruns`` 400 on
    live server (endpoint wrap query verbatim into Lucene), so even a "no SQL"
    disclaimer must not reintroduce the token an LLM could copy.
    """

    def test_query_docs_promise_lucene_only(self) -> None:
        field_info = inspect.signature(list_test_runs).parameters["query"].default
        assert "SQL" not in field_info.description, (
            "list_test_runs query description must not mention SQL -- "
            "/testruns has no SQL support (400 verified live)"
        )
        assert "HAS_VALUE" in field_info.description, (
            "list_test_runs query description must document the HAS_VALUE filter"
        )
        assert "SQL" not in (list_test_runs.__doc__ or ""), (
            "list_test_runs docstring must not mention SQL"
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
    """``create_test_runs`` tool."""

    async def test_duplicate_ids_rejected_before_any_request(
        self, mock_ctx: MagicMock, mock_client: AsyncMock
    ) -> None:
        # Client-supplied ids can collide; server would 409 after partially
        # creating the batch.
        with pytest.raises(ValueError, match=r"Duplicate id\(s\) \['TR-1'\]"):
            await create_test_runs(
                mock_ctx,
                project_id="proj1",
                items=[
                    TestRunCreateSpec(id="TR-1", title="a"),
                    TestRunCreateSpec(id="TR-1", title="b"),
                ],
                dry_run=False,
            )
        mock_client.get.assert_not_awaited()
        mock_client.post.assert_not_awaited()

    async def test_shadowing_custom_key_raises_before_network(
        self, mock_ctx: MagicMock, mock_client: AsyncMock
    ) -> None:
        # title via custom_fields collide standard attr -- local build check
        # raise clear param hint before guard round-trip.
        with pytest.raises(ValueError, match="standard Polarion attributes"):
            await create_test_runs(
                mock_ctx,
                project_id="proj1",
                items=[TestRunCreateSpec(id="TR-1", custom_fields={"title": "shadow"})],
                dry_run=False,
            )

        mock_client.get.assert_not_awaited()
        mock_client.post.assert_not_awaited()

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
        # No enums / template / custom fields -> no guard traffic.
        mock_client.get.assert_not_awaited()
        path = mock_client.post.await_args.args[0]
        assert path == "/projects/proj1/testruns"

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
        # Sampled instances then templates before POST.
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


class TestBuildUpdateTestRunsPayload:
    """``_build_update_test_runs_payload`` unit seam."""

    def test_full_spec_builds_attributes(self) -> None:
        spec = TestRunUpdateSpec(
            test_run_id="TR-100",
            title="New Title",
            status="finished",
            group_id="Release-2.5",
            custom_fields={"goal": "regression"},
        )

        payload = _build_update_test_runs_payload(project_id="proj1", specs=[spec])

        data = payload["data"]
        assert isinstance(data, list)
        resource = data[0]
        assert isinstance(resource, dict)
        assert resource["type"] == "testruns"
        assert resource["id"] == "proj1/TR-100"
        assert resource["attributes"] == {
            "title": "New Title",
            "status": "finished",
            "groupId": "Release-2.5",
            "goal": "regression",
        }
        assert "relationships" not in resource

    def test_partial_spec_skips_unset(self) -> None:
        spec = TestRunUpdateSpec(test_run_id="TR-101", title="Only Title")

        payload = _build_update_test_runs_payload(project_id="proj1", specs=[spec])

        data = payload["data"]
        assert isinstance(data, list)
        resource = data[0]
        assert isinstance(resource, dict)
        assert resource["attributes"] == {"title": "Only Title"}

    def test_multiple_specs_keep_order(self) -> None:
        specs = [
            TestRunUpdateSpec(test_run_id="TR-1", title="A"),
            TestRunUpdateSpec(test_run_id="TR-2", title="B"),
        ]

        payload = _build_update_test_runs_payload(project_id="proj1", specs=specs)

        data = payload["data"]
        assert isinstance(data, list)
        ids = [entry["id"] for entry in data if isinstance(entry, dict)]
        assert ids == ["proj1/TR-1", "proj1/TR-2"]

    def test_custom_field_shadowing_standard_attribute_raises(self) -> None:
        spec = TestRunUpdateSpec(
            test_run_id="TR-102", custom_fields={"title": "shadow"}
        )

        with pytest.raises(ValueError, match="standard Polarion attributes"):
            _build_update_test_runs_payload(project_id="proj1", specs=[spec])


class TestUpdateTestRuns:
    """``update_test_runs`` tool."""

    async def test_duplicate_ids_rejected_before_any_request(
        self, mock_ctx: MagicMock, mock_client: AsyncMock
    ) -> None:
        items = [
            TestRunUpdateSpec(test_run_id="TR-1", title="A"),
            TestRunUpdateSpec(test_run_id="TR-1", title="B"),
        ]

        with pytest.raises(ValueError, match=r"Duplicate test_run_id\(s\)"):
            await update_test_runs(
                mock_ctx, project_id="proj1", items=items, dry_run=False
            )

        mock_client.get.assert_not_awaited()
        mock_client.patch.assert_not_awaited()

    async def test_minimal_update_patches_and_returns_ids(
        self, mock_ctx: MagicMock, mock_client: AsyncMock
    ) -> None:
        mock_client.patch.return_value = None

        result = await update_test_runs(
            mock_ctx,
            project_id="proj1",
            items=[TestRunUpdateSpec(test_run_id="TR-1", title="Renamed")],
            dry_run=False,
        )

        assert result.updated is True
        assert result.dry_run is False
        assert result.test_run_ids == ["TR-1"]
        assert result.payload_preview is None
        # Title-only update need zero guard traffic.
        mock_client.get.assert_not_awaited()
        args, kwargs = mock_client.patch.await_args
        assert args[0] == "/projects/proj1/testruns"
        data = kwargs["json"]["data"]
        assert data[0]["id"] == "proj1/TR-1"

    async def test_dry_run_returns_payload_without_patching(
        self, mock_ctx: MagicMock, mock_client: AsyncMock
    ) -> None:
        result = await update_test_runs(
            mock_ctx,
            project_id="proj1",
            items=[TestRunUpdateSpec(test_run_id="TR-2", title="Preview")],
            dry_run=True,
        )

        assert result.updated is False
        assert result.dry_run is True
        assert result.test_run_ids == []
        assert result.payload_preview is not None
        data = result.payload_preview["data"]
        assert isinstance(data, list)
        mock_client.patch.assert_not_awaited()

    async def test_unknown_status_blocks_before_patch(
        self, mock_ctx: MagicMock, mock_client: AsyncMock
    ) -> None:
        mock_client.get.return_value = {
            "data": {
                "type": "enumerations",
                "id": "testrun-status",
                "attributes": {"options": [{"id": "open"}, {"id": "finished"}]},
            }
        }

        with pytest.raises(ValueError, match=r"items\[0\].*test run status"):
            await update_test_runs(
                mock_ctx,
                project_id="proj1",
                items=[TestRunUpdateSpec(test_run_id="TR-3", status="ghost")],
                dry_run=False,
            )

        mock_client.patch.assert_not_awaited()

    async def test_dry_run_still_runs_guards(
        self, mock_ctx: MagicMock, mock_client: AsyncMock
    ) -> None:
        mock_client.get.return_value = {
            "data": {
                "type": "enumerations",
                "id": "testrun-status",
                "attributes": {"options": [{"id": "open"}]},
            }
        }

        with pytest.raises(ValueError, match="test run status"):
            await update_test_runs(
                mock_ctx,
                project_id="proj1",
                items=[TestRunUpdateSpec(test_run_id="TR-4", status="ghost")],
                dry_run=True,
            )

        mock_client.patch.assert_not_awaited()

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
        mock_client.patch.return_value = None

        result = await update_test_runs(
            mock_ctx,
            project_id="proj1",
            items=[TestRunUpdateSpec(test_run_id="TR-5", custom_fields={"goal": "x"})],
            dry_run=False,
        )

        assert result.test_run_ids == ["TR-5"]
        # Sampled instances then templates before PATCH.
        assert mock_client.get.await_count == 2

    async def test_shadowing_custom_key_raises_before_network(
        self, mock_ctx: MagicMock, mock_client: AsyncMock
    ) -> None:
        # title via custom_fields collide standard attr -- local build check
        # raise clear param hint before guard round-trip.
        with pytest.raises(ValueError, match="standard Polarion attributes"):
            await update_test_runs(
                mock_ctx,
                project_id="proj1",
                items=[
                    TestRunUpdateSpec(
                        test_run_id="TR-9", custom_fields={"title": "shadow"}
                    )
                ],
                dry_run=False,
            )

        mock_client.get.assert_not_awaited()
        mock_client.patch.assert_not_awaited()

    async def test_custom_field_guard_error_names_offending_item(
        self, mock_ctx: MagicMock, mock_client: AsyncMock
    ) -> None:
        # First item title-only (no guard); second's unknown custom key trips
        # guard -- error must carry its batch position, not items[0].
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

        with pytest.raises(ValueError, match=r"items\[1\].*bogus"):
            await update_test_runs(
                mock_ctx,
                project_id="proj1",
                items=[
                    TestRunUpdateSpec(test_run_id="TR-A", title="ok"),
                    TestRunUpdateSpec(test_run_id="TR-B", custom_fields={"bogus": "x"}),
                ],
                dry_run=False,
            )

        mock_client.patch.assert_not_awaited()

    async def test_run_not_found_raises_value_error(
        self, mock_ctx: MagicMock, mock_client: AsyncMock
    ) -> None:
        mock_client.patch.side_effect = PolarionNotFoundError(
            "Not found", status_code=404
        )

        with pytest.raises(ValueError, match="list_test_runs"):
            await update_test_runs(
                mock_ctx,
                project_id="proj1",
                items=[TestRunUpdateSpec(test_run_id="TR-6", title="X")],
                dry_run=False,
            )

    async def test_auth_error_raises_permission_error(
        self, mock_ctx: MagicMock, mock_client: AsyncMock
    ) -> None:
        mock_client.patch.side_effect = PolarionAuthError("auth", status_code=401)

        with pytest.raises(PermissionError):
            await update_test_runs(
                mock_ctx,
                project_id="proj1",
                items=[TestRunUpdateSpec(test_run_id="TR-7", title="X")],
                dry_run=False,
            )

    async def test_other_error_raises_runtime_error(
        self, mock_ctx: MagicMock, mock_client: AsyncMock
    ) -> None:
        mock_client.patch.side_effect = PolarionError("boom", status_code=500)

        with pytest.raises(RuntimeError, match="boom"):
            await update_test_runs(
                mock_ctx,
                project_id="proj1",
                items=[TestRunUpdateSpec(test_run_id="TR-8", title="X")],
                dry_run=False,
            )


class TestUpdateTestRunsFieldValidation:
    """Bulk bounds + spec constraints via ``TypeAdapter`` rebuild."""

    @staticmethod
    def _adapter(param_name: str) -> TypeAdapter[object]:
        hints = get_type_hints(update_test_runs)
        sig = inspect.signature(update_test_runs)
        field_info = sig.parameters[param_name].default
        return TypeAdapter(Annotated[hints[param_name], field_info])

    def test_empty_items_rejected(self) -> None:
        with pytest.raises(ValidationError):
            self._adapter("items").validate_python([])

    def test_items_above_max_rejected(self) -> None:
        specs = [{"test_run_id": f"TR-{i}", "title": "t"} for i in range(51)]
        with pytest.raises(ValidationError):
            self._adapter("items").validate_python(specs)

    def test_items_at_max_accepted(self) -> None:
        specs = [{"test_run_id": f"TR-{i}", "title": "t"} for i in range(50)]
        validated = self._adapter("items").validate_python(specs)
        assert isinstance(validated, list)
        assert len(validated) == 50

    def test_empty_project_id_rejected(self) -> None:
        with pytest.raises(ValidationError):
            self._adapter("project_id").validate_python("")

    def test_spec_requires_non_empty_id(self) -> None:
        with pytest.raises(ValidationError):
            TestRunUpdateSpec(test_run_id="", title="t")

    def test_spec_without_change_rejected(self) -> None:
        with pytest.raises(ValidationError, match="no effective change"):
            TestRunUpdateSpec(test_run_id="TR-1")

    def test_none_custom_values_not_effective(self) -> None:
        with pytest.raises(ValidationError, match="no effective change"):
            TestRunUpdateSpec(test_run_id="TR-1", custom_fields={"goal": None})

    def test_group_id_alone_is_effective(self) -> None:
        spec = TestRunUpdateSpec(test_run_id="TR-1", group_id="Release-1")
        assert spec.group_id == "Release-1"

    def test_unknown_key_rejected(self) -> None:
        with pytest.raises(ValidationError):
            TestRunUpdateSpec(test_run_id="TR-1", bogus="x")  # type: ignore[call-arg]


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
    ``TypeAdapter`` rebuild from signature.
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


def _detail_response() -> dict[str, object]:
    """Full single-testrun GET body covering every detail field."""
    return {
        "data": {
            "type": "testruns",
            "id": "proj1/TR-100",
            "attributes": {
                "title": "Regression 2.5",
                "type": "manual",
                "status": "open",
                "created": "2026-06-01T08:00:00Z",
                "updated": "2026-06-20T10:00:00Z",
                "finishedOn": "2026-06-19T17:30:00Z",
                "groupId": "Release-2.5",
                "isTemplate": False,
                "query": "type:testcase AND component:auth",
                "selectTestCasesBy": "dynamicQueryResult",
                "useReportFromTemplate": False,
                "homePageContent": {
                    "type": "text/html",
                    "value": '<p id="polarion_1">Run <strong>notes</strong></p>',
                },
                "myCustomField": "custom-value",
            },
            "relationships": {
                "author": {"data": {"type": "users", "id": "proj1/bob"}},
                "document": {
                    "data": {"type": "documents", "id": "proj1/Testing/Auth Plan"}
                },
                "template": {"data": {"type": "testruns", "id": "proj1/TPL-1"}},
            },
        },
        "included": [
            {"type": "users", "id": "proj1/bob", "attributes": {"name": "Bob B"}},
        ],
    }


class TestGetTestRun:
    """``get_test_run`` tool."""

    async def test_returns_test_run_detail(
        self, mock_ctx: MagicMock, mock_client: AsyncMock
    ) -> None:
        mock_client.get.return_value = _detail_response()

        result = await get_test_run(
            mock_ctx,
            project_id="proj1",
            test_run_id="TR-100",
            include_home_page_content_html=True,
        )

        assert isinstance(result, TestRunDetail)
        assert result.id == "TR-100"
        assert result.title == "Regression 2.5"
        assert result.type == "manual"
        assert result.status == "open"
        assert result.created == "2026-06-01T08:00:00Z"
        assert result.updated == "2026-06-20T10:00:00Z"
        assert result.finished_on == "2026-06-19T17:30:00Z"
        assert result.group_id == "Release-2.5"
        assert result.is_template is False
        assert result.query == "type:testcase AND component:auth"
        assert result.select_test_cases_by == "dynamicQueryResult"
        assert result.use_report_from_template is False
        assert result.project_id == "proj1"
        assert result.author_id == "bob"
        assert result.author_name == "Bob B"
        assert result.space_id == "Testing"
        assert result.document_name == "Auth Plan"
        assert result.template_id == "TPL-1"
        # Raw HTML passthrough — anchor ids survive verbatim.
        assert result.content_html == (
            '<p id="polarion_1">Run <strong>notes</strong></p>'
        )
        assert result.custom_fields == {"myCustomField": "custom-value"}

    async def test_request_params(
        self, mock_ctx: MagicMock, mock_client: AsyncMock
    ) -> None:
        mock_client.get.return_value = _detail_response()

        await get_test_run(
            mock_ctx,
            project_id="proj1",
            test_run_id="TR-100",
            include_home_page_content_html=False,
        )

        args, kwargs = mock_client.get.call_args
        assert args[0] == "/projects/proj1/testruns/TR-100"
        assert kwargs["params"]["fields[testruns]"] == "@all"
        assert kwargs["params"]["include"] == "author"
        assert kwargs["params"]["fields[users]"] == "name"

    async def test_include_home_page_content_html_false_blanks_field(
        self, mock_ctx: MagicMock, mock_client: AsyncMock
    ) -> None:
        """Flag off blank body — body still travel (``@all`` for customs)."""
        mock_client.get.return_value = _detail_response()

        result = await get_test_run(
            mock_ctx,
            project_id="proj1",
            test_run_id="TR-100",
            include_home_page_content_html=False,
        )

        assert result.content_html == ""
        # Other metadata still populated.
        assert result.title == "Regression 2.5"

    async def test_not_found_raises_value_error(
        self, mock_ctx: MagicMock, mock_client: AsyncMock
    ) -> None:
        mock_client.get.side_effect = PolarionNotFoundError(
            "Not found", status_code=404
        )

        with pytest.raises(ValueError, match="list_test_runs"):
            await get_test_run(
                mock_ctx,
                project_id="proj1",
                test_run_id="TR-404",
                include_home_page_content_html=False,
            )

    async def test_auth_error_raises_permission_error(
        self, mock_ctx: MagicMock, mock_client: AsyncMock
    ) -> None:
        mock_client.get.side_effect = PolarionAuthError("auth", status_code=401)

        with pytest.raises(PermissionError):
            await get_test_run(
                mock_ctx,
                project_id="proj1",
                test_run_id="TR-100",
                include_home_page_content_html=False,
            )

    async def test_other_error_raises_runtime_error(
        self, mock_ctx: MagicMock, mock_client: AsyncMock
    ) -> None:
        mock_client.get.side_effect = PolarionError("boom", status_code=500)

        with pytest.raises(RuntimeError, match="boom"):
            await get_test_run(
                mock_ctx,
                project_id="proj1",
                test_run_id="TR-100",
                include_home_page_content_html=False,
            )

    async def test_non_dict_data_falls_back_to_arg_id(
        self, mock_ctx: MagicMock, mock_client: AsyncMock
    ) -> None:
        mock_client.get.return_value = {"data": []}

        result = await get_test_run(
            mock_ctx,
            project_id="proj1",
            test_run_id="TR-100",
            include_home_page_content_html=False,
        )

        assert result.id == "TR-100"
