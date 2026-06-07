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
from mcp_server_polarion.tools._shared.cache import (
    record_work_item_custom_field_keys,
)
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
    """Build one ``workitems`` resource object for a bulk create POST.

    Skips unset values so creation never overwrites Polarion defaults with
    empties. ``custom_fields`` inline into ``attributes`` via
    ``merge_custom_fields``, which raises on a key colliding with a standard
    field. ``description_html`` arrives pre-converted (pure data-shaping).
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
    """Build the JSON:API body for the bulk ``POST /projects/{p}/workitems``.

    Each spec, paired with its pre-converted ``description_html``, produces
    one resource object via ``_build_work_item_resource``; all are sent in a
    single ``data`` array so N work items create in one request.
    """
    data: list[JsonValue] = [
        _build_work_item_resource(spec=spec, description_html=html)
        for spec, html in zip(specs, descriptions_html, strict=True)
    ]
    return {"data": data}


def _extract_created_work_item_ids(response: dict[str, object]) -> list[str]:
    """Return short work-item ids in submission order from a bulk 201 response.

    Relies on Polarion echoing ``data`` in submission order; the call-site
    count check catches a missing id, not a reordered one. Empty on malformed
    shapes.
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

    The PATCH shape of ``_build_work_item_resource``: a single ``data``
    resource object with a required ``id`` ``"{project_id}/{work_item_id}"``.
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
    """Create one or more Polarion work items (1-50) in a single request, all
    in the same project.

    For every enum-valued field on any item (``type``, ``status``,
    ``severity``, ``custom_fields`` enum entries) you MUST first confirm the
    value via ``list_work_item_enum_options(project_id, field_id,
    work_item_type)`` — unverified ids are accepted by Polarion but persist as
    ghosts that never match Lucene. ``priority`` partly coerces (non-numeric →
    project default; numeric out-of-range still persists verbatim).
    ``custom_fields`` keys are unvalidated and defined per project+type, so
    take them from an existing work item of the same ``type`` via
    ``get_work_item``. Each ``hyperlinks[].role`` is validated against the
    project's ``hyperlink-role`` enumeration (``ValueError`` on an unknown role
    before any write).

    Bulk semantics: enum guards, Markdown conversion, and payload build all run
    BEFORE any write, and Polarion's bulk POST is atomic — a bad enum, colliding
    custom-field key, or any server-rejected attribute on one item rejects the
    whole batch with nothing created. The tool raises if the returned id count
    differs from the number submitted; re-query ``list_work_items`` first.

    Each item is created free-floating — to place one in a document at an
    outline position, follow up with ``move_work_item_to_document``. Direct
    creation into a document (via ``module``) is intentionally not exposed: such
    items land in the recycle bin, invisible in the body. Always pair
    create + move.

    Format asymmetry: ``description`` is Markdown (converted to sanitized HTML
    on write); after creation the round-trip pair is
    ``get_work_item(include_description_html=True)`` ↔
    ``update_work_item(description_html=...)`` (raw HTML verbatim). The two
    formats never mix.

    Args:
        ctx: MCP tool context (injected automatically).
        project_id: Polarion project ID shared by every item.
        items: One or more ``WorkItemCreateSpec`` entries (1-50).
        dry_run: When True, return payload preview only.

    Returns:
        WorkItemsCreateResult with ``created``, ``dry_run``,
        ``work_item_ids`` (short ids in input order; empty on dry-run),
        and ``payload_preview`` (populated on dry-run; None on real create).

    Raises:
        ValueError: Project not found, or a custom-field key collides with
            a standard Polarion attribute.
        PermissionError: Token lacks permission.
        RuntimeError: Other Polarion API errors, or a returned-id count
            that does not match the number of items submitted.
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
    for index, spec in enumerate(items):
        if spec.custom_fields:
            # Create can't hard-guard keys (no project-config endpoint, no prior
            # item to observe) the way update_work_item does — warn instead.
            logger.warning(
                "create_work_items[%d].custom_fields cannot be schema-validated "
                "(no project-config endpoint for custom-field keys); "
                "ensure keys come from an existing work item of this type via "
                "get_work_item to avoid ghost attributes. "
                "project=%s type=%s keys=%s",
                index,
                project_id,
                spec.type,
                sorted(spec.custom_fields),
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

    PATCHes the supplied fields and follows up with a GET so the caller can
    confirm the change in ``current``. ``None`` / empty string / empty list all
    mean ``leave unchanged`` — there is no path to clear a field.

    ``description_html`` is RAW Polarion HTML, sent verbatim with no
    sanitization (XSS filtering is Polarion's job — NEVER pass untrusted input);
    pair with ``get_work_item(include_description_html=True)`` for the
    round-trip. For greenfield authoring use ``create_work_items`` with a
    Markdown ``description`` — the two paths never mix.

    ``hyperlinks`` and ``assignee_ids`` REPLACE the existing lists (pass the
    full list, not a delta). Each hyperlink ``role`` is validated against the
    project's ``hyperlink-role`` enumeration (``ValueError`` before the PATCH,
    on ``dry_run`` too, since an unknown role would persist as a ghost).
    ``custom_fields`` is partial (omitted keys preserved); unknown keys are
    rejected unless seen on a prior ``get_work_item`` for this type (one priming
    read on a miss) — otherwise they would persist as ghost attributes.

    The ``module`` relationship is NOT exposed (Polarion rejects PATCHes that
    touch it) — attach/detach/move via ``move_work_item_to_document`` /
    ``move_work_item_from_document``.

    Prefer ``workflow_action`` over a raw ``status`` edit so transition rules
    run. ``workflow_action`` and ``change_type_to`` MUST be paired with at least
    one body field (Polarion rejects empty PATCH bodies). Unknown ``status`` /
    ``severity`` / ``resolution`` / ``priority`` / ``change_type_to`` ids raise
    ``ValueError`` listing the valid options before the PATCH (on ``dry_run``
    too); with ``change_type_to`` set, ``status`` / ``severity`` /
    ``resolution`` are validated against the target type's options.

    Args:
        ctx: MCP tool context (injected automatically).
        project_id: Polarion project ID.
        work_item_id: Short ID of the work item to update.
        title: Optional new title.
        description_html: Optional new raw HTML body.
        status: Optional new workflow status.
        priority: Optional new priority string.
        severity: Optional new severity.
        due_date: Optional new ISO-8601 date.
        initial_estimate: Optional new duration.
        resolution: Optional new resolution outcome.
        hyperlinks: Optional REPLACEMENT hyperlink list.
        assignee_ids: Optional REPLACEMENT assignee list.
        custom_fields: Optional partial custom-field update.
        workflow_action: Optional ``workflowAction`` query parameter.
        change_type_to: Optional ``changeTypeTo`` query parameter.
        include_current_description_html: When True, return the
            post-update body in ``current.description_html``; default
            False keeps responses small.
        dry_run: When True, return payload preview only.

    Returns:
        WorkItemUpdateResult with ``updated``, ``dry_run``, ``current``
        (post-update detail; None on dry-run), ``changes`` (parameter
        deltas), and ``payload_preview`` (populated on dry-run).

    Raises:
        ValueError: No mutating fields supplied, action without body,
            custom-field key collides with a standard attribute, or work item
            not found.
        PermissionError: Token lacks permission.
        RuntimeError: Other Polarion API errors.
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
        # deepcopy so the result's ``changes`` map is independent of the caller's
        # dict; a shallow copy would alias nested rich-text values.
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

    # Polarion 400s on a PATCH body with neither attributes nor relationships,
    # even when only workflowAction / changeTypeTo is set — catch it here.
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

    # Enum/custom-field args need the item's type (enum options are
    # type-scoped) and prime the custom-key guard cache, so fetch once —
    # on dry_run too, so preview raises the same ValueError as a real call.
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
            if work_item_type:
                record_work_item_custom_field_keys(
                    project_id,
                    work_item_type,
                    current_detail.custom_fields.keys(),
                )

        # Scope status/severity/resolution/priority by the target type
        # (``change_type_to`` if set, since the patch lands there). The guard
        # checks ``type`` first, so an invalid change_type_to raises before
        # it is reused as the scoping axis.
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
        # Fail-closed even when the prefetch could not resolve a type: pass
        # whatever type we have (possibly "") so the guard's own priming GET
        # validates the keys rather than silently skipping the check.
        if custom_fields:
            await guard_work_item_custom_field_keys(
                client,
                project_id,
                work_item_id,
                work_item_type,
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
        # Blank the returned body (it still came over the wire) to keep the
        # common metadata-only update small — mirrors get_work_item.
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
    ``type`` / ``status`` / ``severity`` / ``priority`` / custom-enum value.
    Polarion does NOT validate these on write (unknown ids persist as ghosts;
    ``priority`` only coerces non-numeric input to the project default), so
    resolve first. Work-item fields only — use ``list_document_enum_options``
    for documents. Returns the FULL set (not filtered by current workflow
    state); ``work_item_type='~'`` is the type-agnostic set, and an unknown
    type is silently treated as ``~``, so verify the type id (e.g. via
    ``get_work_item``) first.

    Args:
        ctx: MCP tool context (injected automatically).
        project_id: Polarion project ID.
        field_id: Field id whose options to list.
        work_item_type: Work item type id, or '~' for type-agnostic.
        page_size: Items per page (1-100, default 100).
        page_number: 1-based page number (default 1).

    Returns:
        PaginatedResult of ``EnumOption`` items with:
        - ``id``: Option id to pass back to write tools.
        - ``name``: Human-readable display name.
        - ``description``: Option description; empty when Polarion has none.
        - ``default``: True if Polarion uses this option as the default.
        - ``hidden``: True if the option is hidden in the UI.
        - ``terminal``: For status fields, True for workflow end-states.

    Raises:
        ValueError: Project or field not found. An unknown
            ``work_item_type`` does NOT raise; Polarion silently
            falls back to the ``~`` set.
        PermissionError: Token lacks permission.
        RuntimeError: Other Polarion API errors.
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

    Pass a Lucene ``query`` (`type:requirement`, `status:approved AND
    type:requirement`, `title:SRS*`) or omit it for all work items. Leading
    wildcards (`*foo*`) return HTTP 400. ``module`` and description body text
    are NOT indexed — use the *SQL prefix* below for module scope, and
    ``read_document_parts`` / ``read_document`` for body content.

    **SQL prefix.** A ``query`` starting with ``SQL:(`` runs as native SQL,
    unlocking patterns Lucene cannot express (module-scoped lookup,
    leading-wildcard ``LIKE``, custom-field joins, role-preserving
    traceability). Escape ``'`` as ``''``; there are no bind parameters, so
    escape any user-supplied value before substituting. ``C_DESCRIPTION LIKE``
    does NOT match (CLOB stored elsewhere — use ``read_document_parts`` for
    body search), and ``LIKE`` is rejected inside ``EXISTS (SELECT ...)`` —
    keep it in the top-level ``WHERE`` via ``INNER JOIN``. For module-scoped,
    custom-field, or traceability queries you MUST call ``get_sql_query_recipes``
    and adapt a recipe before writing SQL; do not hand-write these joins from
    memory.

    Args:
        ctx: MCP tool context (injected automatically).
        project_id: Polarion project ID.
        query: Optional Lucene filter, OR a ``SQL:(...)`` prefix for native SQL.
        page_size: Items per page (1-100, default 100).
        page_number: 1-based page number (default 1).

    Returns:
        PaginatedResult of ``WorkItemSummary`` items with ``id``,
        ``title``, ``type``, ``status``, ``priority``, ``updated``,
        ``space_id``, ``document_name``, and ``assignee_ids``.

    Raises:
        ValueError: Project not found.
        PermissionError: Token lacks permission.
        RuntimeError: Other Polarion API errors (incl. bad Lucene or SQL syntax).
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
    Polarion HTML body — the round-trip shape for
    ``update_work_item(description_html=...)``. Only feed it back when the flag
    was True (False blanks it, and the update tool treats ``""`` as
    ``leave unchanged``).

    Args:
        ctx: MCP tool context (injected automatically).
        project_id: Polarion project ID.
        work_item_id: Work Item ID (e.g. 'MCPT-001').
        include_description_html: When True, populate
            ``description_html`` with the raw HTML body. Default False.

    Returns:
        WorkItemDetail with:
        - ``id``: Work Item ID.
        - ``title``: Work Item title.
        - ``type``: Work Item type.
        - ``status``: Workflow status.
        - ``priority``: Priority value as a string (empty when unset).
        - ``updated``: ISO-8601 last-modified timestamp.
        - ``created``: ISO-8601 creation timestamp.
        - ``space_id`` / ``document_name``: Document this work item
          belongs to (both empty when not module-bound).
        - ``outline_number``: Hierarchical position inside the document
          (e.g. '1.2.3'); empty when not in a document.
        - ``assignee_ids``: Short user IDs of assignees.
        - ``author_id``: Short user ID of the author.
        - ``resolution``: Resolution outcome for closed items
          (e.g. 'fixed', 'wontfix'); empty otherwise.
        - ``severity``: Severity classification, used for defects
          (e.g. 'blocker', 'critical'); empty otherwise.
        - ``hyperlinks``: External hyperlinks as ``Hyperlink`` items
          with ``role``, ``title``, ``uri`` fields.
        - ``description_html``: Raw Polarion HTML body — populated only
          when ``include_description_html=True``, otherwise empty.
          Pass this string verbatim back to
          ``update_work_item(description_html=...)`` for a lossless
          round-trip.
        - ``project_id``: Containing project.
        - ``custom_fields``: User-defined custom fields as a
          ``{fieldId: value}`` dict. Keys vary per project and work-item
          type; values are returned verbatim (primitives or
          ``{type: 'text/html', value: '<...>'}`` for rich-text fields).
          Empty dict when no custom fields are populated.

    Raises:
        ValueError: If the work item or project is not found.
        PermissionError: If the token lacks permissions.
        RuntimeError: On unexpected Polarion API errors.
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
    # Prime the guard cache so update_work_item.custom_fields can validate keys.
    if detail.type:
        record_work_item_custom_field_keys(
            project_id, detail.type, detail.custom_fields.keys()
        )
    if not include_description_html:
        # The body always travels over the wire (no sparse-fieldset excludes it);
        # blank it here to honour the include_description_html=False contract.
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

    Synthesis variant of ``get_work_item``: same metadata fields plus
    ``description`` as Markdown (converted from HTML) instead of
    ``description_html`` — use when an LLM needs to read or summarise the body.
    The converter collapses Polarion-specific spans and ID anchors, so the
    Markdown is read-only — do NOT feed it back to ``update_work_item``. For
    round-trip editing, pair ``get_work_item(include_description_html=True)``
    with ``update_work_item(description_html=...)``.

    Args:
        ctx: MCP tool context (injected automatically).
        project_id: Polarion project ID.
        work_item_id: Work Item ID (e.g. 'MCPT-001').

    Returns:
        WorkItemRead with all the metadata fields of ``WorkItemDetail``
        (``id``, ``title``, ``type``, ``status``, ``priority``,
        ``updated``, ``created``, ``space_id`` / ``document_name``,
        ``outline_number``, ``assignee_ids``, ``author_id``,
        ``resolution``, ``severity``, ``hyperlinks``, ``project_id``,
        ``custom_fields``) plus ``description`` carrying the Markdown
        body (empty when the work item has no description).

    Raises:
        ValueError: If the work item or project is not found.
        PermissionError: If the token lacks permissions.
        RuntimeError: On unexpected Polarion API errors.
    """
    # Delegate fetch + error mapping to get_work_item; pull raw HTML so the
    # Markdown converter runs without a second round trip.
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
