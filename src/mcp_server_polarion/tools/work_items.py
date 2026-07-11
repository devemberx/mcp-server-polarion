"""Work item tools — query, create, and update."""

from __future__ import annotations

import logging
from typing import cast
from urllib.parse import urlencode

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
    WorkItemCreateSpec,
    WorkItemDetail,
    WorkItemRead,
    WorkItemsCreateResult,
    WorkItemSummary,
    WorkItemsUpdateResult,
    WorkItemUpdateSpec,
)
from mcp_server_polarion.server import mcp
from mcp_server_polarion.tools._shared.custom_fields import (
    STANDARD_WORK_ITEM_ATTRIBUTES,
    merge_custom_fields,
)
from mcp_server_polarion.tools._shared.fields import (
    MAX_BULK_ITEMS,
    WORK_ITEM_DETAIL_FIELDS,
    WORK_ITEM_LIST_FIELDS,
)
from mcp_server_polarion.tools._shared.guard import (
    guard_hyperlink_roles,
    guard_work_item_custom_fields,
    guard_work_item_enums,
    resolve_work_item_types,
)
from mcp_server_polarion.tools._shared.helpers import (
    encode_path_segment,
    ensure_unique_ids,
    get_client,
    reraise_with_item_context,
    validate_work_item_id_for_lucene,
)
from mcp_server_polarion.tools._shared.pagination import (
    DEFAULT_PAGE_SIZE,
    make_page,
)
from mcp_server_polarion.tools._shared.parse import (
    extract_created_short_ids,
    parse_included_user_name_map,
    parse_work_item_detail,
    parse_work_item_summaries,
)
from mcp_server_polarion.utils import (
    html_to_markdown,
    markdown_to_html,
    polarionify_html,
    sanitize_html,
)

logger = logging.getLogger("mcp_server_polarion.tools.work_items")


def _build_work_item_resource(
    *,
    spec: WorkItemCreateSpec,
    description_html: str,
) -> dict[str, JsonValue]:
    """One ``workitems`` resource for bulk create POST; skip unset (no
    overwriting defaults). ``description_html`` arrive pre-converted.
    """
    attributes: dict[str, JsonValue] = {
        "title": spec.title,
        "type": spec.type,
    }
    if description_html:
        attributes["description"] = {
            "type": "text/html",
            "value": description_html,
        }
    if spec.status:
        attributes["status"] = spec.status
    if spec.priority:
        attributes["priority"] = spec.priority
    if spec.severity:
        attributes["severity"] = spec.severity
    if spec.due_date:
        attributes["dueDate"] = spec.due_date
    if spec.initial_estimate:
        attributes["initialEstimate"] = spec.initial_estimate
    if spec.hyperlinks:
        attributes["hyperlinks"] = [
            {"role": h.role, "title": h.title, "uri": h.uri} for h in spec.hyperlinks
        ]
    merge_custom_fields(attributes, spec.custom_fields, STANDARD_WORK_ITEM_ATTRIBUTES)

    relationships: dict[str, JsonValue] = {}
    if spec.assignee_ids:
        relationships["assignee"] = {
            "data": [{"type": "users", "id": uid} for uid in spec.assignee_ids]
        }

    resource: dict[str, JsonValue] = {
        "type": "workitems",
        "attributes": attributes,
    }
    if relationships:
        resource["relationships"] = relationships

    return resource


def _build_create_work_items_payload(
    *,
    specs: list[WorkItemCreateSpec],
    descriptions_html: list[str],
) -> dict[str, JsonValue]:
    """JSON:API body for bulk ``POST /projects/{p}/workitems``."""
    data: list[JsonValue] = [
        _build_work_item_resource(spec=spec, description_html=html)
        for spec, html in zip(specs, descriptions_html, strict=True)
    ]
    return {"data": data}


def _build_update_work_item_resource(
    *,
    project_id: str,
    spec: WorkItemUpdateSpec,
) -> dict[str, JsonValue]:
    """One ``workitems`` resource for bulk PATCH; skip unset so update
    never blank existing attribute. Spec validator guarantee at least one
    attribute or relationship survive.
    """
    attributes: dict[str, JsonValue] = {}
    if spec.title:
        attributes["title"] = spec.title
    if spec.description_html:
        attributes["description"] = {
            "type": "text/html",
            "value": spec.description_html,
        }
    if spec.status:
        attributes["status"] = spec.status
    if spec.priority:
        attributes["priority"] = spec.priority
    if spec.severity:
        attributes["severity"] = spec.severity
    if spec.due_date:
        attributes["dueDate"] = spec.due_date
    if spec.initial_estimate:
        attributes["initialEstimate"] = spec.initial_estimate
    if spec.resolution:
        attributes["resolution"] = spec.resolution
    if spec.hyperlinks:
        attributes["hyperlinks"] = [
            {"role": h.role, "title": h.title, "uri": h.uri} for h in spec.hyperlinks
        ]
    merge_custom_fields(attributes, spec.custom_fields, STANDARD_WORK_ITEM_ATTRIBUTES)

    relationships: dict[str, JsonValue] = {}
    if spec.assignee_ids:
        relationships["assignee"] = {
            "data": [{"type": "users", "id": uid} for uid in spec.assignee_ids]
        }

    resource: dict[str, JsonValue] = {
        "type": "workitems",
        "id": f"{project_id}/{spec.work_item_id}",
    }
    if attributes:
        resource["attributes"] = attributes
    if relationships:
        resource["relationships"] = relationships

    return resource


def _build_update_work_items_payload(
    *,
    project_id: str,
    specs: list[WorkItemUpdateSpec],
) -> dict[str, JsonValue]:
    """JSON:API body for bulk ``PATCH /projects/{p}/workitems``."""
    data: list[JsonValue] = [
        _build_update_work_item_resource(project_id=project_id, spec=spec)
        for spec in specs
    ]
    return {"data": data}


def _update_query_params(
    workflow_action: str | None, change_type_to: str | None
) -> dict[str, str]:
    """Request-wide PATCH query params; both apply to every item in the batch."""
    params: dict[str, str] = {}
    if workflow_action:
        params["workflowAction"] = workflow_action
    if change_type_to:
        params["changeTypeTo"] = change_type_to
    return params


@mcp.tool(
    tags={"write"},
    timeout=60.0,
    annotations={
        # Additive: non-destructive, non-idempotent (retry duplicate).
        "readOnlyHint": False,
        "destructiveHint": False,
        "idempotentHint": False,
        "openWorldHint": True,
    },
)
async def create_work_items(
    ctx: Context,
    project_id: str = Field(min_length=1, description="Polarion project ID."),
    items: list[WorkItemCreateSpec] = Field(  # noqa: B008
        min_length=1,
        max_length=MAX_BULK_ITEMS,
        description="Work items to create in one request (1-50).",
    ),
    dry_run: bool = Field(
        default=False,
        description="Preview payload without writing; guards still query Polarion.",
    ),
) -> WorkItemsCreateResult:
    """Create 1-50 work items in one project in one bulk request.

    Items are created free-floating — place into a document with
    move_work_item_to_document. Atomic: one bad item rejects the whole
    batch.

    description is Markdown (greenfield only); later edits are raw-HTML
    round-trip via get_work_item(include_description_html=True) and
    update_work_items — formats never mix. Markdown tables get native
    Polarion styling; a paragraph starting 'Table:' directly after a table
    becomes a numbered caption widget. Enum values and custom_fields keys
    are validated on write — resolve ids via list_work_item_enum_options
    first. Returns the new work item ids.
    """
    client = get_client(ctx)
    for spec in items:
        await guard_work_item_enums(
            client,
            project_id,
            work_item_type=spec.type,
            type=spec.type,
            status=spec.status,
            severity=spec.severity,
            priority=spec.priority,
        )
    await guard_hyperlink_roles(
        client,
        project_id,
        [h.role for spec in items for h in (spec.hyperlinks or [])],
    )
    for spec in items:
        if spec.custom_fields:
            await guard_work_item_custom_fields(
                client, project_id, spec.type, spec.custom_fields
            )

    descriptions_html = [
        polarionify_html(sanitize_html(markdown_to_html(spec.description)))
        if spec.description
        else ""
        for spec in items
    ]

    payload = _build_create_work_items_payload(
        specs=items,
        descriptions_html=descriptions_html,
    )

    if dry_run:
        return WorkItemsCreateResult(
            created=False,
            dry_run=True,
            work_item_ids=[],
            payload_preview=payload,
        )

    path = f"/projects/{encode_path_segment(project_id)}/workitems"
    try:
        response = await client.post(path, json=cast(dict[str, object], payload))
    except PolarionAuthError as exc:
        raise PermissionError(
            "Cannot create work items -- check your POLARION_TOKEN permissions."
        ) from exc
    except PolarionNotFoundError as exc:
        raise ValueError(
            f"Project '{project_id}' not found. "
            "Use `list_projects` to discover valid project IDs."
        ) from exc
    except PolarionError as exc:
        raise RuntimeError(f"Failed to create work items: {exc.message}") from exc

    new_ids = extract_created_short_ids(response)
    if len(new_ids) != len(items):
        raise RuntimeError(
            f"Polarion accepted the bulk create but returned {len(new_ids)} "
            f"ids for {len(items)} requested items. The batch may be partially "
            "created; verify with list_work_items before retrying."
        )

    return WorkItemsCreateResult(
        created=True,
        dry_run=False,
        work_item_ids=new_ids,
        payload_preview=None,
    )


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
async def update_work_items(  # noqa: PLR0913
    ctx: Context,
    project_id: str = Field(min_length=1, description="Polarion project ID."),
    items: list[WorkItemUpdateSpec] = Field(  # noqa: B008
        min_length=1,
        max_length=MAX_BULK_ITEMS,
        description=(
            "Per-item changes (1-50). hyperlinks/assignee_ids REPLACE the "
            "stored lists — read each item first and pass full lists, not "
            "deltas."
        ),
    ),
    workflow_action: str | None = Field(
        default=None,
        description="Workflow action ID (e.g. 'close'); applies to EVERY item.",
    ),
    change_type_to: str | None = Field(
        default=None,
        description="New work-item type for EVERY item; RESETS status.",
    ),
    dry_run: bool = Field(
        default=False,
        description="Preview payload without writing; guards still query Polarion.",
    ),
) -> WorkItemsUpdateResult:
    """Update fields on 1-50 existing work items in one bulk PATCH; unset
    fields stay unchanged. hyperlinks/assignee_ids REPLACE the stored
    lists: even to add ONE entry, call get_work_item BEFORE updating and
    resubmit every existing entry plus the new one — anything omitted is
    silently deleted. Atomic: one bad item rejects the whole batch.

    description_html is raw Polarion HTML, sent verbatim — source from
    get_work_item(include_description_html=True); greenfield bodies use
    create_work_items Markdown, formats never mix. To add a table, caption,
    link, or widget, call get_html_recipes first and adapt its template
    before writing description_html — hand-written table markup is
    rejected.

    custom_fields is partial; unknown keys are rejected, values are not
    validated — resolve via list_work_item_enum_options first. module is
    not settable here — use move_work_item_to_document /
    move_work_item_from_document. workflow_action/change_type_to apply to
    EVERY item; change_type_to rescopes enums to the target type and
    resets status. Returns ids only — re-read via get_work_item.
    """
    client = get_client(ctx)

    # Ids embed into the id:(...) existence query below.
    for spec in items:
        validate_work_item_id_for_lucene(spec.work_item_id)
    ensure_unique_ids((spec.work_item_id for spec in items), label="work_item_id")
    payload = _build_update_work_items_payload(project_id=project_id, specs=items)

    # One batched query resolve existence + type for every item (on dry_run
    # too, so preview raise same errors); enum options type-scoped.
    types_by_id = await resolve_work_item_types(
        client, project_id, (spec.work_item_id for spec in items)
    )

    if change_type_to:
        # Request-wide param: validate once on own axis, unprefixed — bad
        # value is no single item's fault.
        await guard_work_item_enums(
            client, project_id, work_item_type=change_type_to, type=change_type_to
        )

    for index, spec in enumerate(items):
        # change_type_to retype in same PATCH — enums + customs scope to new type.
        effective_type = change_type_to or types_by_id.get(spec.work_item_id, "")
        with reraise_with_item_context(index, spec.work_item_id):
            if spec.status or spec.severity or spec.priority or spec.resolution:
                await guard_work_item_enums(
                    client,
                    project_id,
                    work_item_type=effective_type or "~",
                    status=spec.status,
                    severity=spec.severity,
                    priority=spec.priority,
                    resolution=spec.resolution,
                )
            if spec.custom_fields:
                await guard_work_item_custom_fields(
                    client, project_id, effective_type, spec.custom_fields
                )
            if spec.hyperlinks:
                # Per item (option cache make repeats free) so bad role
                # attribute to its batch position like other guards.
                await guard_hyperlink_roles(
                    client, project_id, [h.role for h in spec.hyperlinks]
                )

    query_params = _update_query_params(workflow_action, change_type_to)

    if dry_run:
        preview: dict[str, JsonValue] = dict(payload)
        if query_params:
            preview["query_params"] = cast("dict[str, JsonValue]", dict(query_params))
        return WorkItemsUpdateResult(
            updated=False,
            dry_run=True,
            work_item_ids=[],
            payload_preview=preview,
        )

    path = f"/projects/{encode_path_segment(project_id)}/workitems"
    if query_params:
        path = f"{path}?{urlencode(query_params)}"

    try:
        await client.patch(path, json=cast(dict[str, object], payload))
    except PolarionAuthError as exc:
        raise PermissionError(
            "Cannot update work items -- check your POLARION_TOKEN permissions."
        ) from exc
    except PolarionNotFoundError as exc:
        # Race fallback: existence verified above — PATCH 404 = batch changed
        # under us.
        raise ValueError(
            f"A work item in the batch was not found in project '{project_id}': "
            f"{exc.message} Use `list_work_items` to discover valid IDs."
        ) from exc
    except PolarionError as exc:
        raise RuntimeError(f"Failed to update work items: {exc.message}") from exc

    return WorkItemsUpdateResult(
        updated=True,
        dry_run=False,
        work_item_ids=[spec.work_item_id for spec in items],
        payload_preview=None,
    )


@mcp.tool(
    tags={"read"},
    timeout=60.0,
    annotations={"readOnlyHint": True},
)
async def list_work_items(
    ctx: Context,
    project_id: str = Field(description="Polarion project ID."),
    query: str | None = Field(
        default=None,
        description=(
            "Optional Lucene filter (e.g. 'type:requirement', 'title:SRS*') "
            "OR a 'SQL:(...)' prefix for native SQL."
        ),
    ),
    page_size: int = Field(default=DEFAULT_PAGE_SIZE, ge=1, le=100),
    page_number: int = Field(default=1, ge=1),
) -> PaginatedResult[WorkItemSummary]:
    """List / search work items in a project.

    Leading Lucene wildcards are rejected; module and body text are NOT
    Lucene-indexed — scope by document via SQL:(...) or
    read_document_parts, never a Lucene module term. For SQL:(...), call
    get_sql_query_recipes first and adapt a recipe — never hand-write SQL.
    For one known id, use get_work_item instead of scanning.
    """
    client = get_client(ctx)
    params: dict[str, str | int] = {
        "fields[workitems]": WORK_ITEM_LIST_FIELDS,
        # include author resolve display name; fields[users]=name trim payload.
        "include": "author",
        "fields[users]": "name",
        "page[size]": page_size,
        "page[number]": page_number,
    }
    if query is not None:
        params["query"] = query
    try:
        response = await client.get(
            f"/projects/{encode_path_segment(project_id)}/workitems",
            params=params,
        )
    except PolarionNotFoundError as exc:
        raise ValueError(
            f"Project '{project_id}' not found. "
            "Use `list_projects` to discover valid project IDs."
        ) from exc
    except PolarionAuthError as exc:
        raise PermissionError(
            "Cannot list work items -- check your POLARION_TOKEN permissions."
        ) from exc
    except PolarionError as exc:
        raise RuntimeError(f"Failed to list work items: {exc.message}") from exc

    data = response.get("data", [])
    items = parse_work_item_summaries(data, parse_included_user_name_map(response))

    return make_page(items, response, page_number, page_size)


@mcp.tool(
    tags={"read"},
    timeout=60.0,
    annotations={"readOnlyHint": True},
)
async def get_work_item(
    ctx: Context,
    project_id: str = Field(description="Polarion project ID."),
    work_item_id: str = Field(description="Work item ID (e.g. 'MCPT-001')."),
    include_description_html: bool = Field(
        default=False,
        description="Fill description_html with raw HTML for round-trip editing.",
    ),
) -> WorkItemDetail:
    """Get full details of one work item by ID.

    include_description_html=True fills description_html with raw HTML — the
    required source for update_work_items description_html. Never feed back
    a blanked (flag=False) body.
    """
    client = get_client(ctx)
    path = (
        f"/projects/{encode_path_segment(project_id)}"
        f"/workitems/{encode_path_segment(work_item_id)}"
    )
    try:
        response = await client.get(
            path,
            params={
                "fields[workitems]": WORK_ITEM_DETAIL_FIELDS,
                "include": "assignee,author",
                "fields[users]": "name",
            },
        )
    except PolarionNotFoundError as exc:
        raise ValueError(
            f"Work item '{work_item_id}' not found in project "
            f"'{project_id}'. "
            "Use `list_work_items` to discover valid IDs."
        ) from exc
    except PolarionAuthError as exc:
        raise PermissionError(
            "Cannot access work item -- check your POLARION_TOKEN permissions."
        ) from exc
    except PolarionError as exc:
        raise RuntimeError(
            f"Failed to get work item '{work_item_id}': {exc.message}"
        ) from exc

    data = response.get("data", {})
    if not isinstance(data, dict):
        data = {}

    detail = parse_work_item_detail(
        data,
        project_id=project_id,
        fallback_id=work_item_id,
        user_names=parse_included_user_name_map(response),
    )
    if not include_description_html:
        # Body always travel over wire; blank it per the False contract.
        detail = detail.model_copy(update={"description_html": ""})
    return detail


@mcp.tool(
    tags={"read"},
    timeout=60.0,
    annotations={"readOnlyHint": True},
)
async def read_work_item(
    ctx: Context,
    project_id: str = Field(description="Polarion project ID."),
    work_item_id: str = Field(description="Work item ID (e.g. 'MCPT-001')."),
) -> WorkItemRead:
    """Read one work item with its body rendered as Markdown.

    Synthesis output — collapses Polarion anchors; NEVER feed it to
    update_work_items. Edits round-trip via
    get_work_item(include_description_html=True) instead.
    """
    # Pull raw HTML from get_work_item — conversion need no second round trip.
    detail = await get_work_item(
        ctx,
        project_id=project_id,
        work_item_id=work_item_id,
        include_description_html=True,
    )
    description = (
        html_to_markdown(detail.description_html) if detail.description_html else ""
    )
    return WorkItemRead(
        **detail.model_dump(exclude={"description_html"}),
        description=description,
    )
