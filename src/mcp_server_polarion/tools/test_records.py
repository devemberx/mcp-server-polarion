"""Test record tools — list, get, create, and update execution records of a test run."""

from __future__ import annotations

from typing import cast

from fastmcp import Context
from pydantic import Field

from mcp_server_polarion.core.exceptions import (
    PolarionAuthError,
    PolarionError,
    PolarionNotFoundError,
)
from mcp_server_polarion.models import (
    JsonValue,
    PaginatedResult,
    TestRecordCreateSpec,
    TestRecordDetail,
    TestRecordsCreateResult,
    TestRecordSummary,
    TestRecordsUpdateResult,
    TestRecordUpdateSpec,
)
from mcp_server_polarion.server import mcp
from mcp_server_polarion.tools._shared.errors import auth_error
from mcp_server_polarion.tools._shared.fields import (
    MAX_BULK_ITEMS,
    TEST_RECORD_DETAIL_FIELDS,
    TEST_RECORD_LIST_FIELDS,
)
from mcp_server_polarion.tools._shared.guard import (
    guard_test_record_defect_targets,
    guard_test_record_results,
)
from mcp_server_polarion.tools._shared.helpers import (
    encode_path_segment,
    ensure_unique_ids,
    get_client,
    qualify_work_item_id,
    reraise_with_item_context,
    test_record_path,
)
from mcp_server_polarion.tools._shared.pagination import (
    DEFAULT_PAGE_SIZE,
    make_page,
)
from mcp_server_polarion.tools._shared.parse import (
    extract_created_full_ids,
    parse_included_user_name_map,
    parse_test_record_detail,
    parse_test_record_summaries,
)


def _build_test_record_resource(
    *,
    project_id: str,
    spec: TestRecordCreateSpec,
) -> dict[str, JsonValue]:
    """One ``testrecords`` resource for bulk create POST. No client-set
    ``id`` -- server compose the 5-segment id (testCase + auto-incremented
    iteration).
    """
    attributes: dict[str, JsonValue] = {}
    if spec.result:
        attributes["result"] = spec.result
    if spec.comment:
        # Verbatim passthrough, no Markdown conversion (comments pattern).
        attributes["comment"] = {"type": spec.comment_format, "value": spec.comment}

    relationships: dict[str, JsonValue] = {
        "testCase": {
            "data": {
                "id": qualify_work_item_id(spec.test_case_id, project_id),
                "type": "workitems",
            }
        }
    }
    if spec.defect_id:
        relationships["defect"] = {
            "data": {
                "id": qualify_work_item_id(spec.defect_id, project_id),
                "type": "workitems",
            }
        }

    resource: dict[str, JsonValue] = {"type": "testrecords"}
    if attributes:
        resource["attributes"] = attributes
    resource["relationships"] = relationships
    return resource


def _build_create_test_records_payload(
    *,
    project_id: str,
    specs: list[TestRecordCreateSpec],
) -> dict[str, JsonValue]:
    """JSON:API body for bulk ``POST .../testruns/{tr}/testrecords``."""
    data: list[JsonValue] = [
        _build_test_record_resource(project_id=project_id, spec=spec) for spec in specs
    ]
    return {"data": data}


@mcp.tool(
    tags={"write"},
    timeout=60.0,
    annotations={
        # Additive: non-destructive; repeat testCase starts a new iteration.
        "readOnlyHint": False,
        "destructiveHint": False,
        "idempotentHint": False,
        "openWorldHint": True,
    },
)
async def create_test_records(
    ctx: Context,
    project_id: str = Field(min_length=1, description="Polarion project ID."),
    test_run_id: str = Field(min_length=1, description="Test run ID."),
    items: list[TestRecordCreateSpec] = Field(  # noqa: B008
        min_length=1,
        max_length=MAX_BULK_ITEMS,
        description="Test records to create in one request (1-50).",
    ),
    dry_run: bool = Field(
        default=False,
        description="Preview payload without writing; guards still query Polarion.",
    ),
) -> TestRecordsCreateResult:
    """Create 1-50 test records on one test run, recording which test cases
    were executed with what result.

    Use list_test_records to read them back; create_test_runs creates the
    run itself. Atomic: one bad item rejects the whole batch. Posting the
    same test_case_id again starts a new iteration rather than replacing it
    -- use separate calls, not duplicates in one batch. comment is sent
    verbatim in comment_format, no Markdown conversion.

    Returns record_ids as full 5-segment ids -- never shortened. result is
    validated against the project's testing enumerations; defect must
    reference an existing work item. An invalid test_case_id is rejected by
    Polarion -- resolve via list_work_items first.
    """
    client = get_client(ctx)
    qualified_test_case_ids = [
        qualify_work_item_id(spec.test_case_id, project_id) for spec in items
    ]
    ensure_unique_ids(qualified_test_case_ids, label="test_case_id")

    payload = _build_create_test_records_payload(project_id=project_id, specs=items)

    await guard_test_record_results(
        client,
        project_id,
        (spec.result for spec in items if spec.result is not None),
    )
    await guard_test_record_defect_targets(
        client,
        project_id,
        (
            qualify_work_item_id(spec.defect_id, project_id)
            for spec in items
            if spec.defect_id
        ),
    )

    if dry_run:
        return TestRecordsCreateResult(
            created=False,
            dry_run=True,
            record_ids=[],
            payload_preview=payload,
        )

    path = (
        f"/projects/{encode_path_segment(project_id)}"
        f"/testruns/{encode_path_segment(test_run_id)}/testrecords"
    )
    try:
        response = await client.post(path, json=cast(dict[str, object], payload))
    except PolarionAuthError as exc:
        raise auth_error("create test records", exc) from exc
    except PolarionNotFoundError as exc:
        raise ValueError(
            f"Test run '{test_run_id}' or project '{project_id}' not found. "
            "Use `list_test_runs` (or `list_projects`) to discover valid IDs."
        ) from exc
    except PolarionError as exc:
        raise RuntimeError(f"Failed to create test records: {exc.message}") from exc

    new_ids = extract_created_full_ids(
        response, expected_count=len(items), list_tool="list_test_records"
    )

    return TestRecordsCreateResult(
        created=True,
        dry_run=False,
        record_ids=new_ids,
        payload_preview=None,
    )


# record_id = projectId/testRunId/testCaseProjectId/testCaseId/iteration.
_RECORD_ID_SEGMENTS = 5


def _build_update_test_record_resource(
    *,
    project_id: str,
    spec: TestRecordUpdateSpec,
) -> dict[str, JsonValue]:
    """One ``testrecords`` resource for bulk PATCH; skip unset so update
    never blank existing attribute. ``id`` = ``spec.record_id`` verbatim --
    record ids never parsed. Defect id qualified: bare id pass guard via
    *project_id* fallback but store dangling unqualified -- qualify keep
    guard and payload on same id. Spec validator guarantee at least one
    effective field survive.
    """
    attributes: dict[str, JsonValue] = {}
    if spec.result:
        attributes["result"] = spec.result
    if spec.comment:
        attributes["comment"] = {"type": spec.comment_format, "value": spec.comment}

    resource: dict[str, JsonValue] = {
        "type": "testrecords",
        "id": spec.record_id,
    }
    # Defect-only spec: omit empty attributes -- live-verified 204, defect
    # store, prior values keep.
    if attributes:
        resource["attributes"] = attributes
    if spec.defect_id:
        resource["relationships"] = {
            "defect": {
                "data": {
                    "type": "workitems",
                    "id": qualify_work_item_id(spec.defect_id, project_id),
                }
            }
        }
    return resource


def _build_update_test_records_payload(
    *,
    project_id: str,
    specs: list[TestRecordUpdateSpec],
) -> dict[str, JsonValue]:
    """JSON:API body for bulk ``PATCH /projects/{p}/testruns/{r}/testrecords``."""
    data: list[JsonValue] = [
        _build_update_test_record_resource(project_id=project_id, spec=spec)
        for spec in specs
    ]
    return {"data": data}


@mcp.tool(
    tags={"write"},
    timeout=60.0,
    annotations={
        "readOnlyHint": False,
        "destructiveHint": True,
        "idempotentHint": True,
        "openWorldHint": True,
    },
)
async def update_test_records(
    ctx: Context,
    project_id: str = Field(min_length=1, description="Polarion project ID."),
    test_run_id: str = Field(
        min_length=1, description="Test run ID (e.g. 'TR-2026-01')."
    ),
    items: list[TestRecordUpdateSpec] = Field(  # noqa: B008
        min_length=1,
        max_length=MAX_BULK_ITEMS,
        description="Per-record changes (1-50); unset fields stay unchanged.",
    ),
    dry_run: bool = Field(
        default=False,
        description="Preview payload without writing; guards still query Polarion.",
    ),
) -> TestRecordsUpdateResult:
    """Set result, comment, and/or defect link on 1-50 test records of one
    test run in a single bulk PATCH.

    Run-level fields (title, status, group_id) — use update_test_runs
    instead. Atomic: one bad item rejects the whole batch; no record changes.

    record_id must be copied verbatim from list_test_records — never
    decomposed. comment is sent verbatim; Polarion stores it as text/html
    regardless of the comment_format sent, so a later read always shows
    text/html.

    Returns the echoed record_ids only — re-read via list_test_records.
    result must already be a value the run uses (discover via
    list_test_records) or the write is rejected; defect_id must
    reference an existing work item or the write is rejected.
    """
    client = get_client(ctx)
    ensure_unique_ids((spec.record_id for spec in items), label="record_id")

    payload = _build_update_test_records_payload(project_id=project_id, specs=items)

    prefix = f"{project_id}/{test_run_id}/"
    for index, spec in enumerate(items):
        with reraise_with_item_context(index, spec.record_id):
            segments = spec.record_id.split("/")
            if len(segments) != _RECORD_ID_SEGMENTS or not spec.record_id.startswith(
                prefix
            ):
                raise ValueError(
                    f"record_id '{spec.record_id}' must be the full 5-segment id "
                    f"from list_test_records, starting with '{prefix}'."
                )
            if spec.result:
                await guard_test_record_results(client, project_id, [spec.result])

    await guard_test_record_defect_targets(
        client,
        project_id,
        (
            qualify_work_item_id(spec.defect_id, project_id)
            for spec in items
            if spec.defect_id
        ),
    )

    if dry_run:
        return TestRecordsUpdateResult(
            updated=False,
            dry_run=True,
            record_ids=[],
            payload_preview=payload,
        )

    path = (
        f"/projects/{encode_path_segment(project_id)}"
        f"/testruns/{encode_path_segment(test_run_id)}/testrecords"
    )
    try:
        await client.patch(path, json=cast(dict[str, object], payload))
    except PolarionAuthError as exc:
        raise auth_error("update test records", exc) from exc
    except PolarionNotFoundError as exc:
        raise ValueError(
            f"Test run '{test_run_id}' not found in project '{project_id}'. "
            "Use `list_test_runs` to discover valid IDs."
        ) from exc
    except PolarionError as exc:
        raise RuntimeError(f"Failed to update test records: {exc.message}") from exc

    return TestRecordsUpdateResult(
        updated=True,
        dry_run=False,
        record_ids=[spec.record_id for spec in items],
        payload_preview=None,
    )


@mcp.tool(
    tags={"read"},
    timeout=60.0,
    annotations={"readOnlyHint": True},
)
async def list_test_records(  # noqa: PLR0913
    ctx: Context,
    project_id: str = Field(description="Polarion project ID."),
    test_run_id: str = Field(description="Test run ID (e.g. 'TR-2026-01')."),
    result: str | None = Field(
        default=None,
        description="Filter by result enum ID (e.g. 'passed', 'failed', 'blocked').",
    ),
    page_size: int = Field(default=DEFAULT_PAGE_SIZE, ge=1, le=100),
    page_number: int = Field(default=1, ge=1),
) -> PaginatedResult[TestRecordSummary]:
    """List execution records of one test run — one row per test case
    iteration. For run metadata use get_test_run.

    Filter by result (e.g. 'failed') or omit for all; not-yet-executed
    records have empty result. Lucene query is NOT supported here. Returns
    summaries — id is the exact value update_test_records takes as
    record_id; defect_id links the failure work item.
    """
    client = get_client(ctx)
    params: dict[str, str | int] = {
        "fields[testrecords]": TEST_RECORD_LIST_FIELDS,
        "include": "executedBy",
        "fields[users]": "name",
        "page[size]": page_size,
        "page[number]": page_number,
    }
    if result is not None:
        params["testResultId"] = result
    path = (
        f"/projects/{encode_path_segment(project_id)}"
        f"/testruns/{encode_path_segment(test_run_id)}/testrecords"
    )
    try:
        response = await client.get(path, params=params)
    except PolarionNotFoundError as exc:
        raise ValueError(
            f"Test run '{test_run_id}' not found in project '{project_id}'. "
            "Use `list_test_runs` to discover valid IDs."
        ) from exc
    except PolarionAuthError as exc:
        raise auth_error("list test records", exc) from exc
    except PolarionError as exc:
        raise RuntimeError(f"Failed to list test records: {exc.message}") from exc

    items = parse_test_record_summaries(response)

    return make_page(items, response, page_number, page_size)


@mcp.tool(
    tags={"read"},
    timeout=60.0,
    annotations={"readOnlyHint": True},
)
async def get_test_record(
    ctx: Context,
    project_id: str = Field(description="Polarion project ID."),
    test_run_id: str = Field(description="Test run ID (e.g. 'TR-2026-01')."),
    test_case_id: str = Field(
        description=(
            "Full test case work item ID 'project/WI-id' as returned by "
            "list_test_records."
        )
    ),
    iteration: int = Field(
        default=0, ge=0, description="Record iteration number (0-based)."
    ),
) -> TestRecordDetail:
    """Get full detail of one test-case iteration inside a test run:
    execution comment and test-case revision.

    Use list_test_records for run-wide summaries, get_test_run for run
    metadata. comment_html carries the record's raw HTML comment;
    plain-text comments return as-is. Verify coordinates via
    list_test_records if not found.
    """
    client = get_client(ctx)
    path = test_record_path(project_id, test_run_id, test_case_id, iteration)
    try:
        response = await client.get(
            path,
            params={
                "fields[testrecords]": TEST_RECORD_DETAIL_FIELDS,
                "include": "executedBy",
                "fields[users]": "name",
            },
        )
    except PolarionNotFoundError as exc:
        raise ValueError(
            f"Test record for case '{test_case_id}' iteration {iteration} not "
            f"found in test run '{test_run_id}' (project '{project_id}'). "
            "Use `list_test_records` to discover valid coordinates."
        ) from exc
    except PolarionAuthError as exc:
        raise auth_error("access test record", exc) from exc
    except PolarionError as exc:
        raise RuntimeError(f"Failed to get test record: {exc.message}") from exc

    data = response.get("data", {})
    if not isinstance(data, dict):
        data = {}

    return parse_test_record_detail(
        data,
        project_id=project_id,
        test_run_id=test_run_id,
        user_names=parse_included_user_name_map(response),
    )
