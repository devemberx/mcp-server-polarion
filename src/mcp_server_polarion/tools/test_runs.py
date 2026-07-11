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
    TEST_RUN_DETAIL_FIELDS,
    TEST_RUN_LIST_FIELDS,
)
from mcp_server_polarion.tools._shared.guard import (
    guard_test_run_custom_fields,
    guard_test_run_enums,
    guard_test_run_templates,
)
from mcp_server_polarion.tools._shared.helpers import (
    encode_path_segment,
    ensure_unique_ids,
    get_client,
    reraise_with_item_context,
)
from mcp_server_polarion.tools._shared.pagination import (
    DEFAULT_PAGE_SIZE,
    make_page,
)
from mcp_server_polarion.tools._shared.parse import (
    extract_created_short_ids,
    parse_included_user_name_map,
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

    Returns actual run instances by default; set templates=True for the
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
    is true (the run inherits its template's report). Never feed back a
    blanked (flag=False) body.
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
