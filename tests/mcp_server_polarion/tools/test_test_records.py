"""Test record tools — list/get parsing and param forwarding, bulk
create/update payloads and guards, error mapping, field bounds.
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
    TestRecordCreateSpec,
    TestRecordDetail,
    TestRecordUpdateSpec,
)
from mcp_server_polarion.tools._shared.fields import TEST_RECORD_DETAIL_FIELDS
from mcp_server_polarion.tools.test_records import (
    _build_create_test_records_payload,
    _build_test_record_resource,
    _build_update_test_records_payload,
    create_test_records,
    get_test_record,
    list_test_records,
    update_test_records,
)
from tests.mcp_server_polarion.tools._shared.guard._builders import (
    project_enum_response,
    workitems_response,
)


class TestListTestRecords:
    """``list_test_records`` tool."""

    async def test_returns_test_records(
        self, mock_ctx: MagicMock, mock_client: AsyncMock
    ) -> None:
        mock_client.get.return_value = {
            "data": [
                {
                    "type": "testrecords",
                    "id": "proj1/TR-001/proj1/TC-42/0",
                    "attributes": {
                        "executed": "2026-06-01T10:00:00Z",
                        "duration": 12.5,
                        "result": "failed",
                        "iteration": 0,
                    },
                    "relationships": {
                        "testCase": {
                            "data": {"type": "workitems", "id": "proj1/TC-42"}
                        },
                        "executedBy": {
                            "data": {"type": "users", "id": "proj1/devemberx"}
                        },
                        "defect": {"data": {"type": "workitems", "id": "proj1/DEF-7"}},
                    },
                },
                {
                    "type": "testrecords",
                    "id": "proj1/TR-001/proj1/TC-43/1",
                    "attributes": {"iteration": 1},
                    "relationships": {
                        "testCase": {
                            "data": {"type": "workitems", "id": "proj1/TC-43"}
                        },
                    },
                },
            ],
            "included": [
                {
                    "type": "users",
                    "id": "proj1/devemberx",
                    "attributes": {"name": "Devember X"},
                }
            ],
            # Live endpoint omit meta.totalCount -- total falls back to
            # offset estimate.
        }

        result = await list_test_records(
            mock_ctx,
            project_id="proj1",
            test_run_id="TR-001",
            result=None,
            page_size=100,
            page_number=1,
        )

        assert isinstance(result, PaginatedResult)
        assert len(result.items) == 2
        assert result.total_count == 2

        first = result.items[0]
        # Full work-item ids preserved -- never derived from 5-segment record id.
        assert first.id == "proj1/TR-001/proj1/TC-42/0"
        assert first.test_case_id == "proj1/TC-42"
        assert first.iteration == 0
        assert first.result == "failed"
        assert first.executed == "2026-06-01T10:00:00Z"
        assert first.duration == 12.5
        assert first.executed_by_name == "Devember X"
        assert first.defect_id == "proj1/DEF-7"

        second = result.items[1]
        # Not-yet-executed record -> blanks and zeros.
        assert second.test_case_id == "proj1/TC-43"
        assert second.iteration == 1
        assert second.result == ""
        assert second.executed == ""
        assert second.duration == 0.0
        assert second.executed_by_name == ""
        assert second.defect_id == ""

    async def test_sparse_fieldset_and_includes_requested(
        self, mock_ctx: MagicMock, mock_client: AsyncMock
    ) -> None:
        mock_client.get.return_value = {"data": []}

        await list_test_records(
            mock_ctx,
            project_id="proj1",
            test_run_id="TR-001",
            result=None,
            page_size=100,
            page_number=1,
        )

        args, kwargs = mock_client.get.call_args
        assert args[0] == "/projects/proj1/testruns/TR-001/testrecords"
        params = kwargs["params"]
        # Sparse fieldset drop relationships -- all three named explicit.
        assert "testCase" in params["fields[testrecords]"]
        assert "executedBy" in params["fields[testrecords]"]
        assert "defect" in params["fields[testrecords]"]
        assert params["include"] == "executedBy"
        assert params["fields[users]"] == "name"

    async def test_result_filter_forwarded(
        self, mock_ctx: MagicMock, mock_client: AsyncMock
    ) -> None:
        mock_client.get.return_value = {"data": []}

        await list_test_records(
            mock_ctx,
            project_id="proj1",
            test_run_id="TR-001",
            result="failed",
            page_size=100,
            page_number=1,
        )

        _, kwargs = mock_client.get.call_args
        assert kwargs["params"]["testResultId"] == "failed"

    async def test_result_none_omits_param(
        self, mock_ctx: MagicMock, mock_client: AsyncMock
    ) -> None:
        mock_client.get.return_value = {"data": []}

        await list_test_records(
            mock_ctx,
            project_id="proj1",
            test_run_id="TR-001",
            result=None,
            page_size=100,
            page_number=1,
        )

        _, kwargs = mock_client.get.call_args
        assert "testResultId" not in kwargs["params"]

    async def test_run_not_found_raises_value_error(
        self, mock_ctx: MagicMock, mock_client: AsyncMock
    ) -> None:
        mock_client.get.side_effect = PolarionNotFoundError(
            "Not found", status_code=404
        )

        with pytest.raises(ValueError, match="not found"):
            await list_test_records(
                mock_ctx,
                project_id="proj1",
                test_run_id="missing",
                result=None,
                page_size=100,
                page_number=1,
            )

    async def test_auth_error_raises_permission_error(
        self, mock_ctx: MagicMock, mock_client: AsyncMock
    ) -> None:
        mock_client.get.side_effect = PolarionAuthError("auth", status_code=401)

        with pytest.raises(PermissionError):
            await list_test_records(
                mock_ctx,
                project_id="proj1",
                test_run_id="TR-001",
                result=None,
                page_size=100,
                page_number=1,
            )

    async def test_other_error_raises_runtime_error(
        self, mock_ctx: MagicMock, mock_client: AsyncMock
    ) -> None:
        mock_client.get.side_effect = PolarionError("boom", status_code=500)

        with pytest.raises(RuntimeError, match="boom"):
            await list_test_records(
                mock_ctx,
                project_id="proj1",
                test_run_id="TR-001",
                result=None,
                page_size=100,
                page_number=1,
            )


class TestListTestRecordsFieldValidation:
    """``page_size`` bounds — direct calls bypass JSON Schema; proven via
    ``TypeAdapter`` rebuild from signature.
    """

    @staticmethod
    def _adapter(param_name: str) -> TypeAdapter[object]:
        hints = get_type_hints(list_test_records)
        sig = inspect.signature(list_test_records)
        field_info = sig.parameters[param_name].default
        return TypeAdapter(Annotated[hints[param_name], field_info])

    def test_page_size_boundaries_accepted(self) -> None:
        adapter = self._adapter("page_size")
        assert adapter.validate_python(1) == 1
        assert adapter.validate_python(100) == 100

    def test_page_size_above_max_rejected(self) -> None:
        with pytest.raises(ValidationError):
            self._adapter("page_size").validate_python(101)

    def test_page_number_below_min_rejected(self) -> None:
        with pytest.raises(ValidationError):
            self._adapter("page_number").validate_python(0)


class TestBuildUpdateTestRecordsPayload:
    """``_build_update_test_records_payload`` unit seam."""

    def test_full_spec_builds_attributes(self) -> None:
        spec = TestRecordUpdateSpec(
            record_id="proj1/TR-1/proj1/WI-1/0",
            result="passed",
            comment="Looks good",
            comment_format="text/html",
            defect_id="proj1/WI-9",
        )

        payload = _build_update_test_records_payload(project_id="proj1", specs=[spec])

        data = payload["data"]
        assert isinstance(data, list)
        resource = data[0]
        assert isinstance(resource, dict)
        assert resource["type"] == "testrecords"
        assert resource["id"] == "proj1/TR-1/proj1/WI-1/0"
        assert resource["attributes"] == {
            "result": "passed",
            "comment": {"type": "text/html", "value": "Looks good"},
        }
        assert resource["relationships"] == {
            "defect": {"data": {"type": "workitems", "id": "proj1/WI-9"}}
        }

    def test_partial_spec_skips_unset(self) -> None:
        spec = TestRecordUpdateSpec(
            record_id="proj1/TR-1/proj1/WI-1/0", result="failed"
        )

        payload = _build_update_test_records_payload(project_id="proj1", specs=[spec])

        data = payload["data"]
        assert isinstance(data, list)
        resource = data[0]
        assert isinstance(resource, dict)
        assert resource["attributes"] == {"result": "failed"}
        assert "relationships" not in resource

    def test_multiple_specs_keep_order(self) -> None:
        specs = [
            TestRecordUpdateSpec(record_id="proj1/TR-1/proj1/WI-1/0", result="passed"),
            TestRecordUpdateSpec(record_id="proj1/TR-1/proj1/WI-2/0", result="failed"),
        ]

        payload = _build_update_test_records_payload(project_id="proj1", specs=specs)

        data = payload["data"]
        assert isinstance(data, list)
        ids = [entry["id"] for entry in data if isinstance(entry, dict)]
        assert ids == ["proj1/TR-1/proj1/WI-1/0", "proj1/TR-1/proj1/WI-2/0"]

    def test_comment_only_spec_uses_own_format(self) -> None:
        spec = TestRecordUpdateSpec(record_id="proj1/TR-1/proj1/WI-1/0", comment="Note")

        payload = _build_update_test_records_payload(project_id="proj1", specs=[spec])

        data = payload["data"]
        assert isinstance(data, list)
        resource = data[0]
        assert isinstance(resource, dict)
        assert resource["attributes"] == {
            "comment": {"type": "text/plain", "value": "Note"}
        }

    def test_defect_only_spec_omits_empty_attributes(self) -> None:
        # Live-verified: attributes key absent -- 204, defect store, prior
        # result keep.
        spec = TestRecordUpdateSpec(
            record_id="proj1/TR-1/proj1/WI-1/0", defect_id="proj1/WI-9"
        )

        payload = _build_update_test_records_payload(project_id="proj1", specs=[spec])

        data = payload["data"]
        assert isinstance(data, list)
        resource = data[0]
        assert isinstance(resource, dict)
        assert "attributes" not in resource
        assert resource["relationships"] == {
            "defect": {"data": {"type": "workitems", "id": "proj1/WI-9"}}
        }

    def test_bare_defect_id_qualified_with_project(self) -> None:
        # Bare id pass guard (project fallback) yet store dangling
        # unqualified -- payload must carry qualified 2-segment id.
        spec = TestRecordUpdateSpec(
            record_id="proj1/TR-1/proj1/WI-1/0", defect_id="WI-9"
        )

        payload = _build_update_test_records_payload(project_id="proj1", specs=[spec])

        data = payload["data"]
        assert isinstance(data, list)
        resource = data[0]
        assert isinstance(resource, dict)
        assert resource["relationships"] == {
            "defect": {"data": {"type": "workitems", "id": "proj1/WI-9"}}
        }


def _result_enum_response(options: list[str]) -> dict[str, object]:
    """``testing/test-result`` enumeration GET body."""
    return {
        "data": {
            "type": "enumerations",
            "id": "test-result",
            "attributes": {"options": [{"id": option} for option in options]},
        }
    }


def _workitems_response(project_id: str, ids: list[str]) -> dict[str, object]:
    """Existence-check GET body for ``/projects/{p}/workitems``."""
    return {"data": [{"type": "workitems", "id": f"{project_id}/{wi}"} for wi in ids]}


class TestUpdateTestRecords:
    """``update_test_records`` tool."""

    async def test_duplicate_ids_rejected_before_any_request(
        self, mock_ctx: MagicMock, mock_client: AsyncMock
    ) -> None:
        records = [
            TestRecordUpdateSpec(record_id="proj1/TR-1/proj1/WI-1/0", result="passed"),
            TestRecordUpdateSpec(record_id="proj1/TR-1/proj1/WI-1/0", result="failed"),
        ]

        with pytest.raises(ValueError, match=r"Duplicate record_id\(s\)"):
            await update_test_records(
                mock_ctx,
                project_id="proj1",
                test_run_id="TR-1",
                items=records,
                dry_run=False,
            )

        mock_client.get.assert_not_awaited()
        mock_client.patch.assert_not_awaited()

    async def test_minimal_update_patches_and_returns_ids(
        self, mock_ctx: MagicMock, mock_client: AsyncMock
    ) -> None:
        mock_client.patch.return_value = None

        result = await update_test_records(
            mock_ctx,
            project_id="proj1",
            test_run_id="TR-1",
            items=[
                TestRecordUpdateSpec(
                    record_id="proj1/TR-1/proj1/WI-1/0", comment="Looks fine"
                )
            ],
            dry_run=False,
        )

        assert result.updated is True
        assert result.dry_run is False
        assert result.record_ids == ["proj1/TR-1/proj1/WI-1/0"]
        assert result.payload_preview is None
        # Comment-only update needs zero guard traffic.
        mock_client.get.assert_not_awaited()
        args, kwargs = mock_client.patch.await_args
        assert args[0] == "/projects/proj1/testruns/TR-1/testrecords"
        data = kwargs["json"]["data"]
        assert data[0]["id"] == "proj1/TR-1/proj1/WI-1/0"

    async def test_dry_run_returns_payload_without_patching(
        self, mock_ctx: MagicMock, mock_client: AsyncMock
    ) -> None:
        result = await update_test_records(
            mock_ctx,
            project_id="proj1",
            test_run_id="TR-1",
            items=[
                TestRecordUpdateSpec(
                    record_id="proj1/TR-1/proj1/WI-1/0", comment="Preview me"
                )
            ],
            dry_run=True,
        )

        assert result.updated is False
        assert result.dry_run is True
        assert result.record_ids == []
        assert result.payload_preview is not None
        data = result.payload_preview["data"]
        assert isinstance(data, list)
        mock_client.patch.assert_not_awaited()

    async def test_dry_run_still_runs_guards(
        self, mock_ctx: MagicMock, mock_client: AsyncMock
    ) -> None:
        mock_client.get.return_value = _result_enum_response(["passed", "failed"])

        with pytest.raises(ValueError, match="result"):
            await update_test_records(
                mock_ctx,
                project_id="proj1",
                test_run_id="TR-1",
                items=[
                    TestRecordUpdateSpec(
                        record_id="proj1/TR-1/proj1/WI-1/0", result="ghost"
                    )
                ],
                dry_run=True,
            )

        mock_client.patch.assert_not_awaited()

    async def test_unknown_result_blocks_before_patch(
        self, mock_ctx: MagicMock, mock_client: AsyncMock
    ) -> None:
        mock_client.get.return_value = _result_enum_response(["passed", "failed"])

        with pytest.raises(ValueError, match=r"items\[0\].*result"):
            await update_test_records(
                mock_ctx,
                project_id="proj1",
                test_run_id="TR-1",
                items=[
                    TestRecordUpdateSpec(
                        record_id="proj1/TR-1/proj1/WI-1/0", result="ghost"
                    )
                ],
                dry_run=False,
            )

        mock_client.patch.assert_not_awaited()

    async def test_defect_guard_blocks_before_patch(
        self, mock_ctx: MagicMock, mock_client: AsyncMock
    ) -> None:
        mock_client.get.return_value = _workitems_response("proj1", [])

        with pytest.raises(ValueError, match="proj1/WI-9"):
            await update_test_records(
                mock_ctx,
                project_id="proj1",
                test_run_id="TR-1",
                items=[
                    TestRecordUpdateSpec(
                        record_id="proj1/TR-1/proj1/WI-1/0",
                        defect_id="proj1/WI-9",
                    )
                ],
                dry_run=False,
            )

        mock_client.patch.assert_not_awaited()

    async def test_bare_defect_id_guarded_and_sent_qualified(
        self, mock_ctx: MagicMock, mock_client: AsyncMock
    ) -> None:
        # Bare defect id: guard + payload both use project-qualified id.
        mock_client.get.return_value = _workitems_response("proj1", ["WI-9"])
        mock_client.patch.return_value = None

        result = await update_test_records(
            mock_ctx,
            project_id="proj1",
            test_run_id="TR-1",
            items=[
                TestRecordUpdateSpec(
                    record_id="proj1/TR-1/proj1/WI-1/0",
                    defect_id="WI-9",
                )
            ],
            dry_run=False,
        )

        assert result.updated is True
        _, get_kwargs = mock_client.get.await_args
        assert "id:(WI-9)" in get_kwargs["params"]["query"]
        _, patch_kwargs = mock_client.patch.await_args
        defect = patch_kwargs["json"]["data"][0]["relationships"]["defect"]
        assert defect["data"]["id"] == "proj1/WI-9"

    async def test_per_item_error_names_offending_item(
        self, mock_ctx: MagicMock, mock_client: AsyncMock
    ) -> None:
        mock_client.get.return_value = _result_enum_response(["passed", "failed"])

        with pytest.raises(ValueError, match=r"items\[1\]"):
            await update_test_records(
                mock_ctx,
                project_id="proj1",
                test_run_id="TR-1",
                items=[
                    TestRecordUpdateSpec(
                        record_id="proj1/TR-1/proj1/WI-1/0", comment="fine"
                    ),
                    TestRecordUpdateSpec(
                        record_id="proj1/TR-1/proj1/WI-2/0", result="ghost"
                    ),
                ],
                dry_run=False,
            )

        mock_client.patch.assert_not_awaited()

    async def test_bad_prefix_record_id_rejected(
        self, mock_ctx: MagicMock, mock_client: AsyncMock
    ) -> None:
        with pytest.raises(ValueError, match=r"items\[0\]"):
            await update_test_records(
                mock_ctx,
                project_id="proj1",
                test_run_id="TR-1",
                items=[
                    TestRecordUpdateSpec(
                        record_id="other/TR-1/proj1/WI-1/0", result="passed"
                    )
                ],
                dry_run=False,
            )

        mock_client.get.assert_not_awaited()
        mock_client.patch.assert_not_awaited()

    async def test_bad_segment_count_record_id_rejected(
        self, mock_ctx: MagicMock, mock_client: AsyncMock
    ) -> None:
        with pytest.raises(ValueError, match=r"items\[0\]"):
            await update_test_records(
                mock_ctx,
                project_id="proj1",
                test_run_id="TR-1",
                items=[
                    TestRecordUpdateSpec(
                        record_id="proj1/TR-1/proj1/WI-1", result="passed"
                    )
                ],
                dry_run=False,
            )

        mock_client.get.assert_not_awaited()
        mock_client.patch.assert_not_awaited()

    async def test_run_not_found_raises_value_error(
        self, mock_ctx: MagicMock, mock_client: AsyncMock
    ) -> None:
        mock_client.patch.side_effect = PolarionNotFoundError(
            "Not found", status_code=404
        )

        with pytest.raises(ValueError, match="list_test_runs"):
            await update_test_records(
                mock_ctx,
                project_id="proj1",
                test_run_id="TR-1",
                items=[
                    TestRecordUpdateSpec(
                        record_id="proj1/TR-1/proj1/WI-1/0", result="passed"
                    )
                ],
                dry_run=False,
            )

    async def test_auth_error_raises_permission_error_with_detail(
        self, mock_ctx: MagicMock, mock_client: AsyncMock
    ) -> None:
        # Polarion 403 detail must surface: e-signature-configured run types
        # reject record writes with portal-only remedy — generic token hint
        # alone misleads.
        mock_client.patch.side_effect = PolarionAuthError(
            "cannot be executed without providing an e-signature", status_code=403
        )

        with pytest.raises(PermissionError, match="without providing an e-signature"):
            await update_test_records(
                mock_ctx,
                project_id="proj1",
                test_run_id="TR-1",
                items=[
                    TestRecordUpdateSpec(
                        record_id="proj1/TR-1/proj1/WI-1/0", result="passed"
                    )
                ],
                dry_run=False,
            )

    async def test_unknown_record_in_batch_raises_runtime_error(
        self, mock_ctx: MagicMock, mock_client: AsyncMock
    ) -> None:
        # Live-verified: unknown record id -- whole-batch 400, not 404.
        mock_client.patch.side_effect = PolarionError(
            "Test Record 'proj1/TR-1/proj1/WI-1/0' was not found", status_code=400
        )

        with pytest.raises(RuntimeError, match="was not found"):
            await update_test_records(
                mock_ctx,
                project_id="proj1",
                test_run_id="TR-1",
                items=[
                    TestRecordUpdateSpec(
                        record_id="proj1/TR-1/proj1/WI-1/0", result="passed"
                    )
                ],
                dry_run=False,
            )


class TestUpdateTestRecordsFieldValidation:
    """Bulk bounds via ``TypeAdapter`` rebuild; spec constraints live in
    ``tests/mcp_server_polarion/models/test_test_runs.py``.
    """

    @staticmethod
    def _adapter(param_name: str) -> TypeAdapter[object]:
        hints = get_type_hints(update_test_records)
        sig = inspect.signature(update_test_records)
        field_info = sig.parameters[param_name].default
        return TypeAdapter(Annotated[hints[param_name], field_info])

    def test_empty_records_rejected(self) -> None:
        with pytest.raises(ValidationError):
            self._adapter("items").validate_python([])

    def test_records_above_max_rejected(self) -> None:
        specs = [
            {"record_id": f"proj1/TR-1/proj1/WI-{i}/0", "result": "passed"}
            for i in range(51)
        ]
        with pytest.raises(ValidationError):
            self._adapter("items").validate_python(specs)

    def test_records_at_max_accepted(self) -> None:
        specs = [
            {"record_id": f"proj1/TR-1/proj1/WI-{i}/0", "result": "passed"}
            for i in range(50)
        ]
        validated = self._adapter("items").validate_python(specs)
        assert isinstance(validated, list)
        assert len(validated) == 50


def _record_detail_response() -> dict[str, object]:
    """Full single-testrecord GET body covering every detail field."""
    return {
        "data": {
            "type": "testrecords",
            "id": "proj1/TR-100/proj1/TC-42/0",
            "attributes": {
                "executed": "2026-06-01T10:00:00Z",
                "duration": 12.5,
                "result": "failed",
                "iteration": 0,
                "testCaseRevision": "42",
                "comment": {
                    "type": "text/html",
                    "value": (
                        '<p id="polarion_1">Investigate <strong>timeout</strong></p>'
                    ),
                },
            },
            "relationships": {
                "testCase": {"data": {"type": "workitems", "id": "proj1/TC-42"}},
                "executedBy": {"data": {"type": "users", "id": "proj1/devemberx"}},
                "defect": {"data": {"type": "workitems", "id": "proj1/DEF-7"}},
            },
        },
        "included": [
            {
                "type": "users",
                "id": "proj1/devemberx",
                "attributes": {"name": "Devember X"},
            }
        ],
    }


class TestGetTestRecord:
    """``get_test_record`` tool."""

    async def test_returns_test_record_detail_with_comment_html(
        self, mock_ctx: MagicMock, mock_client: AsyncMock
    ) -> None:
        mock_client.get.return_value = _record_detail_response()

        result = await get_test_record(
            mock_ctx,
            project_id="proj1",
            test_run_id="TR-100",
            test_case_id="proj1/TC-42",
            iteration=0,
        )

        assert isinstance(result, TestRecordDetail)
        assert result.project_id == "proj1"
        assert result.test_run_id == "TR-100"
        assert result.test_case_id == "proj1/TC-42"
        assert result.iteration == 0
        assert result.result == "failed"
        assert result.executed == "2026-06-01T10:00:00Z"
        assert result.duration == 12.5
        assert result.executed_by_id == "devemberx"
        assert result.executed_by_name == "Devember X"
        assert result.defect_id == "proj1/DEF-7"
        assert result.test_case_revision == "42"
        # Raw HTML passthrough -- anchor ids survive verbatim.
        assert result.comment_html == (
            '<p id="polarion_1">Investigate <strong>timeout</strong></p>'
        )

    async def test_request_params_and_encoded_path(
        self, mock_ctx: MagicMock, mock_client: AsyncMock
    ) -> None:
        mock_client.get.return_value = _record_detail_response()

        await get_test_record(
            mock_ctx,
            project_id="proj1",
            test_run_id="TR-100",
            test_case_id="proj1/TC-42",
            iteration=0,
        )

        args, kwargs = mock_client.get.call_args
        assert args[0] == "/projects/proj1/testruns/TR-100/testrecords/proj1/TC-42/0"
        params = kwargs["params"]
        assert params["fields[testrecords]"] == TEST_RECORD_DETAIL_FIELDS
        assert params["include"] == "executedBy"
        assert params["fields[users]"] == "name"

    async def test_test_case_id_without_slash_raises_before_http(
        self, mock_ctx: MagicMock, mock_client: AsyncMock
    ) -> None:
        with pytest.raises(ValueError, match="project/WI-id"):
            await get_test_record(
                mock_ctx,
                project_id="proj1",
                test_run_id="TR-100",
                test_case_id="TC-42",
                iteration=0,
            )

        mock_client.get.assert_not_called()

    async def test_not_found_raises_value_error(
        self, mock_ctx: MagicMock, mock_client: AsyncMock
    ) -> None:
        mock_client.get.side_effect = PolarionNotFoundError(
            "Not found", status_code=404
        )

        with pytest.raises(ValueError, match="list_test_records"):
            await get_test_record(
                mock_ctx,
                project_id="proj1",
                test_run_id="TR-100",
                test_case_id="proj1/TC-42",
                iteration=0,
            )

    async def test_auth_error_raises_permission_error(
        self, mock_ctx: MagicMock, mock_client: AsyncMock
    ) -> None:
        mock_client.get.side_effect = PolarionAuthError("auth", status_code=401)

        with pytest.raises(PermissionError):
            await get_test_record(
                mock_ctx,
                project_id="proj1",
                test_run_id="TR-100",
                test_case_id="proj1/TC-42",
                iteration=0,
            )

    async def test_other_error_raises_runtime_error(
        self, mock_ctx: MagicMock, mock_client: AsyncMock
    ) -> None:
        mock_client.get.side_effect = PolarionError("boom", status_code=500)

        with pytest.raises(RuntimeError, match="boom"):
            await get_test_record(
                mock_ctx,
                project_id="proj1",
                test_run_id="TR-100",
                test_case_id="proj1/TC-42",
                iteration=0,
            )

    async def test_non_dict_data_falls_back_to_empty_detail(
        self, mock_ctx: MagicMock, mock_client: AsyncMock
    ) -> None:
        mock_client.get.return_value = {"data": []}

        result = await get_test_record(
            mock_ctx,
            project_id="proj1",
            test_run_id="TR-100",
            test_case_id="proj1/TC-42",
            iteration=0,
        )

        assert result.project_id == "proj1"
        assert result.test_run_id == "TR-100"
        assert result.test_case_id == ""


class TestGetTestRecordFieldValidation:
    """``iteration`` bound -- direct calls bypass JSON Schema; proven via
    ``TypeAdapter`` rebuild from signature.
    """

    @staticmethod
    def _adapter(param_name: str) -> TypeAdapter[object]:
        hints = get_type_hints(get_test_record)
        sig = inspect.signature(get_test_record)
        field_info = sig.parameters[param_name].default
        return TypeAdapter(Annotated[hints[param_name], field_info])

    def test_iteration_zero_accepted(self) -> None:
        adapter = self._adapter("iteration")
        assert adapter.validate_python(0) == 0

    def test_iteration_below_zero_rejected(self) -> None:
        with pytest.raises(ValidationError):
            self._adapter("iteration").validate_python(-1)


class TestBuildTestRecordResource:
    """``_build_test_record_resource`` unit seam."""

    def test_minimal_spec_sends_test_case_only(self) -> None:
        resource = _build_test_record_resource(
            project_id="proj1",
            spec=TestRecordCreateSpec(test_case_id="MCPT-1"),
        )

        assert resource == {
            "type": "testrecords",
            "relationships": {
                "testCase": {"data": {"id": "proj1/MCPT-1", "type": "workitems"}}
            },
        }

    def test_full_spec_builds_attributes_and_defect(self) -> None:
        resource = _build_test_record_resource(
            project_id="proj1",
            spec=TestRecordCreateSpec(
                test_case_id="MCPT-1",
                result="passed",
                comment="looks good",
                comment_format="text/plain",
                defect_id="MCPT-99",
            ),
        )

        assert resource == {
            "type": "testrecords",
            "attributes": {
                "result": "passed",
                "comment": {"type": "text/plain", "value": "looks good"},
            },
            "relationships": {
                "testCase": {"data": {"id": "proj1/MCPT-1", "type": "workitems"}},
                "defect": {"data": {"id": "proj1/MCPT-99", "type": "workitems"}},
            },
        }

    def test_bare_ids_qualified_with_project_id(self) -> None:
        resource = _build_test_record_resource(
            project_id="proj1",
            spec=TestRecordCreateSpec(test_case_id="MCPT-1", defect_id="MCPT-99"),
        )

        relationships = resource["relationships"]
        assert relationships["testCase"]["data"]["id"] == "proj1/MCPT-1"  # type: ignore[index,call-overload]
        assert relationships["defect"]["data"]["id"] == "proj1/MCPT-99"  # type: ignore[index,call-overload]

    def test_already_qualified_ids_pass_through(self) -> None:
        resource = _build_test_record_resource(
            project_id="proj1",
            spec=TestRecordCreateSpec(
                test_case_id="OtherProj/MCPT-1", defect_id="OtherProj/MCPT-99"
            ),
        )

        relationships = resource["relationships"]
        assert relationships["testCase"]["data"]["id"] == "OtherProj/MCPT-1"  # type: ignore[index,call-overload]
        assert relationships["defect"]["data"]["id"] == "OtherProj/MCPT-99"  # type: ignore[index,call-overload]

    def test_comment_format_passthrough_no_conversion(self) -> None:
        resource = _build_test_record_resource(
            project_id="proj1",
            spec=TestRecordCreateSpec(
                test_case_id="MCPT-1",
                comment="**not markdown**",
                comment_format="text/html",
            ),
        )

        assert resource["attributes"]["comment"] == {  # type: ignore[index,call-overload]
            "type": "text/html",
            "value": "**not markdown**",
        }

    def test_no_result_or_comment_omits_attributes_key(self) -> None:
        resource = _build_test_record_resource(
            project_id="proj1",
            spec=TestRecordCreateSpec(test_case_id="MCPT-1"),
        )

        assert "attributes" not in resource


class TestBuildCreateTestRecordsPayload:
    """``_build_create_test_records_payload`` unit seam."""

    def test_wraps_resources_in_data_list(self) -> None:
        payload = _build_create_test_records_payload(
            project_id="proj1",
            specs=[
                TestRecordCreateSpec(test_case_id="MCPT-1"),
                TestRecordCreateSpec(test_case_id="MCPT-2", result="passed"),
            ],
        )

        data = payload["data"]
        assert isinstance(data, list)
        assert len(data) == 2
        first, second = data[0], data[1]
        assert isinstance(first, dict)
        assert isinstance(second, dict)
        assert first["relationships"]["testCase"]["data"]["id"] == "proj1/MCPT-1"  # type: ignore[index,call-overload]
        assert second["attributes"]["result"] == "passed"  # type: ignore[index,call-overload]


class TestCreateTestRecords:
    """``create_test_records`` tool."""

    async def test_duplicate_test_case_id_rejected_before_any_request(
        self, mock_ctx: MagicMock, mock_client: AsyncMock
    ) -> None:
        with pytest.raises(ValueError, match=r"Duplicate test_case_id\(s\)"):
            await create_test_records(
                mock_ctx,
                project_id="proj1",
                test_run_id="run1",
                items=[
                    TestRecordCreateSpec(test_case_id="MCPT-1"),
                    TestRecordCreateSpec(test_case_id="MCPT-1"),
                ],
                dry_run=False,
            )
        mock_client.get.assert_not_awaited()
        mock_client.post.assert_not_awaited()

    async def test_bare_and_qualified_same_id_collide(
        self, mock_ctx: MagicMock, mock_client: AsyncMock
    ) -> None:
        # "WI-1" bare qualifies to "proj1/WI-1" -- same identity as explicit.
        with pytest.raises(ValueError, match=r"Duplicate test_case_id\(s\)"):
            await create_test_records(
                mock_ctx,
                project_id="proj1",
                test_run_id="run1",
                items=[
                    TestRecordCreateSpec(test_case_id="WI-1"),
                    TestRecordCreateSpec(test_case_id="proj1/WI-1"),
                ],
                dry_run=False,
            )
        mock_client.post.assert_not_awaited()

    async def test_minimal_create_posts_and_returns_full_ids(
        self, mock_ctx: MagicMock, mock_client: AsyncMock
    ) -> None:
        # 201 body per live findings: id only, no attributes echoed.
        mock_client.post.return_value = {
            "data": [
                {
                    "type": "testrecords",
                    "id": "MCP_Test_Project/run1/MCP_Test_Project/MCPT-568/0",
                }
            ]
        }

        result = await create_test_records(
            mock_ctx,
            project_id="MCP_Test_Project",
            test_run_id="run1",
            items=[TestRecordCreateSpec(test_case_id="MCPT-568")],
            dry_run=False,
        )

        assert result.created is True
        assert result.dry_run is False
        assert result.record_ids == [
            "MCP_Test_Project/run1/MCP_Test_Project/MCPT-568/0"
        ]
        assert result.payload_preview is None
        path = mock_client.post.await_args.args[0]
        assert path == "/projects/MCP_Test_Project/testruns/run1/testrecords"

    async def test_result_guard_blocks_before_post(
        self, mock_ctx: MagicMock, mock_client: AsyncMock
    ) -> None:
        mock_client.get.return_value = project_enum_response(
            "test-result", ["passed", "failed", "blocked"]
        )

        with pytest.raises(ValueError, match="ghost"):
            await create_test_records(
                mock_ctx,
                project_id="proj1",
                test_run_id="run1",
                items=[TestRecordCreateSpec(test_case_id="MCPT-1", result="ghost")],
                dry_run=False,
            )

        mock_client.post.assert_not_awaited()

    async def test_defect_guard_blocks_before_post(
        self, mock_ctx: MagicMock, mock_client: AsyncMock
    ) -> None:
        mock_client.get.return_value = workitems_response("proj1", [])

        with pytest.raises(ValueError, match="dangling"):
            await create_test_records(
                mock_ctx,
                project_id="proj1",
                test_run_id="run1",
                items=[
                    TestRecordCreateSpec(test_case_id="MCPT-1", defect_id="MCPT-99999")
                ],
                dry_run=False,
            )

        mock_client.post.assert_not_awaited()

    async def test_dry_run_returns_preview_and_still_runs_guards(
        self, mock_ctx: MagicMock, mock_client: AsyncMock
    ) -> None:
        mock_client.get.return_value = project_enum_response("test-result", ["passed"])

        result = await create_test_records(
            mock_ctx,
            project_id="proj1",
            test_run_id="run1",
            items=[TestRecordCreateSpec(test_case_id="MCPT-1", result="passed")],
            dry_run=True,
        )

        assert result.created is False
        assert result.dry_run is True
        assert result.record_ids == []
        assert result.payload_preview is not None
        mock_client.get.assert_awaited()
        mock_client.post.assert_not_awaited()

    async def test_run_not_found_raises_value_error(
        self, mock_ctx: MagicMock, mock_client: AsyncMock
    ) -> None:
        mock_client.post.side_effect = PolarionNotFoundError(
            "Not found", status_code=404
        )

        # 404 ambiguous: run or project may be missing -- message names both.
        with pytest.raises(
            ValueError, match=r"Test run 'missing' or project 'proj1' not found"
        ):
            await create_test_records(
                mock_ctx,
                project_id="proj1",
                test_run_id="missing",
                items=[TestRecordCreateSpec(test_case_id="MCPT-1")],
                dry_run=False,
            )

    async def test_auth_error_raises_permission_error(
        self, mock_ctx: MagicMock, mock_client: AsyncMock
    ) -> None:
        mock_client.post.side_effect = PolarionAuthError("auth", status_code=401)

        with pytest.raises(PermissionError):
            await create_test_records(
                mock_ctx,
                project_id="proj1",
                test_run_id="run1",
                items=[TestRecordCreateSpec(test_case_id="MCPT-1")],
                dry_run=False,
            )

    async def test_other_error_raises_runtime_error(
        self, mock_ctx: MagicMock, mock_client: AsyncMock
    ) -> None:
        # Server validates testCase itself; 400 detail must flow through.
        mock_client.post.side_effect = PolarionError(
            "Test Case is missing, or the one specified is invalid.",
            status_code=400,
        )

        with pytest.raises(RuntimeError, match="Test Case is missing"):
            await create_test_records(
                mock_ctx,
                project_id="proj1",
                test_run_id="run1",
                items=[TestRecordCreateSpec(test_case_id="MCPT-1")],
                dry_run=False,
            )

    async def test_id_count_mismatch_raises(
        self, mock_ctx: MagicMock, mock_client: AsyncMock
    ) -> None:
        mock_client.post.return_value = {"data": []}

        with pytest.raises(RuntimeError, match="list_test_records"):
            await create_test_records(
                mock_ctx,
                project_id="proj1",
                test_run_id="run1",
                items=[TestRecordCreateSpec(test_case_id="MCPT-1")],
                dry_run=False,
            )


class TestCreateTestRecordsFieldValidation:
    """Bulk bounds + spec constraints via ``TypeAdapter`` rebuild."""

    @staticmethod
    def _adapter(param_name: str) -> TypeAdapter[object]:
        hints = get_type_hints(create_test_records)
        sig = inspect.signature(create_test_records)
        field_info = sig.parameters[param_name].default
        return TypeAdapter(Annotated[hints[param_name], field_info])

    def test_empty_items_rejected(self) -> None:
        with pytest.raises(ValidationError):
            self._adapter("items").validate_python([])

    def test_items_above_max_rejected(self) -> None:
        specs = [{"test_case_id": f"MCPT-{i}"} for i in range(51)]
        with pytest.raises(ValidationError):
            self._adapter("items").validate_python(specs)

    def test_items_at_max_accepted(self) -> None:
        specs = [{"test_case_id": f"MCPT-{i}"} for i in range(50)]
        validated = self._adapter("items").validate_python(specs)
        assert isinstance(validated, list)
        assert len(validated) == 50

    def test_empty_project_id_rejected(self) -> None:
        with pytest.raises(ValidationError):
            self._adapter("project_id").validate_python("")

    def test_empty_test_run_id_rejected(self) -> None:
        with pytest.raises(ValidationError):
            self._adapter("test_run_id").validate_python("")

    def test_spec_requires_non_empty_test_case_id(self) -> None:
        with pytest.raises(ValidationError):
            TestRecordCreateSpec(test_case_id="")

    def test_extra_spec_key_rejected(self) -> None:
        with pytest.raises(ValidationError):
            TestRecordCreateSpec(test_case_id="MCPT-1", bogus="x")  # type: ignore[call-arg]
