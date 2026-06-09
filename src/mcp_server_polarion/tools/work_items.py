"""Work item tools — query, create, update, and SQL recipes."""

from __future__ import annotations

import copy
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
    MAX_BODY_HTML_LEN,
    EnumOption,
    Hyperlink,
    JsonValue,
    PaginatedResult,
    SqlRecipeGallery,
    WorkItemCreateSpec,
    WorkItemDetail,
    WorkItemRead,
    WorkItemsCreateResult,
    WorkItemSummary,
    WorkItemUpdateResult,
)
from mcp_server_polarion.server import mcp
from mcp_server_polarion.tools._shared.guard import (
    guard_hyperlink_roles,
    guard_work_item_custom_field_keys,
    guard_work_item_enums,
)
from mcp_server_polarion.tools._shared.helpers import (
    DEFAULT_PAGE_SIZE,
    MAX_BULK_ITEMS,
    STANDARD_WORK_ITEM_ATTRIBUTES,
    WORK_ITEM_DETAIL_FIELDS,
    WORK_ITEM_LIST_FIELDS,
    build_enum_option,
    compute_has_more,
    encode_path_segment,
    extract_short_id,
    extract_total_count,
    get_client,
    merge_custom_fields,
    parse_work_item_detail,
    parse_work_item_summaries,
    safe_str,
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
    """Build one ``workitems`` resource for a bulk create POST.

    Skips unset values (no overwriting defaults). ``custom_fields`` inline via
    ``merge_custom_fields`` (raises on standard-field collision).
    ``description_html`` arrives pre-converted.
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
    """Build the JSON:API body for bulk ``POST /projects/{p}/workitems``.

    One resource per (spec, description_html) pair in a single ``data`` array.
    """
    data: list[JsonValue] = [
        _build_work_item_resource(spec=spec, description_html=html)
        for spec, html in zip(specs, descriptions_html, strict=True)
    ]
    return {"data": data}


def _extract_created_work_item_ids(response: dict[str, object]) -> list[str]:
    """Return short work-item ids (submission order) from a bulk 201 response.

    Relies on Polarion echoing ``data`` in order; the call-site count check
    catches a missing id, not a reordered one. Empty on malformed shapes.
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


def _build_update_work_item_payload(  # noqa: PLR0913
    *,
    project_id: str,
    work_item_id: str,
    title: str | None,
    description_html: str | None,
    status: str | None,
    priority: str | None,
    severity: str | None,
    due_date: str | None,
    initial_estimate: str | None,
    resolution: str | None,
    hyperlinks: list[Hyperlink] | None,
    assignee_ids: list[str] | None,
    custom_fields: dict[str, object] | None = None,
) -> dict[str, JsonValue]:
    """Build the JSON:API PATCH body for ``/projects/{p}/workitems/{work_item}``.

    Single ``data`` resource with required ``id`` ``"{project}/{work_item}"``.
    Skips unset values so an update never blanks an existing attribute.
    """
    attributes: dict[str, JsonValue] = {}
    if title:
        attributes["title"] = title
    if description_html:
        attributes["description"] = {
            "type": "text/html",
            "value": description_html,
        }
    if status:
        attributes["status"] = status
    if priority:
        attributes["priority"] = priority
    if severity:
        attributes["severity"] = severity
    if due_date:
        attributes["dueDate"] = due_date
    if initial_estimate:
        attributes["initialEstimate"] = initial_estimate
    if resolution:
        attributes["resolution"] = resolution
    if hyperlinks:
        attributes["hyperlinks"] = [
            {"role": h.role, "title": h.title, "uri": h.uri} for h in hyperlinks
        ]
    merge_custom_fields(attributes, custom_fields, STANDARD_WORK_ITEM_ATTRIBUTES)

    relationships: dict[str, JsonValue] = {}
    if assignee_ids:
        relationships["assignee"] = {
            "data": [{"type": "users", "id": uid} for uid in assignee_ids]
        }

    item: dict[str, JsonValue] = {
        "type": "workitems",
        "id": f"{project_id}/{work_item_id}",
    }
    if attributes:
        item["attributes"] = attributes
    if relationships:
        item["relationships"] = relationships

    return {"data": item}


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
        description=(
            "One or more work items to create in a single request "
            "(1-50). Pass a single-element list to create just one."
        ),
    ),
    dry_run: bool = Field(
        default=False,
        description=(
            "When True, return the payload preview without writing; the enum "
            "guard still calls Polarion's getAvailableOptions, so that "
            "endpoint must be reachable."
        ),
    ),
) -> WorkItemsCreateResult:
    """Create one or more Polarion work items (1-50) in one request, same project.

    Confirm every enum value (``type`` / ``status`` / ``severity`` / custom
    enums) via ``list_work_item_enum_options`` first — unverified ids persist as
    ghosts that never match Lucene. ``custom_fields`` keys are validated against
    the type's existing schema; a key no item of that ``type`` uses is rejected,
    and a type with no populated customs blocks the write (nothing to validate
    against). ``hyperlinks[].role`` validated against ``hyperlink-role``.

    Atomic: guards + conversion + build run before the POST; one bad item rejects
    the whole batch (nothing created). Raises if the returned id count differs
    from submitted — re-query ``list_work_items`` first.

    Items are free-floating; place in a document via ``move_work_item_to_document``
    (direct ``module`` create lands in the recycle bin). ``description`` is
    Markdown → sanitized HTML; the post-create round-trip is raw HTML via
    ``get_work_item(include_description_html=True)`` ↔ ``update_work_item``.
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
            await guard_work_item_custom_field_keys(
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
async def update_work_item(  # noqa: PLR0912, PLR0913, PLR0915
    ctx: Context,
    project_id: str = Field(min_length=1, description="Polarion project ID."),
    work_item_id: str = Field(
        min_length=1,
        description="Short ID of an EXISTING work item (e.g. 'MCPT-042').",
    ),
    title: str | None = None,
    description_html: str | None = Field(
        default=None,
        max_length=MAX_BODY_HTML_LEN,
        description="New raw Polarion HTML body (round-trip shape from get_work_item).",
    ),
    status: str | None = Field(
        default=None,
        description=(
            "New workflow status; prefer ``workflow_action`` for real transitions."
        ),
    ),
    priority: str | None = Field(
        default=None,
        description="New priority string (e.g. '50.0').",
    ),
    severity: str | None = None,
    due_date: str | None = Field(
        default=None, description="New due date 'YYYY-MM-DD'."
    ),
    initial_estimate: str | None = Field(
        default=None,
        description="New Polarion duration (e.g. '5 1/2d', '1w 2d').",
    ),
    resolution: str | None = Field(
        default=None,
        description=(
            "New resolution outcome; "
            "prefer ``workflow_action`` so workflow rules apply."
        ),
    ),
    hyperlinks: list[Hyperlink] | None = Field(  # noqa: B008
        default=None,
        description="REPLACES the hyperlink list — pass the full list, not a delta.",
    ),
    assignee_ids: list[str] | None = Field(  # noqa: B008
        default=None,
        description="REPLACES the assignee list — pass the full list, not a delta.",
    ),
    custom_fields: dict[str, object] | None = Field(  # noqa: B008
        default=None,
        description=(
            "Partial custom-field update; "
            "rich-text values must be ``{'type':'text/html','value':...}``."
        ),
    ),
    workflow_action: str | None = Field(
        default=None,
        description=(
            "Workflow action ID (e.g. 'close'); "
            "must be paired with at least one body field."
        ),
    ),
    change_type_to: str | None = Field(
        default=None,
        description=(
            "Change work-item type; RESETS status; "
            "must be paired with at least one body field."
        ),
    ),
    include_current_description_html: bool = Field(
        default=False,
        description=(
            "When True, return the post-PATCH raw HTML body in "
            "``current.description_html``."
        ),
    ),
    dry_run: bool = Field(
        default=False,
        description=(
            "When True, return the payload preview without writing; "
            "enum/custom-field guards still query Polarion, so the work item "
            "must be readable and the validation endpoint reachable."
        ),
    ),
) -> WorkItemUpdateResult:
    """Update an existing Polarion work item.

    PATCHes supplied fields, then GETs so ``current`` reflects the change.
    ``None`` / empty all mean leave unchanged — no clear path.

    ``description_html`` is RAW Polarion HTML, verbatim, no sanitization (NEVER
    pass untrusted input); round-trip with
    ``get_work_item(include_description_html=True)``. Greenfield: use
    ``create_work_items`` Markdown — paths never mix.

    ``hyperlinks`` / ``assignee_ids`` REPLACE (full list, not delta); each
    hyperlink ``role`` validated against ``hyperlink-role`` (before PATCH, on
    dry_run too). ``custom_fields`` partial; a key absent from the type's
    sampled schema is rejected (a type with no populated customs blocks it).

    ``module`` NOT exposed — use ``move_work_item_to_document`` /
    ``move_work_item_from_document``. Prefer ``workflow_action`` over raw
    ``status``; ``workflow_action`` / ``change_type_to`` MUST pair with ≥1 body
    field (empty PATCH 400s). Unknown ``status`` / ``severity`` / ``resolution``
    / ``priority`` / ``change_type_to`` raise ``ValueError`` listing valid
    options; with ``change_type_to`` set, status/severity/resolution scope to
    the target type.
    """
    changes: dict[str, JsonValue] = {}
    if title:
        changes["title"] = title
    if description_html:
        changes["description_html"] = description_html
    if status:
        changes["status"] = status
    if priority:
        changes["priority"] = priority
    if severity:
        changes["severity"] = severity
    if due_date:
        changes["due_date"] = due_date
    if initial_estimate:
        changes["initial_estimate"] = initial_estimate
    if resolution:
        changes["resolution"] = resolution
    if hyperlinks:
        changes["hyperlinks"] = [
            {"role": h.role, "title": h.title, "uri": h.uri} for h in hyperlinks
        ]
    if assignee_ids:
        changes["assignee_ids"] = list(assignee_ids)
    if custom_fields:
        # deepcopy: shallow would alias nested rich-text values into ``changes``.
        changes["custom_fields"] = cast(JsonValue, copy.deepcopy(custom_fields))
    if workflow_action:
        changes["workflow_action"] = workflow_action
    if change_type_to:
        changes["change_type_to"] = change_type_to

    if not changes:
        raise ValueError(
            "Nothing to update -- pass at least one of title, "
            "description_html, status, priority, severity, due_date, "
            "initial_estimate, resolution, hyperlinks, assignee_ids, "
            "custom_fields, workflow_action, or change_type_to."
        )

    payload = _build_update_work_item_payload(
        project_id=project_id,
        work_item_id=work_item_id,
        title=title,
        description_html=description_html,
        status=status,
        priority=priority,
        severity=severity,
        due_date=due_date,
        initial_estimate=initial_estimate,
        resolution=resolution,
        hyperlinks=hyperlinks,
        assignee_ids=assignee_ids,
        custom_fields=custom_fields,
    )

    # Polarion 400s on a PATCH body with no attributes/relationships, even when
    # only workflowAction / changeTypeTo is set — catch it here.
    payload_data = cast(dict[str, JsonValue], payload["data"])
    if "attributes" not in payload_data and "relationships" not in payload_data:
        raise ValueError(
            "Polarion's PATCH endpoint requires at least one body field "
            "(attribute or relationship) even when triggering "
            "workflow_action or change_type_to. Pair the action with one "
            "of: title, description, status, priority, severity, due_date, "
            "initial_estimate, resolution, hyperlinks, or assignee_ids."
        )

    client = get_client(ctx)
    base_path = (
        f"/projects/{encode_path_segment(project_id)}"
        f"/workitems/{encode_path_segment(work_item_id)}"
    )

    # Enum options are type-scoped; fetch the item's type once (on dry_run too,
    # so preview raises the same ValueError) and prime the custom-key cache.
    work_item_type = ""
    if status or severity or priority or resolution or change_type_to or custom_fields:
        try:
            prefetch = await client.get(
                base_path,
                params={"fields[workitems]": "@all"},
            )
        except PolarionNotFoundError as exc:
            raise ValueError(
                f"Work item '{work_item_id}' in project '{project_id}' not found. "
                "Use `list_work_items` to discover valid IDs."
            ) from exc
        except PolarionAuthError as exc:
            raise PermissionError(
                "Cannot read work item -- check your POLARION_TOKEN permissions."
            ) from exc
        except PolarionError as exc:
            raise RuntimeError(
                f"Failed to read work item for guard: {exc.message}"
            ) from exc
        prefetch_data = prefetch.get("data", {})
        if isinstance(prefetch_data, dict):
            current_detail = parse_work_item_detail(
                prefetch_data,
                project_id=project_id,
                fallback_id=work_item_id,
            )
            work_item_type = current_detail.type

        # Scope status/severity/resolution/priority by the target type
        # (change_type_to if set). Guard checks ``type`` first, so an invalid
        # change_type_to raises before being reused as the scoping axis.
        effective_type = change_type_to or work_item_type or "~"
        await guard_work_item_enums(
            client,
            project_id,
            work_item_type=effective_type,
            type=change_type_to,
            status=status,
            severity=severity,
            priority=priority,
            resolution=resolution,
        )
        # change_type_to retypes the item in the same PATCH, so custom_fields
        # belong to the new type's schema; validate against it, not the current.
        if custom_fields:
            await guard_work_item_custom_field_keys(
                client,
                project_id,
                change_type_to or work_item_type,
                custom_fields,
            )

    if hyperlinks:
        await guard_hyperlink_roles(client, project_id, [h.role for h in hyperlinks])

    if dry_run:
        return WorkItemUpdateResult(
            updated=False,
            dry_run=True,
            current=None,
            changes=changes,
            payload_preview=payload,
        )

    query_params: dict[str, str] = {}
    if workflow_action:
        query_params["workflowAction"] = workflow_action
    if change_type_to:
        query_params["changeTypeTo"] = change_type_to
    patch_path = f"{base_path}?{urlencode(query_params)}" if query_params else base_path

    try:
        await client.patch(patch_path, json=cast(dict[str, object], payload))
        response = await client.get(
            base_path,
            params={
                "fields[workitems]": WORK_ITEM_DETAIL_FIELDS,
                "include": "assignee",
            },
        )
    except PolarionAuthError as exc:
        raise PermissionError(
            "Cannot update work item -- check your POLARION_TOKEN permissions."
        ) from exc
    except PolarionNotFoundError as exc:
        raise ValueError(
            f"Work item '{work_item_id}' in project '{project_id}' not found. "
            "Use `list_work_items` to discover valid IDs."
        ) from exc
    except PolarionError as exc:
        raise RuntimeError(f"Failed to update work item: {exc.message}") from exc

    data = response.get("data", {})
    if not isinstance(data, dict):
        data = {}
    current = parse_work_item_detail(
        data,
        project_id=project_id,
        fallback_id=work_item_id,
    )
    if not include_current_description_html:
        # Blank the body (still came over the wire) to keep metadata-only
        # updates small — mirrors get_work_item.
        current = current.model_copy(update={"description_html": ""})

    return WorkItemUpdateResult(
        updated=True,
        dry_run=False,
        current=current,
        changes=changes,
        payload_preview=None,
    )


@mcp.tool(
    tags={"read"},
    timeout=60.0,
    annotations={"readOnlyHint": True},
)
async def list_work_item_enum_options(  # noqa: PLR0913
    ctx: Context,
    project_id: str = Field(description="Polarion project ID."),
    field_id: str = Field(
        description=(
            "Field id (e.g. 'status', 'type', 'severity', 'priority',"
            " or a custom field id)."
        ),
    ),
    work_item_type: str = Field(
        description=(
            "Work item type id (e.g. 'task', 'requirement')."
            " Pass '~' for type-agnostic options."
        ),
    ),
    page_size: int = Field(default=DEFAULT_PAGE_SIZE, ge=1, le=100),
    page_number: int = Field(default=1, ge=1),
) -> PaginatedResult[EnumOption]:
    """List valid enum options for a work item field of the given type.

    Call before ``create_work_items`` / ``update_work_item`` to resolve a
    ``type`` / ``status`` / ``severity`` / ``priority`` / custom-enum value —
    Polarion does NOT validate on write (unknown ids persist as ghosts).
    Work-item fields only. Returns the FULL set; ``work_item_type='~'`` is
    type-agnostic, and an unknown type silently falls back to ``~``, so verify
    the type id first.
    """
    client = get_client(ctx)
    path = (
        f"/projects/{encode_path_segment(project_id)}"
        f"/workitems/fields/{encode_path_segment(field_id)}"
        "/actions/getAvailableOptions"
    )
    params: dict[str, str | int] = {
        "type": work_item_type,
        "page[size]": page_size,
        "page[number]": page_number,
    }
    try:
        response = await client.get(path, params=params)
    except PolarionNotFoundError as exc:
        raise ValueError(
            f"No enum options for field '{field_id}' on work item type "
            f"'{work_item_type}' in project '{project_id}'."
        ) from exc
    except PolarionAuthError as exc:
        raise PermissionError(
            "Cannot list work item enum options"
            " -- check your POLARION_TOKEN permissions."
        ) from exc
    except PolarionError as exc:
        raise RuntimeError(
            f"Failed to list enum options for field '{field_id}': {exc.message}"
        ) from exc

    data = response.get("data", [])
    items: list[EnumOption] = []
    if isinstance(data, list):
        for entry in data:
            if isinstance(entry, dict):
                items.append(build_enum_option(entry))

    raw_total = extract_total_count(response)
    total = raw_total
    if total <= 0 and items:
        total = (page_number - 1) * page_size + len(items)

    return PaginatedResult[EnumOption](
        items=items,
        total_count=total,
        page=page_number,
        page_size=page_size,
        has_more=compute_has_more(
            response, raw_total, page_number, page_size, len(items)
        ),
    )


@mcp.tool(
    tags={"read"},
    annotations={"readOnlyHint": True},
)
async def get_sql_query_recipes() -> SqlRecipeGallery:
    """Fetch copy-paste SQL recipes for the ``list_work_items`` ``SQL:(...)`` prefix.

    Call this before writing a module-scoped, custom-field, or traceability
    SQL query, then adapt a recipe instead of hand-writing joins from memory.
    Returns the table schema plus parameterised recipes as one Markdown
    document; loaded on demand so it never occupies always-on context.
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
    """List and search work items in a Polarion project.

    Lucene ``query`` (`type:requirement`, `title:SRS*`) or omit for all. Leading
    wildcards 400. ``module`` and body text are NOT indexed — use the SQL prefix
    for module scope, ``read_document_parts`` for body.

    **SQL prefix.** A ``query`` starting with ``SQL:(`` runs native SQL for
    patterns Lucene can't express. Escape ``'`` as ``''`` (no bind params).
    ``C_DESCRIPTION LIKE`` does NOT match; ``LIKE`` is rejected inside
    ``EXISTS`` — keep it top-level via ``INNER JOIN``. Before writing any SQL
    call ``get_sql_query_recipes`` and adapt a recipe — it holds the common
    patterns (document scope, custom-field, traceability); do not hand-write.
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

    # Fall back to the seen-item count only when the API total is missing/zero
    # and the page is non-empty.
    raw_wi_total = extract_total_count(response)
    work_item_total = raw_wi_total
    if work_item_total == 0 and items:
        work_item_total = (page_number - 1) * page_size + len(items)

    return PaginatedResult[WorkItemSummary](
        items=items,
        total_count=work_item_total,
        page=page_number,
        page_size=page_size,
        has_more=compute_has_more(
            response, raw_wi_total, page_number, page_size, len(items)
        ),
    )


@mcp.tool(
    tags={"read"},
    timeout=60.0,
    annotations={"readOnlyHint": True},
)
async def get_work_item(
    ctx: Context,
    project_id: str = Field(description="Polarion project ID."),
    work_item_id: str = Field(description="Work Item ID (e.g. 'MCPT-001')."),
    include_description_html: bool = Field(
        default=False,
        description=(
            "When True, fill ``description_html`` with raw HTML for round-trip editing."
        ),
    ),
) -> WorkItemDetail:
    """Get full details of a single Polarion work item.

    With ``include_description_html=True``, ``description_html`` carries the raw
    Polarion HTML body — the round-trip shape for ``update_work_item``. Only
    feed it back when the flag was True (False blanks it; ``""`` = unchanged).
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
    work_item_id: str = Field(description="Work Item ID (e.g. 'MCPT-001')."),
) -> WorkItemRead:
    """Read a Polarion work item with its body rendered as Markdown.

    Synthesis variant of ``get_work_item``: same metadata plus ``description``
    as Markdown. Read-only (collapses Polarion spans/anchors) — do NOT feed
    back to ``update_work_item``; round-trip via the HTML pair instead.
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
