"""Tests for the work item link tools."""

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
    PaginatedResult,
    WorkItemLink,
    WorkItemLinkRef,
    WorkItemLinksCreateResult,
    WorkItemLinksDeleteResult,
    WorkItemLinkSpec,
    WorkItemLinkUpdateResult,
    WorkItemLinkUpdateSpec,
)
from mcp_server_polarion.server import mcp
from mcp_server_polarion.tools.links import (
    _build_create_links_payload,
    _build_delete_links_payload,
    _build_update_link_payload,
    _extract_created_link_ids,
    create_work_item_links,
    delete_work_item_links,
    list_work_item_links,
    update_work_item_link,
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


def _echo_targets_exist(
    path: str, *, params: dict[str, object] | None = None, **_: object
) -> dict[str, object]:
    """Guard GET stub: report every id in the ``id:(...)`` query as existing."""
    project = path.split("/")[2]
    query = str((params or {}).get("query", ""))
    ids = query.removeprefix("id:(").removesuffix(")").split()
    return {"data": [{"type": "workitems", "id": f"{project}/{i}"} for i in ids]}


def _forward_links_response(composite_ids: list[str]) -> dict[str, object]:
    """A JSON:API forward-link page used by the delete pre-read."""
    return {
        "data": [{"type": "linkedworkitems", "id": cid} for cid in composite_ids],
        "meta": {"totalCount": len(composite_ids)},
    }


class TestListWorkItemLinks:
    """Tests for the ``list_work_item_links`` tool.

    Each call returns a single page in a single direction (``forward`` or
    ``back``). Pagination matches the convention used by other list tools.
    """

    async def test_forward_returns_paginated_result(
        self, mock_ctx: MagicMock, mock_client: AsyncMock
    ) -> None:
        mock_client.get.return_value = {
            "data": [
                {
                    "id": "proj1/MCPT-001/parent/proj1/MCPT-010",
                    "attributes": {"role": "parent", "suspect": False},
                    "relationships": {
                        "workItem": {
                            "data": {
                                "type": "workitems",
                                "id": "proj1/MCPT-010",
                            }
                        },
                    },
                },
            ],
            "included": [
                {
                    "type": "workitems",
                    "id": "proj1/MCPT-010",
                    "attributes": {
                        "title": "Parent Item",
                        "type": "heading",
                        "status": "open",
                    },
                    "relationships": {
                        "module": {
                            "data": {
                                "type": "documents",
                                "id": "proj1/Design/SRS",
                            }
                        },
                    },
                },
            ],
            "meta": {"totalCount": 1},
        }

        result = await list_work_item_links(
            mock_ctx,
            project_id="proj1",
            work_item_id="MCPT-001",
            direction="forward",
            page_size=100,
            page_number=1,
        )

        assert isinstance(result, PaginatedResult)
        assert result.total_count == 1
        assert result.page == 1
        assert result.page_size == 100
        assert result.has_more is False
        assert len(result.items) == 1

        fwd = result.items[0]
        assert isinstance(fwd, WorkItemLink)
        assert fwd.direction == "forward"
        assert fwd.id == "MCPT-010"
        assert fwd.role == "parent"
        assert fwd.title == "Parent Item"
        assert fwd.suspect is False
        assert fwd.type == "heading"
        assert fwd.status == "open"
        assert fwd.space_id == "Design"
        assert fwd.document_name == "SRS"

    async def test_forward_signals_has_more_when_total_exceeds_page(
        self, mock_ctx: MagicMock, mock_client: AsyncMock
    ) -> None:
        # Simulate page 1 of a 5-item set with page_size=2 — server returns
        # the first 2 items, meta.totalCount reports the full collection
        # size, and has_more should be True (2 * 1 < 5).
        mock_client.get.return_value = {
            "data": [
                {
                    "id": f"proj1/MCPT-001/parent/proj1/MCPT-{i:03d}",
                    "attributes": {"role": "parent", "suspect": False},
                    "relationships": {
                        "workItem": {
                            "data": {
                                "type": "workitems",
                                "id": f"proj1/MCPT-{i:03d}",
                            }
                        },
                    },
                }
                for i in range(2)
            ],
            "included": [
                {
                    "type": "workitems",
                    "id": f"proj1/MCPT-{i:03d}",
                    "attributes": {
                        "title": f"work item {i}",
                        "type": "requirement",
                        "status": "open",
                    },
                }
                for i in range(2)
            ],
            "meta": {"totalCount": 5},
        }

        result = await list_work_item_links(
            mock_ctx,
            project_id="proj1",
            work_item_id="MCPT-001",
            direction="forward",
            page_size=2,
            page_number=1,
        )

        assert result.total_count == 5
        assert result.page == 1
        assert result.page_size == 2
        assert result.has_more is True
        assert len(result.items) == 2

    async def test_forward_passes_pagination_params(
        self, mock_ctx: MagicMock, mock_client: AsyncMock
    ) -> None:
        mock_client.get.return_value = {"data": []}

        await list_work_item_links(
            mock_ctx,
            project_id="proj1",
            work_item_id="MCPT-001",
            direction="forward",
            page_size=25,
            page_number=3,
        )

        calls = mock_client.get.call_args_list
        assert len(calls) == 1
        assert calls[0][0][0] == "/projects/proj1/workitems/MCPT-001/linkedworkitems"
        params = calls[0][1]["params"]
        assert params["fields[linkedworkitems]"] == "@all"
        assert params["include"] == "workItem"
        assert params["page[size]"] == 25
        assert params["page[number]"] == 3

    async def test_back_returns_paginated_result(
        self, mock_ctx: MagicMock, mock_client: AsyncMock
    ) -> None:
        mock_client.get.return_value = {
            "data": [
                {
                    "id": "proj1/MCPT-020",
                    "attributes": {
                        "title": "Related Item",
                        "type": "requirement",
                        "status": "draft",
                    },
                    "relationships": {
                        "module": {
                            "data": {
                                "type": "documents",
                                "id": "proj1/Requirements/SysRS",
                            }
                        },
                    },
                },
                {
                    "id": "proj1/MCPT-030",
                    "attributes": {
                        "title": "Verifier Item",
                        "type": "testCase",
                        "status": "approved",
                    },
                },
            ],
            "meta": {"totalCount": 2},
        }

        result = await list_work_item_links(
            mock_ctx,
            project_id="proj1",
            work_item_id="MCPT-001",
            direction="back",
            page_size=100,
            page_number=1,
        )

        assert isinstance(result, PaginatedResult)
        assert result.total_count == 2
        assert result.has_more is False
        assert len(result.items) == 2

        back_by_id = {item.id: item for item in result.items}
        assert set(back_by_id) == {"MCPT-020", "MCPT-030"}
        for item in result.items:
            assert item.direction == "back"
            assert item.role is None
            assert item.suspect is False
        assert back_by_id["MCPT-020"].type == "requirement"
        assert back_by_id["MCPT-020"].space_id == "Requirements"
        assert back_by_id["MCPT-020"].document_name == "SysRS"
        assert back_by_id["MCPT-030"].space_id == ""

    async def test_back_passes_pagination_params(
        self, mock_ctx: MagicMock, mock_client: AsyncMock
    ) -> None:
        mock_client.get.return_value = {"data": []}

        await list_work_item_links(
            mock_ctx,
            project_id="proj1",
            work_item_id="MCPT-001",
            direction="back",
            page_size=10,
            page_number=2,
        )

        calls = mock_client.get.call_args_list
        assert len(calls) == 1
        assert calls[0][0][0] == "/projects/proj1/workitems"
        params = calls[0][1]["params"]
        assert params["query"] == "linkedWorkItems:MCPT-001"
        assert params["page[size]"] == 10
        assert params["page[number]"] == 2

    async def test_no_links_returns_empty_page(
        self, mock_ctx: MagicMock, mock_client: AsyncMock
    ) -> None:
        mock_client.get.return_value = {"data": []}

        result = await list_work_item_links(
            mock_ctx,
            project_id="proj1",
            work_item_id="MCPT-001",
            direction="forward",
            page_size=100,
            page_number=1,
        )

        assert result.items == []
        assert result.total_count == 0
        assert result.has_more is False

    async def test_forward_not_found_raises_value_error(
        self, mock_ctx: MagicMock, mock_client: AsyncMock
    ) -> None:
        mock_client.get.side_effect = PolarionNotFoundError(
            "Not found",
            status_code=404,
        )

        with pytest.raises(ValueError, match="not found"):
            await list_work_item_links(
                mock_ctx,
                project_id="proj1",
                work_item_id="MCPT-999",
                direction="forward",
                page_size=100,
                page_number=1,
            )

    async def test_back_not_found_raises_value_error(
        self, mock_ctx: MagicMock, mock_client: AsyncMock
    ) -> None:
        mock_client.get.side_effect = PolarionNotFoundError(
            "Not found",
            status_code=404,
        )

        with pytest.raises(ValueError, match="not found"):
            await list_work_item_links(
                mock_ctx,
                project_id="proj1",
                work_item_id="MCPT-999",
                direction="back",
                page_size=100,
                page_number=1,
            )

    async def test_auth_error_raises_permission_error(
        self, mock_ctx: MagicMock, mock_client: AsyncMock
    ) -> None:
        mock_client.get.side_effect = PolarionAuthError(
            "Forbidden",
            status_code=403,
        )

        with pytest.raises(PermissionError, match="POLARION_TOKEN"):
            await list_work_item_links(
                mock_ctx,
                project_id="proj1",
                work_item_id="MCPT-001",
                direction="forward",
                page_size=100,
                page_number=1,
            )

    async def test_back_polarion_error_raises_runtime_error(
        self, mock_ctx: MagicMock, mock_client: AsyncMock
    ) -> None:
        mock_client.get.side_effect = PolarionError(
            "Boom",
            status_code=500,
        )

        with pytest.raises(RuntimeError, match="Backlink query failed"):
            await list_work_item_links(
                mock_ctx,
                project_id="proj1",
                work_item_id="MCPT-001",
                direction="back",
                page_size=100,
                page_number=1,
            )

    async def test_direction_default_is_forward_in_registered_schema(
        self,
    ) -> None:
        """Guard the JSON-Schema default for ``direction``.

        Direct invocation cannot exercise FastMCP's default-injection
        (passing the ``Field(...)`` sentinel through), so the registered
        tool schema is the authoritative place to verify the default
        the LLM will see. Regressions here would silently break the
        zero-arg call path.
        """
        tools = await mcp.list_tools()
        tool = next(t for t in tools if t.name == "list_work_item_links")
        direction_schema = tool.parameters["properties"]["direction"]
        assert direction_schema["default"] == "forward"
        assert direction_schema["enum"] == ["forward", "back"]

    @pytest.mark.parametrize(
        "bad_id",
        [
            "MCPT-1 OR title:*",
            "MCPT-1:foo",
            "*MCPT*",
            "MCPT-1\\",
            "MCPT-1/MCPT-2",
            "MCPT 1",
            "",
        ],
    )
    async def test_back_direction_rejects_lucene_metacharacters(
        self,
        mock_ctx: MagicMock,
        mock_client: AsyncMock,
        bad_id: str,
    ) -> None:
        """Back-link Lucene query is built from the raw id; bad chars must abort."""
        with pytest.raises(ValueError, match="Lucene"):
            await list_work_item_links(
                mock_ctx,
                project_id="proj1",
                work_item_id=bad_id,
                direction="back",
                page_size=10,
                page_number=1,
            )
        mock_client.get.assert_not_called()


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


class TestCreateWorkItemLinksRoleGuard:
    """The link-role guard runs after the target guard, before the write."""

    @staticmethod
    def _stub(valid_roles: list[str]) -> object:
        """GET stub: echo targets as existing, serve the role enumeration."""

        def fake_get(
            path: str, *, params: dict[str, object] | None = None, **_: object
        ) -> dict[str, object]:
            if "/enumerations/" in path:
                return _project_enum_get_response("workitem-link-role", valid_roles)
            return _echo_targets_exist(path, params=params)

        return fake_get

    async def test_unknown_role_raises_without_post(
        self, mock_ctx: MagicMock, mock_client: AsyncMock
    ) -> None:
        mock_client.get.side_effect = self._stub(["parent", "relates_to"])

        with pytest.raises(ValueError, match="ghost_role") as exc:
            await create_work_item_links(
                mock_ctx,
                project_id="MyProj",
                work_item_id="MCPT-1",
                links=[
                    WorkItemLinkSpec(role="ghost_role", target_work_item_id="MCPT-2")
                ],
                dry_run=True,
            )

        assert "relates_to" in str(exc.value)
        mock_client.post.assert_not_called()

    async def test_valid_role_proceeds_to_post(
        self, mock_ctx: MagicMock, mock_client: AsyncMock
    ) -> None:
        mock_client.get.side_effect = self._stub(["parent", "relates_to"])
        mock_client.post.return_value = {
            "data": [
                {"type": "linkedworkitems", "id": "MyProj/MCPT-1/parent/MyProj/MCPT-2"}
            ]
        }

        result = await create_work_item_links(
            mock_ctx,
            project_id="MyProj",
            work_item_id="MCPT-1",
            links=[WorkItemLinkSpec(role="parent", target_work_item_id="MCPT-2")],
            dry_run=False,
        )

        assert result.created is True
        mock_client.post.assert_called_once()

    async def test_target_guard_runs_before_role_guard(
        self, mock_ctx: MagicMock, mock_client: AsyncMock
    ) -> None:
        # Missing target AND unknown role: the target guard runs first, so its
        # error surfaces and the role enumeration is never fetched.
        def fake_get(
            path: str, *, params: dict[str, object] | None = None, **_: object
        ) -> dict[str, object]:
            if "/enumerations/" in path:
                raise AssertionError("role guard ran before the target guard")
            return {"data": []}

        mock_client.get.side_effect = fake_get

        with pytest.raises(ValueError, match="MyProj/MCPT-2"):
            await create_work_item_links(
                mock_ctx,
                project_id="MyProj",
                work_item_id="MCPT-1",
                links=[
                    WorkItemLinkSpec(role="ghost_role", target_work_item_id="MCPT-2")
                ],
                dry_run=True,
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

        with pytest.raises(RuntimeError, match="Refusing the delete"):
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
