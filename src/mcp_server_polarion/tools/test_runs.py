"""Test run tools — list, search, and create test runs in a project."""

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
    TestRunsCreateResult,
    TestRunSummary,
)
from mcp_server_polarion.server import mcp
from mcp_server_polarion.tools._shared.custom_fields import (
    STANDARD_TEST_RUN_ATTRIBUTES,
    merge_custom_fields,
)
from mcp_server_polarion.tools._shared.fields import (
    MAX_BULK_ITEMS,
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
)
from mcp_server_polarion.tools._shared.pagination import (
    DEFAULT_PAGE_SIZE,
    make_page,
)
from mcp_server_polarion.tools._shared.parse import (
    extract_created_short_ids,
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
    """Create 1-50 test runs in one project in a single bulk request.

    id is required per item (Polarion REST does not auto-generate one).
    type/status are validated against the project's testing enumerations and
    template_id against existing templates (list_test_runs(templates=True)) —
    unknown ids raise ValueError. custom_fields keys are validated against a
    sample of existing runs; enum-typed custom values are not (test runs have
    no options API). Atomic: one bad item rejects the whole batch; an id-count
    mismatch raises — re-query list_test_runs before retrying.
    """
    client = get_client(ctx)
    ensure_unique_ids((spec.id for spec in items), label="id")
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

    payload = _build_create_test_runs_payload(project_id=project_id, specs=items)

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
            "Optional Lucene filter (e.g. 'status:open', 'author.id:devemberx', "
            "'HAS_VALUE:<field>' to match runs with that field populated)."
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

    Returns actual run instances by default; set templates=True for the reusable
    template blueprints instead. Filter with a Lucene query (status:open,
    type:manual, author.id:<userid>, HAS_VALUE:<field>) or omit for all.
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
