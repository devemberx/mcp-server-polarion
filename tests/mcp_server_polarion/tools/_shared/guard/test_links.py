"""Link guard tests: roles, target existence, delete-time matching."""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from mcp_server_polarion.core.exceptions import (
    PolarionAuthError,
    PolarionError,
    PolarionNotFoundError,
)
from mcp_server_polarion.models import WorkItemLinkSpec
from mcp_server_polarion.tools._shared.guard import (
    guard_hyperlink_roles,
    guard_work_item_link_roles,
    guard_work_item_link_targets,
    partition_delete_links,
)
from tests.mcp_server_polarion.tools._shared.guard._builders import (
    project_enum_response,
    workitems_response,
)


def _link(target: str, *, project: str | None = None) -> WorkItemLinkSpec:
    return WorkItemLinkSpec(
        role="relates_to", target_work_item_id=target, target_project_id=project
    )


class TestGuardWorkItemLinkTargets:
    """Target-existence guard for ``create_work_item_links`` (uncached)."""

    async def test_all_targets_exist_one_get_per_project(
        self, mock_client: AsyncMock
    ) -> None:
        mock_client.get.return_value = workitems_response("P", ["A", "B"])

        await guard_work_item_link_targets(mock_client, "P", [_link("A"), _link("B")])

        mock_client.get.assert_awaited_once()
        path, kwargs = (
            mock_client.get.call_args.args[0],
            mock_client.get.call_args.kwargs,
        )
        assert path == "/projects/P/workitems"
        assert kwargs["params"]["query"] == "id:(A B)"
        assert kwargs["params"]["fields[workitems]"] == "id"

    async def test_missing_target_raises_value_error(
        self, mock_client: AsyncMock
    ) -> None:
        mock_client.get.return_value = workitems_response("P", ["A"])

        with pytest.raises(ValueError, match="P/B") as exc:
            await guard_work_item_link_targets(
                mock_client, "P", [_link("A"), _link("B")]
            )

        assert "dangling" in str(exc.value)

    async def test_cross_project_two_gets(self, mock_client: AsyncMock) -> None:
        responses = {
            "P": workitems_response("P", ["A"]),
            "Q": workitems_response("Q", ["X"]),
        }

        async def fake_get(path: str, **kwargs: object) -> dict[str, object]:
            project = path.split("/")[2]
            return responses[project]

        mock_client.get.side_effect = fake_get

        await guard_work_item_link_targets(
            mock_client, "P", [_link("A"), _link("X", project="Q")]
        )

        assert mock_client.get.await_count == 2

    async def test_missing_in_cross_project_is_caught(
        self, mock_client: AsyncMock
    ) -> None:
        async def fake_get(path: str, **kwargs: object) -> dict[str, object]:
            project = path.split("/")[2]
            return workitems_response(project, ["A"] if project == "P" else [])

        mock_client.get.side_effect = fake_get

        with pytest.raises(ValueError, match="Q/X"):
            await guard_work_item_link_targets(
                mock_client, "P", [_link("A"), _link("X", project="Q")]
            )

    async def test_chunks_above_page_size(self, mock_client: AsyncMock) -> None:
        ids = sorted(f"WI-{n}" for n in range(150))

        async def fake_get(path: str, **kwargs: object) -> dict[str, object]:
            query = str(kwargs["params"]["query"])  # type: ignore[index]
            chunk = query.removeprefix("id:(").removesuffix(")").split()
            return workitems_response("P", chunk)

        mock_client.get.side_effect = fake_get

        await guard_work_item_link_targets(mock_client, "P", [_link(i) for i in ids])

        assert mock_client.get.await_count == 2
        queries = [
            str(call.kwargs["params"]["query"])
            for call in mock_client.get.await_args_list
        ]
        assert [q.count(" ") + 1 for q in queries] == [100, 50]

    async def test_unreachable_backend_blocks_write(
        self, mock_client: AsyncMock
    ) -> None:
        mock_client.get.side_effect = PolarionError("backend down")

        with pytest.raises(RuntimeError, match="Refusing the write"):
            await guard_work_item_link_targets(mock_client, "P", [_link("A")])

    async def test_auth_error_raises_permission_error(
        self, mock_client: AsyncMock
    ) -> None:
        mock_client.get.side_effect = PolarionAuthError("forbidden", status_code=403)

        with pytest.raises(PermissionError, match="lacks permission"):
            await guard_work_item_link_targets(mock_client, "P", [_link("A")])

    async def test_missing_target_project_raises_value_error(
        self, mock_client: AsyncMock
    ) -> None:
        mock_client.get.side_effect = PolarionNotFoundError("no such project")

        with pytest.raises(ValueError, match="P/A"):
            await guard_work_item_link_targets(mock_client, "P", [_link("A")])


class TestGuardWorkItemLinkRoles:
    """Link-role guard for ``create_work_item_links``."""

    async def test_valid_role_passes(self, mock_client: AsyncMock) -> None:
        mock_client.get.return_value = project_enum_response(
            "workitem-link-role", ["parent", "relates_to"]
        )

        await guard_work_item_link_roles(mock_client, "P", ["relates_to", "parent"])

    async def test_unknown_role_raises_with_options(
        self, mock_client: AsyncMock
    ) -> None:
        mock_client.get.return_value = project_enum_response(
            "workitem-link-role", ["parent", "relates_to"]
        )

        with pytest.raises(ValueError, match="ghost_role") as exc:
            await guard_work_item_link_roles(mock_client, "P", ["ghost_role"])

        assert "relates_to" in str(exc.value)

    async def test_dedup_one_get_for_repeated_roles(
        self, mock_client: AsyncMock
    ) -> None:
        mock_client.get.return_value = project_enum_response(
            "workitem-link-role", ["parent"]
        )

        await guard_work_item_link_roles(mock_client, "P", ["parent", "parent"])

        mock_client.get.assert_awaited_once()

    async def test_empty_roles_skip_check(self, mock_client: AsyncMock) -> None:
        await guard_work_item_link_roles(mock_client, "P", [])

        mock_client.get.assert_not_awaited()

    async def test_empty_option_set_defers(self, mock_client: AsyncMock) -> None:
        mock_client.get.side_effect = PolarionNotFoundError("nope", status_code=404)

        await guard_work_item_link_roles(mock_client, "P", ["anything"])

    async def test_unreachable_backend_blocks_write(
        self, mock_client: AsyncMock
    ) -> None:
        mock_client.get.side_effect = PolarionError("backend down")

        with pytest.raises(RuntimeError, match="Refusing the write"):
            await guard_work_item_link_roles(mock_client, "P", ["relates_to"])

    async def test_auth_error_raises_permission_error(
        self, mock_client: AsyncMock
    ) -> None:
        mock_client.get.side_effect = PolarionAuthError("forbidden", status_code=403)

        with pytest.raises(PermissionError, match="lacks permission"):
            await guard_work_item_link_roles(mock_client, "P", ["relates_to"])


class TestGuardHyperlinkRoles:
    """Hyperlink-role guard for ``create_work_items`` / ``update_work_items``."""

    async def test_valid_role_passes(self, mock_client: AsyncMock) -> None:
        mock_client.get.return_value = project_enum_response(
            "hyperlink-role", ["ref_int", "ref_ext"]
        )

        await guard_hyperlink_roles(mock_client, "P", ["ref_ext"])

    async def test_unknown_role_raises_with_options(
        self, mock_client: AsyncMock
    ) -> None:
        mock_client.get.return_value = project_enum_response(
            "hyperlink-role", ["ref_int", "ref_ext"]
        )

        with pytest.raises(ValueError, match="ghost") as exc:
            await guard_hyperlink_roles(mock_client, "P", ["ghost"])

        assert "ref_int" in str(exc.value)

    async def test_empty_roles_skip_check(self, mock_client: AsyncMock) -> None:
        await guard_hyperlink_roles(mock_client, "P", [])

        mock_client.get.assert_not_awaited()


def _linkedworkitems_response(composite_ids: list[str]) -> dict[str, object]:
    """A JSON:API forward-link page; ids are the 5-segment composite form."""
    return {
        "data": [{"type": "linkedworkitems", "id": cid} for cid in composite_ids],
        "meta": {"totalCount": len(composite_ids)},
    }


class TestPartitionDeleteLinks:
    """Pre-read + matched/no-op split for ``delete_work_item_links``."""

    async def test_splits_matched_and_not_found_preserving_order(
        self, mock_client: AsyncMock
    ) -> None:
        mock_client.get.return_value = _linkedworkitems_response(
            ["P/MCPT-1/parent/P/MCPT-2", "P/MCPT-1/relates_to/P/MCPT-9"]
        )

        matched, not_found = await partition_delete_links(
            mock_client,
            "P",
            "MCPT-1",
            [
                "P/MCPT-1/relates_to/P/MCPT-9",
                "P/MCPT-1/verifies/P/MCPT-3",
                "P/MCPT-1/parent/P/MCPT-2",
            ],
        )

        assert matched == [
            "P/MCPT-1/relates_to/P/MCPT-9",
            "P/MCPT-1/parent/P/MCPT-2",
        ]
        assert not_found == ["P/MCPT-1/verifies/P/MCPT-3"]

    async def test_reads_only_id_field(self, mock_client: AsyncMock) -> None:
        mock_client.get.return_value = _linkedworkitems_response([])

        await partition_delete_links(
            mock_client, "P", "MCPT-1", ["P/MCPT-1/parent/P/MCPT-2"]
        )

        path, kwargs = (
            mock_client.get.call_args.args[0],
            mock_client.get.call_args.kwargs,
        )
        assert path == "/projects/P/workitems/MCPT-1/linkedworkitems"
        assert kwargs["params"]["fields[linkedworkitems]"] == "id"

    async def test_paginates_above_page_size(self, mock_client: AsyncMock) -> None:
        full = [f"P/MCPT-1/relates_to/P/WI-{n}" for n in range(100)]
        tail = ["P/MCPT-1/relates_to/P/WI-100"]

        async def fake_get(path: str, **kwargs: object) -> dict[str, object]:
            page = kwargs["params"]["page[number]"]  # type: ignore[index]
            return _linkedworkitems_response(full if page == 1 else tail)

        mock_client.get.side_effect = fake_get

        matched, not_found = await partition_delete_links(
            mock_client,
            "P",
            "MCPT-1",
            ["P/MCPT-1/relates_to/P/WI-100"],
        )

        assert mock_client.get.await_count == 2
        assert matched == ["P/MCPT-1/relates_to/P/WI-100"]
        assert not_found == []

    async def test_source_wi_404_raises_value_error(
        self, mock_client: AsyncMock
    ) -> None:
        mock_client.get.side_effect = PolarionNotFoundError("not found")

        with pytest.raises(ValueError, match="Source work item 'MCPT-1' not found"):
            await partition_delete_links(
                mock_client,
                "P",
                "MCPT-1",
                ["P/MCPT-1/parent/P/MCPT-2"],
            )

    async def test_auth_error_raises_permission_error(
        self, mock_client: AsyncMock
    ) -> None:
        mock_client.get.side_effect = PolarionAuthError("auth")

        with pytest.raises(PermissionError):
            await partition_delete_links(
                mock_client,
                "P",
                "MCPT-1",
                ["P/MCPT-1/parent/P/MCPT-2"],
            )

    async def test_unreachable_backend_blocks_with_runtime_error(
        self, mock_client: AsyncMock
    ) -> None:
        mock_client.get.side_effect = PolarionError("backend down")

        with pytest.raises(RuntimeError, match="Refusing the delete"):
            await partition_delete_links(
                mock_client,
                "P",
                "MCPT-1",
                ["P/MCPT-1/parent/P/MCPT-2"],
            )
