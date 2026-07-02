"""Work item tools — query, create, update, and SQL recipes."""

from __future__ import annotations

import logging
from importlib import resources
from typing import Final, cast
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
    SqlRecipeGallery,
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
)
from mcp_server_polarion.tools._shared.helpers import (
    encode_path_segment,
    get_client,
    safe_str,
)
from mcp_server_polarion.tools._shared.pagination import (
    DEFAULT_PAGE_SIZE,
    make_page,
)
from mcp_server_polarion.tools._shared.parse import (
    extract_short_id,
    parse_work_item_detail,
    parse_work_item_summaries,
)
from mcp_server_polarion.utils import (
    html_to_markdown,
    markdown_to_html,
    sanitize_html,
)

logger = logging.getLogger("mcp_server_polarion.tools.work_items")


def _build_work_item_resource(
    *,
    spec: WorkItemCreateSpec,
    description_html: str,
) -> dict[str, JsonValue]:
    """One ``workitems`` resource for a bulk create POST; skips unset (no
    overwriting defaults). ``description_html`` arrives pre-converted.
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


def _extract_created_work_item_ids(response: dict[str, object]) -> list[str]:
    """Short work-item ids from a bulk 201 response, relying on Polarion echoing
    ``data`` in submission order (call-site count check catches missing ids,
    not reordered ones).
    """
    data = response.get("data")
    if not isinstance(data, list):
        return []
    ids: list[str] = []
    for item in data:
        if isinstance(item, dict):
            full_id = safe_str(item.get("id", ""))
            if full_id:
                ids.append(extract_short_id(full_id))
    return ids


def _build_update_work_item_resource(
    *,
    project_id: str,
    spec: WorkItemUpdateSpec,
) -> dict[str, JsonValue]:
    """One ``workitems`` resource for a bulk update PATCH; skips unset so an
    update never blanks an existing attribute.
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


_SQL_QUERY_RECIPES: Final[str] = (
    resources.files("mcp_server_polarion.tools")
    .joinpath("guides", "sql_query_recipes.md")
    .read_text(encoding="utf-8")
)


@mcp.tool(
    tags={"write"},
    timeout=60.0,
    annotations={
        # Additive: non-destructive, but non-idempotent (a retry duplicates).
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
    """Create 1-50 work items in one project in a single bulk request.

    Standard enums (type/status/severity/priority) are validated — unknown ids
    raise ValueError with valid options. custom_fields keys are validated
    against the type's schema. Atomic: one bad item rejects the whole batch; an
    id-count mismatch raises — re-query list_work_items before retrying.

    Items are created free-floating; place into a document with
    move_work_item_to_document (this tool cannot). description is Markdown →
    sanitized HTML; later edits are raw-HTML round-trip via
    get_work_item(include_description_html=True) ↔ update_work_items.
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
        sanitize_html(markdown_to_html(spec.description)) if spec.description else ""
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

    new_ids = _extract_created_work_item_ids(response)
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
async def update_work_items(  # noqa: PLR0912, PLR0913
    ctx: Context,
    project_id: str = Field(min_length=1, description="Polarion project ID."),
    items: list[WorkItemUpdateSpec] = Field(  # noqa: B008
        min_length=1,
        max_length=MAX_BULK_ITEMS,
        description="Work items to update in one request (1-50).",
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
    """Update fields on 1-50 existing work items in one bulk request.

    Fetch current state with get_work_item BEFORE updating. Per item, unset
    fields stay unchanged. description_html is raw Polarion HTML, sent verbatim
    — source from get_work_item(include_description_html=True); greenfield
    bodies use create_work_items Markdown, formats never mix.

    hyperlinks/assignee_ids REPLACE the stored list — resubmit every existing
    entry plus the change or omissions are deleted. custom_fields is partial,
    keys outside the type schema rejected, values NOT validated — resolve via
    list_work_item_enum_options first.

    module not settable here — use move_work_item_to_document /
    move_work_item_from_document. workflow_action/change_type_to apply to
    EVERY item in the batch; change_type_to scopes status/severity/resolution
    to the target type and resets status. Unknown enum ids raise ValueError
    with options. Atomic: one bad item rejects the whole batch.
    """
    client = get_client(ctx)

    for spec in items:
        needs_enum_guard = bool(
            spec.status
            or spec.severity
            or spec.priority
            or spec.resolution
            or change_type_to
            or spec.custom_fields
        )
        if not needs_enum_guard:
            continue
        # Enum options are type-scoped: one prefetch per item (on dry_run too,
        # so preview raises the same errors) resolves the type and primes the
        # custom-key cache.
        item_path = (
            f"/projects/{encode_path_segment(project_id)}"
            f"/workitems/{encode_path_segment(spec.work_item_id)}"
        )
        try:
            prefetch = await client.get(
                item_path,
                params={"fields[workitems]": "@all"},
            )
        except PolarionNotFoundError as exc:
            raise ValueError(
                f"Work item '{spec.work_item_id}' in project '{project_id}' "
                "not found. Use `list_work_items` to discover valid IDs."
            ) from exc
        except PolarionAuthError as exc:
            raise PermissionError(
                "Cannot read work item -- check your POLARION_TOKEN permissions."
            ) from exc
        except PolarionError as exc:
            raise RuntimeError(
                f"Failed to read work item for guard: {exc.message}"
            ) from exc
        work_item_type = ""
        prefetch_data = prefetch.get("data", {})
        if isinstance(prefetch_data, dict):
            current_detail = parse_work_item_detail(
                prefetch_data,
                project_id=project_id,
                fallback_id=spec.work_item_id,
            )
            work_item_type = current_detail.type

        # Scope enums by target type; guard checks ``type`` first, so an invalid
        # change_type_to raises before being reused as the scoping axis.
        effective_type = change_type_to or work_item_type or "~"
        await guard_work_item_enums(
            client,
            project_id,
            work_item_type=effective_type,
            type=change_type_to,
            status=spec.status,
            severity=spec.severity,
            priority=spec.priority,
            resolution=spec.resolution,
        )
        # change_type_to retypes in the same PATCH — customs belong to the new type.
        if spec.custom_fields:
            await guard_work_item_custom_fields(
                client,
                project_id,
                change_type_to or work_item_type,
                spec.custom_fields,
            )

    await guard_hyperlink_roles(
        client,
        project_id,
        [h.role for spec in items for h in (spec.hyperlinks or [])],
    )

    payload = _build_update_work_items_payload(project_id=project_id, specs=items)

    if dry_run:
        return WorkItemsUpdateResult(
            updated=False,
            dry_run=True,
            work_item_ids=[],
            payload_preview=payload,
        )

    query_params: dict[str, str] = {}
    if workflow_action:
        query_params["workflowAction"] = workflow_action
    if change_type_to:
        query_params["changeTypeTo"] = change_type_to
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
        raise ValueError(
            f"A work item in the batch was not found in project '{project_id}'. "
            "Use `list_work_items` to discover valid IDs."
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
    annotations={"readOnlyHint": True},
)
async def get_sql_query_recipes() -> SqlRecipeGallery:
    """Fetch copy-paste SQL recipes for the list_work_items SQL:(...) prefix.

    Call before writing any SQL query (document scope, custom-field,
    traceability); adapt a recipe instead of hand-writing joins. Includes the
    table schema.
    """
    return SqlRecipeGallery(recipes=_SQL_QUERY_RECIPES)


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

    Lucene query (type:requirement, title:SRS*; leading wildcards 400) or omit
    for all. module and body text are NOT Lucene-indexed — scope by document
    via SQL:(...) or read_document_parts, never a Lucene module term.

    SQL:(...) runs native SQL: call get_sql_query_recipes first and adapt a
    recipe (document scope, custom-field, traceability), do not hand-write.
    Escape ' as ''; keep LIKE top-level via INNER JOIN (rejected inside EXISTS;
    C_DESCRIPTION LIKE never matches).
    """
    client = get_client(ctx)
    params: dict[str, str | int] = {
        "fields[workitems]": WORK_ITEM_LIST_FIELDS,
        # To-many ``assignee.data`` is only inlined when explicitly included.
        "include": "assignee",
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
    items = parse_work_item_summaries(data)

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
        description="Fill ``description_html`` with raw HTML for round-trip editing.",
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
                "include": "assignee",
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
    )
    if not include_description_html:
        # Body always travels over the wire; blank it per the False contract.
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

    get_work_item plus description as Markdown. Synthesis output (collapses
    Polarion anchors) — NEVER feed to update_work_items; round-trip via the HTML
    pair instead.
    """
    # Pull raw HTML from get_work_item so conversion needs no second round trip.
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
