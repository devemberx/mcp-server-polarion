"""Test run tools — list, search, get, create, and update test runs in a project."""

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
    TestRunCreateSpec,
    TestRunDetail,
    TestRunsCreateResult,
    TestRunSummary,
    TestRunsUpdateResult,
    TestRunUpdateSpec,
)
from mcp_server_polarion.server import mcp
from mcp_server_polarion.tools._shared.custom_fields import (
    STANDARD_TEST_RUN_ATTRIBUTES,
    merge_custom_fields,
)
from mcp_server_polarion.tools._shared.fields import (
    MAX_BULK_ITEMS,
    TEST_RECORD_DETAIL_FIELDS,
    TEST_RECORD_LIST_FIELDS,
    TEST_RUN_DETAIL_FIELDS,
    TEST_RUN_LIST_FIELDS,
)
from mcp_server_polarion.tools._shared.guard import (
    guard_test_record_defect_targets,
    guard_test_record_results,
    guard_test_run_custom_fields,
    guard_test_run_enums,
    guard_test_run_templates,
)
from mcp_server_polarion.tools._shared.helpers import (
    encode_path_segment,
    ensure_unique_ids,
    get_client,
    qualify_work_item_id,
    reraise_with_item_context,
)
from mcp_server_polarion.tools._shared.pagination import (
    DEFAULT_PAGE_SIZE,
    make_page,
)
from mcp_server_polarion.tools._shared.parse import (
    extract_created_full_ids,
    extract_created_short_ids,
    parse_included_user_name_map,
    parse_test_record_detail,
    parse_test_record_summaries,
    parse_test_run_detail,
    parse_test_run_summaries,
)


def _build_test_run_resource(
    *,
    project_id: str,
    spec: TestRunCreateSpec,
) -> dict[str, JsonValue]:
    """One ``testruns`` resource for bulk create POST; skip unset so
    template (or Polarion default) fill them.
    """
    attributes: dict[str, JsonValue] = {"id": spec.id}
    if spec.title:
        attributes["title"] = spec.title
    if spec.type:
        attributes["type"] = spec.type
    if spec.status:
        attributes["status"] = spec.status
    merge_custom_fields(attributes, spec.custom_fields, STANDARD_TEST_RUN_ATTRIBUTES)

    resource: dict[str, JsonValue] = {
        "type": "testruns",
        "attributes": attributes,
    }
    if spec.template_id:
        resource["relationships"] = {
            "template": {
                "data": {
                    "type": "testruns",
                    "id": f"{project_id}/{spec.template_id}",
                }
            }
        }
    return resource


def _build_create_test_runs_payload(
    *,
    project_id: str,
    specs: list[TestRunCreateSpec],
) -> dict[str, JsonValue]:
    """JSON:API body for bulk ``POST /projects/{p}/testruns``."""
    data: list[JsonValue] = [
        _build_test_run_resource(project_id=project_id, spec=spec) for spec in specs
    ]
    return {"data": data}


@mcp.tool(
    tags={"write"},
    timeout=60.0,
    annotations={
        # Additive: non-destructive; retry 409s on same explicit id.
        "readOnlyHint": False,
        "destructiveHint": False,
        "idempotentHint": False,
        "openWorldHint": True,
    },
)
async def create_test_runs(
    ctx: Context,
    project_id: str = Field(min_length=1, description="Polarion project ID."),
    items: list[TestRunCreateSpec] = Field(  # noqa: B008
        min_length=1,
        max_length=MAX_BULK_ITEMS,
        description="Test runs to create in one request (1-50).",
    ),
    dry_run: bool = Field(
        default=False,
        description="Preview payload without writing; guards still query Polarion.",
    ),
) -> TestRunsCreateResult:
    """Create 1-50 test runs in one project in one bulk request.

    id is required per item — never auto-generated.
    type/status are validated against the project's testing enumerations and
    template_id against existing templates (list_test_runs(templates=True)).
    custom_fields keys are validated against a sample of existing runs;
    enum-typed custom values are not (test runs have no options API).
    Atomic: one bad item rejects the whole batch.
    """
    client = get_client(ctx)
    ensure_unique_ids((spec.id for spec in items), label="id")

    # Build pre-guards -- standard-attr shadow collision raise locally,
    # clearer than network key-guard message.
    payload = _build_create_test_runs_payload(project_id=project_id, specs=items)

    for spec in items:
        await guard_test_run_enums(
            client, project_id, type=spec.type, status=spec.status
        )
    await guard_test_run_templates(
        client, project_id, [spec.template_id for spec in items if spec.template_id]
    )
    for spec in items:
        if spec.custom_fields:
            await guard_test_run_custom_fields(client, project_id, spec.custom_fields)

    if dry_run:
        return TestRunsCreateResult(
            created=False,
            dry_run=True,
            test_run_ids=[],
            payload_preview=payload,
        )

    path = f"/projects/{encode_path_segment(project_id)}/testruns"
    try:
        response = await client.post(path, json=cast(dict[str, object], payload))
    except PolarionAuthError as exc:
        raise PermissionError(
            "Cannot create test runs -- check your POLARION_TOKEN permissions."
        ) from exc
    except PolarionNotFoundError as exc:
        raise ValueError(
            f"Project '{project_id}' not found. "
            "Use `list_projects` to discover valid project IDs."
        ) from exc
    except PolarionError as exc:
        raise RuntimeError(f"Failed to create test runs: {exc.message}") from exc

    new_ids = extract_created_short_ids(response)
    if len(new_ids) != len(items):
        raise RuntimeError(
            f"Polarion accepted the bulk create but returned {len(new_ids)} "
            f"ids for {len(items)} requested runs. The batch may be partially "
            "created; verify with list_test_runs before retrying."
        )

    return TestRunsCreateResult(
        created=True,
        dry_run=False,
        test_run_ids=new_ids,
        payload_preview=None,
    )


def _build_update_test_run_resource(
    *,
    project_id: str,
    spec: TestRunUpdateSpec,
) -> dict[str, JsonValue]:
    """One ``testruns`` resource for bulk PATCH; skip unset so update
    never blank existing attribute. Spec validator guarantee at least one
    attribute survive; all writables live under ``attributes``.
    """
    attributes: dict[str, JsonValue] = {}
    if spec.title:
        attributes["title"] = spec.title
    if spec.status:
        attributes["status"] = spec.status
    if spec.group_id:
        attributes["groupId"] = spec.group_id
    merge_custom_fields(attributes, spec.custom_fields, STANDARD_TEST_RUN_ATTRIBUTES)

    return {
        "type": "testruns",
        "id": f"{project_id}/{spec.test_run_id}",
        "attributes": attributes,
    }


def _build_update_test_runs_payload(
    *,
    project_id: str,
    specs: list[TestRunUpdateSpec],
) -> dict[str, JsonValue]:
    """JSON:API body for bulk ``PATCH /projects/{p}/testruns``."""
    data: list[JsonValue] = [
        _build_update_test_run_resource(project_id=project_id, spec=spec)
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
async def update_test_runs(
    ctx: Context,
    project_id: str = Field(min_length=1, description="Polarion project ID."),
    items: list[TestRunUpdateSpec] = Field(  # noqa: B008
        min_length=1,
        max_length=MAX_BULK_ITEMS,
        description="Per-run changes (1-50); unset fields stay unchanged.",
    ),
    dry_run: bool = Field(
        default=False,
        description="Preview payload without writing; guards still query Polarion.",
    ),
) -> TestRunsUpdateResult:
    """Update fields on 1-50 existing test runs in one bulk PATCH; unset
    fields stay unchanged. Atomic: one bad item rejects the whole batch.

    Writable: title, status, group_id, custom_fields. status is validated
    against the project's testing enumerations. custom_fields is partial;
    keys are validated against a sample of existing runs, values are not
    (test runs have no options API). finishedOn is server-managed — not
    settable. Returns ids only — re-read via list_test_runs.
    """
    client = get_client(ctx)
    ensure_unique_ids((spec.test_run_id for spec in items), label="test_run_id")

    # Build pre-guards -- standard-attr shadow collision raise locally,
    # clearer than network key-guard message.
    payload = _build_update_test_runs_payload(project_id=project_id, specs=items)

    for index, spec in enumerate(items):
        with reraise_with_item_context(index, spec.test_run_id):
            if spec.status:
                await guard_test_run_enums(client, project_id, status=spec.status)
            if spec.custom_fields:
                await guard_test_run_custom_fields(
                    client, project_id, spec.custom_fields
                )

    if dry_run:
        return TestRunsUpdateResult(
            updated=False,
            dry_run=True,
            test_run_ids=[],
            payload_preview=payload,
        )

    path = f"/projects/{encode_path_segment(project_id)}/testruns"
    try:
        await client.patch(path, json=cast(dict[str, object], payload))
    except PolarionAuthError as exc:
        raise PermissionError(
            "Cannot update test runs -- check your POLARION_TOKEN permissions."
        ) from exc
    except PolarionNotFoundError as exc:
        raise ValueError(
            f"A test run in the batch was not found in project '{project_id}': "
            f"{exc.message} Use `list_test_runs` to discover valid IDs."
        ) from exc
    except PolarionError as exc:
        raise RuntimeError(f"Failed to update test runs: {exc.message}") from exc

    return TestRunsUpdateResult(
        updated=True,
        dry_run=False,
        test_run_ids=[spec.test_run_id for spec in items],
        payload_preview=None,
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
        raise PermissionError(
            "Cannot create test records -- check your POLARION_TOKEN permissions."
        ) from exc
    except PolarionNotFoundError as exc:
        raise ValueError(
            f"Test run '{test_run_id}' or project '{project_id}' not found. "
            "Use `list_test_runs` (or `list_projects`) to discover valid IDs."
        ) from exc
    except PolarionError as exc:
        raise RuntimeError(f"Failed to create test records: {exc.message}") from exc

    new_ids = extract_created_full_ids(response)
    if len(new_ids) != len(items):
        raise RuntimeError(
            f"Polarion accepted the bulk create but returned {len(new_ids)} "
            f"ids for {len(items)} requested records. The batch may be "
            "partially created; verify with list_test_records before retrying."
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
        # Surface Polarion detail whole: e-signature-configured run types 403
        # record writes with portal-only remedy — token hint alone mislead.
        raise PermissionError(f"Cannot update test records -- {exc.message}") from exc
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
async def list_test_runs(  # noqa: PLR0913
    ctx: Context,
    project_id: str = Field(description="Polarion project ID."),
    query: str | None = Field(
        default=None,
        description=(
            "Optional Lucene filter (e.g. 'status:open', 'groupId:Release-2.5', "
            "'author.name:\"Jane Doe\"', 'HAS_VALUE:<field>' to match runs "
            "with that field populated)."
        ),
    ),
    templates: bool = Field(
        default=False,
        description="List template blueprints instead of actual run instances.",
    ),
    page_size: int = Field(default=DEFAULT_PAGE_SIZE, ge=1, le=100),
    page_number: int = Field(default=1, ge=1),
) -> PaginatedResult[TestRunSummary]:
    """List / search test runs in a project.

    Returns run instances by default; set templates=True for the
    reusable template blueprints. Filter by person with author.name (exact,
    quoted) — author.id does not match on test runs; discover the full name
    from an unfiltered page first.
    """
    client = get_client(ctx)
    params: dict[str, str | int] = {
        "fields[testruns]": TEST_RUN_LIST_FIELDS,
        "include": "author",
        "fields[users]": "name",
        "page[size]": page_size,
        "page[number]": page_number,
    }
    if query is not None:
        params["query"] = query
    if templates:
        params["templates"] = "true"
    try:
        response = await client.get(
            f"/projects/{encode_path_segment(project_id)}/testruns",
            params=params,
        )
    except PolarionNotFoundError as exc:
        raise ValueError(
            f"Project '{project_id}' not found. "
            "Use `list_projects` to discover valid project IDs."
        ) from exc
    except PolarionAuthError as exc:
        raise PermissionError(
            "Cannot list test runs -- check your POLARION_TOKEN permissions."
        ) from exc
    except PolarionError as exc:
        raise RuntimeError(f"Failed to list test runs: {exc.message}") from exc

    items = parse_test_run_summaries(response)

    return make_page(items, response, page_number, page_size)


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
    summaries — record_id is the exact id update_test_records takes;
    defect_id links the failure work item.
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
        raise PermissionError(
            "Cannot list test records -- check your POLARION_TOKEN permissions."
        ) from exc
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
    if "/" not in test_case_id:
        raise ValueError(
            f"test_case_id '{test_case_id}' must be the full 'project/WI-id' "
            "form returned by list_test_records, not the short work item ID."
        )
    tc_project, tc_id = test_case_id.split("/", 1)

    client = get_client(ctx)
    path = (
        f"/projects/{encode_path_segment(project_id)}"
        f"/testruns/{encode_path_segment(test_run_id)}"
        f"/testrecords/{encode_path_segment(tc_project)}/{encode_path_segment(tc_id)}"
        f"/{encode_path_segment(str(iteration))}"
    )
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
        raise PermissionError(
            "Cannot access test record -- check your POLARION_TOKEN permissions."
        ) from exc
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


@mcp.tool(
    tags={"read"},
    timeout=60.0,
    annotations={"readOnlyHint": True},
)
async def get_test_run(
    ctx: Context,
    project_id: str = Field(description="Polarion project ID."),
    test_run_id: str = Field(description="Test run ID (e.g. 'TR-2026-01')."),
    include_homepage_content_html: bool = Field(
        default=False,
        description="Fill content_html with the run's raw HTML report body.",
    ),
) -> TestRunDetail:
    """Get full details of one test run by ID.

    Returns writable fields (title, status, group_id, custom_fields) plus
    read-only context: test-case selection, template provenance, author, and
    timestamps. include_homepage_content_html=True fills content_html with
    the raw HTML report body; it stays empty when use_report_from_template
    is true. Never feed back a blanked (flag=False) body.
    """
    client = get_client(ctx)
    path = (
        f"/projects/{encode_path_segment(project_id)}"
        f"/testruns/{encode_path_segment(test_run_id)}"
    )
    try:
        response = await client.get(
            path,
            params={
                "fields[testruns]": TEST_RUN_DETAIL_FIELDS,
                "include": "author",
                "fields[users]": "name",
            },
        )
    except PolarionNotFoundError as exc:
        raise ValueError(
            f"Test run '{test_run_id}' not found in project '{project_id}'. "
            "Use `list_test_runs` to discover valid IDs."
        ) from exc
    except PolarionAuthError as exc:
        raise PermissionError(
            "Cannot access test run -- check your POLARION_TOKEN permissions."
        ) from exc
    except PolarionError as exc:
        raise RuntimeError(
            f"Failed to get test run '{test_run_id}': {exc.message}"
        ) from exc

    data = response.get("data", {})
    if not isinstance(data, dict):
        data = {}

    detail = parse_test_run_detail(
        data,
        project_id=project_id,
        fallback_id=test_run_id,
        user_names=parse_included_user_name_map(response),
    )
    if not include_homepage_content_html:
        # Body always travel over wire; blank per flag=False contract.
        detail = detail.model_copy(update={"content_html": ""})
    return detail
