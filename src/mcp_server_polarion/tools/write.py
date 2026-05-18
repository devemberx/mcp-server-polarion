"""Write MCP tools for Polarion ALM.

Currently provides ``create_work_item``, ``update_work_item``,
``move_work_item_to_document``, ``update_document``, and
``create_document``. All write tools follow the strict patterns
documented in ``CLAUDE.md``: they convert Markdown input to sanitized
HTML, build minimal request payloads (skipping unset fields rather
than sending empty values), and map domain exceptions to user-facing
ones at the tool layer.
"""

from __future__ import annotations

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
    DocumentCreateResult,
    DocumentUpdateResult,
    Hyperlink,
    JsonValue,
    WorkItemCreateResult,
    WorkItemMoveResult,
    WorkItemUpdateResult,
)
from mcp_server_polarion.server import mcp
from mcp_server_polarion.tools._helpers import (
    STANDARD_DOCUMENT_ATTRIBUTES,
    STANDARD_WORK_ITEM_ATTRIBUTES,
    WORK_ITEM_DETAIL_FIELDS,
    encode_path_segment,
    extract_short_id,
    get_client,
    merge_custom_fields,
    parse_work_item_detail,
    safe_str,
    split_module_id,
)
from mcp_server_polarion.utils import markdown_to_html, sanitize_html

# Caps tool-layer body payloads so a prompt-injected caller cannot ship a
# multi-megabyte blob to Polarion. Observed real document bodies stay
# under ~30 KB, so 2 MiB leaves ~70x headroom.
MAX_BODY_HTML_LEN: Final[int] = 2_000_000

# ---------------------------------------------------------------------------
# Private helpers
# ---------------------------------------------------------------------------


def _build_work_item_payload(  # noqa: PLR0913
    *,
    title: str,
    type: str,
    description_html: str,
    status: str | None,
    priority: str | None,
    severity: str | None,
    assignee_ids: list[str] | None,
    due_date: str | None,
    initial_estimate: str | None,
    hyperlinks: list[Hyperlink] | None,
    custom_fields: dict[str, object] | None = None,
) -> dict[str, JsonValue]:
    """Build the JSON:API request body for ``POST /projects/{p}/workitems``.

    Only attaches keys for values that are explicitly set — ``None``,
    empty strings, and empty lists are skipped so we never overwrite
    Polarion defaults with empty values on creation. ``custom_fields``
    entries are inlined into ``attributes`` alongside the standard
    fields via ``merge_custom_fields``; colliding keys raise
    ``ValueError`` so the caller cannot accidentally shadow an explicit
    standard parameter.
    """
    attributes: dict[str, JsonValue] = {
        "title": title,
        "type": type,
    }
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
        "attributes": attributes,
    }
    if relationships:
        item["relationships"] = relationships

    return {"data": [item]}


def _extract_created_id(response: dict[str, object]) -> str | None:
    """Extract the short work-item ID from a 201 create response.

    Polarion returns ``{"data": [{"type": "workitems",
    "id": "projectId/MCPT-042", ...}]}``.  Returns the short ID
    (``"MCPT-042"``) or ``None`` if the response shape is unexpected.
    """
    data = response.get("data")
    if not isinstance(data, list) or not data:
        return None
    first = data[0]
    if not isinstance(first, dict):
        return None
    full_id = safe_str(first.get("id", ""))
    if not full_id:
        return None
    return extract_short_id(full_id)


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

    Mirrors ``_build_work_item_payload`` but produces a PATCH-shaped body:
    ``data`` is a single resource object (not a list), with a required
    ``id`` of the form ``"{project_id}/{work_item_id}"``. Only attaches
    keys for values that are explicitly set — ``None``, empty strings,
    and empty lists are skipped so we never overwrite Polarion attributes
    with empty values on update. ``custom_fields`` entries are inlined
    into ``attributes`` alongside the standard fields via
    ``merge_custom_fields``; colliding keys raise ``ValueError``.
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


def _build_move_to_document_payload(
    *,
    project_id: str,
    target_space_id: str,
    target_document_name: str,
    previous_part_id: str | None,
    next_part_id: str | None,
) -> dict[str, JsonValue]:
    """Build the request body for the ``moveToDocument`` action endpoint.

    Note: this endpoint is NOT JSON:API — the body is a flat object
    with ``targetDocument``, plus exactly one of ``previousPart`` or
    ``nextPart``. The tool layer validates the exactly-one invariant
    before calling this helper, but we re-check here so a future
    direct caller cannot accidentally produce a ``".../None"``
    literal-string payload.
    """
    if (previous_part_id is None) == (next_part_id is None):
        msg = (
            "_build_move_to_document_payload requires exactly one of "
            "previous_part_id or next_part_id to be set."
        )
        raise ValueError(msg)

    target_doc = f"{project_id}/{target_space_id}/{target_document_name}"
    payload: dict[str, JsonValue] = {"targetDocument": target_doc}
    if previous_part_id is not None:
        payload["previousPart"] = f"{target_doc}/{previous_part_id}"
    else:
        payload["nextPart"] = f"{target_doc}/{next_part_id}"
    return payload


def _build_update_document_payload(  # noqa: PLR0913
    *,
    project_id: str,
    space_id: str,
    document_name: str,
    title: str | None,
    status: str | None,
    type: str | None,
    home_page_content_html: str | None = None,
    custom_fields: dict[str, object] | None = None,
) -> dict[str, JsonValue]:
    """Build the JSON:API request body for ``PATCH .../documents/{d}``.

    Mirrors ``_build_update_work_item_payload``'s PATCH shape: ``data`` is
    a single resource object (NOT a list) with a required ``id`` of the
    form ``"{project_id}/{space_id}/{document_name}"``. Only attaches
    keys for values that are explicitly set -- ``None`` values are
    skipped so JSON:API omit-preserve takes effect (the server keeps
    the existing server-side value). ``custom_fields`` entries are
    inlined into ``attributes`` via ``merge_custom_fields``; colliding
    keys raise ``ValueError``.

    ``home_page_content_html`` is treated as RAW Polarion HTML and is
    wrapped verbatim into ``{"type":"text/html","value":...}`` — no
    sanitization, no Markdown conversion. The body-clearing guard
    (rejecting empty strings) lives in the tool layer (``update_document``)
    rather than here so direct callers can opt out if needed.
    """
    attributes: dict[str, JsonValue] = {}
    if title is not None:
        attributes["title"] = title
    if status is not None:
        attributes["status"] = status
    if type is not None:
        attributes["type"] = type
    if home_page_content_html is not None:
        attributes["homePageContent"] = {
            "type": "text/html",
            "value": home_page_content_html,
        }
    merge_custom_fields(attributes, custom_fields, STANDARD_DOCUMENT_ATTRIBUTES)

    item: dict[str, JsonValue] = {
        "type": "documents",
        "id": f"{project_id}/{space_id}/{document_name}",
    }
    if attributes:
        item["attributes"] = attributes

    return {"data": item}


def _build_create_document_payload(  # noqa: PLR0913
    *,
    module_name: str,
    title: str,
    type: str,
    home_page_content_html: str,
    status: str | None,
    custom_fields: dict[str, object] | None = None,
) -> dict[str, JsonValue]:
    """Build the JSON:API request body for ``POST .../spaces/{s}/documents``.

    Mirrors ``_build_work_item_payload``'s POST shape: ``data`` is a
    single-element list with ``type=documents`` and inline ``attributes``.
    Only attaches keys for values that are explicitly set -- ``None`` and
    empty strings are skipped so we never overwrite Polarion defaults
    with empty values on creation. ``custom_fields`` entries are inlined
    into ``attributes`` alongside the standard fields via
    ``merge_custom_fields``; colliding keys raise ``ValueError``.
    """
    attributes: dict[str, JsonValue] = {
        "moduleName": module_name,
        "title": title,
        "type": type,
    }
    if status:
        attributes["status"] = status
    if home_page_content_html:
        attributes["homePageContent"] = {
            "type": "text/html",
            "value": home_page_content_html,
        }
    merge_custom_fields(attributes, custom_fields, STANDARD_DOCUMENT_ATTRIBUTES)

    item: dict[str, JsonValue] = {
        "type": "documents",
        "attributes": attributes,
    }
    return {"data": [item]}


# ---------------------------------------------------------------------------
# Write tools
# ---------------------------------------------------------------------------


@mcp.tool(
    tags={"write"},
    timeout=60.0,
    annotations={
        # Pure additive operation per MCP spec — creates a new work item without
        # mutating existing data, so destructiveHint is False. Not idempotent
        # because retrying with the same input creates a duplicate.
        "readOnlyHint": False,
        "destructiveHint": False,
        "idempotentHint": False,
        "openWorldHint": True,
    },
)
async def create_work_item(  # noqa: PLR0913
    ctx: Context,
    project_id: str = Field(description="Polarion project ID."),
    title: str = Field(
        min_length=1, description="Work item title (required, non-empty)."
    ),
    type: str = Field(
        min_length=1,
        description="Work item type (e.g. 'requirement', 'task', 'testCase').",
    ),
    description: str | None = Field(
        default=None,
        max_length=MAX_BODY_HTML_LEN,
        description="Optional Markdown body; converted to sanitized HTML on write.",
    ),
    status: str | None = Field(
        default=None,
        description=(
            "Optional initial workflow status (project default applies if omitted)."
        ),
    ),
    priority: str | None = Field(
        default=None,
        description="Optional priority string (e.g. '50.0').",
    ),
    severity: str | None = Field(
        default=None,
        description="Optional severity classification (e.g. 'major', 'critical').",
    ),
    assignee_ids: list[str] | None = Field(  # noqa: B008
        default=None,
        description="Optional short user IDs to assign (e.g. ['alice', 'bob']).",
    ),
    due_date: str | None = Field(
        default=None,
        description="Optional due date 'YYYY-MM-DD'.",
    ),
    initial_estimate: str | None = Field(
        default=None,
        description="Optional Polarion duration (e.g. '5 1/2d', '1w 2d', '4h').",
    ),
    hyperlinks: list[Hyperlink] | None = Field(  # noqa: B008
        default=None,
        description=(
            "Optional external hyperlinks; each must have ``role`` and ``uri``."
        ),
    ),
    custom_fields: dict[str, object] | None = Field(  # noqa: B008
        default=None,
        description=(
            "Optional custom fields keyed by Polarion field ID; "
            "rich-text values must be ``{'type':'text/html','value':...}``."
        ),
    ),
    dry_run: bool = Field(
        default=False,
        description="When True, return payload preview without calling Polarion.",
    ),
) -> WorkItemCreateResult:
    """Create a new Polarion work item in a project.

    The work item is created free-floating — to place it inside a
    document at a specific outline position, follow up with
    ``move_work_item_to_document``.

    Format asymmetry: ``description`` here is Markdown (converted to
    sanitized HTML on write) because greenfield authoring is natural for
    LLMs. After creation the round-trip pair is
    ``get_work_item(include_description_html=True)`` ↔
    ``update_work_item(description_html=...)`` which speaks raw HTML
    verbatim. The two formats never mix.

    Polarion does NOT validate enum membership server-side. Unknown
    ``type`` / ``status`` / ``severity`` ids are stored verbatim as
    ghost values that look real on later reads but never match Lucene
    queries. ``priority`` is the only partial exception: a non-numeric
    string coerces to the project default, but a numeric string outside
    the enum set (e.g. ``"999.0"``) also stores verbatim. Resolve valid
    ids first via ``list_work_item_enum_options(project_id, field_id,
    work_item_type)``. ``custom_fields`` is the same story: unknown
    field IDs — including brand-new IDs that no work item of this type
    has ever used — silently persist as ghost attributes. Pass keys
    taken from a prior ``get_work_item``.

    Args:
        ctx: MCP tool context (injected automatically).
        project_id: Polarion project ID.
        title: Work item title.
        type: Work item type.
        description: Optional Markdown body.
        status: Optional workflow status.
        priority: Optional priority string.
        severity: Optional severity classification.
        assignee_ids: Optional short user IDs.
        due_date: Optional ISO-8601 date.
        initial_estimate: Optional duration string.
        hyperlinks: Optional ``Hyperlink`` list.
        custom_fields: Optional custom-field dict.
        dry_run: When True, return payload preview only.

    Returns:
        WorkItemCreateResult with ``created``, ``dry_run``,
        ``work_item_id`` (None on dry-run), and ``payload_preview``
        (populated on dry-run; None on real create).

    Raises:
        ValueError: Project not found, or custom-field key collides with
            a standard Polarion attribute.
        PermissionError: Token lacks permission.
        RuntimeError: Other Polarion API errors, or accepted-but-no-ID.
    """
    description_html = (
        sanitize_html(markdown_to_html(description)) if description else ""
    )

    payload = _build_work_item_payload(
        title=title,
        type=type,
        description_html=description_html,
        status=status,
        priority=priority,
        severity=severity,
        assignee_ids=assignee_ids,
        due_date=due_date,
        initial_estimate=initial_estimate,
        hyperlinks=hyperlinks,
        custom_fields=custom_fields,
    )

    if dry_run:
        return WorkItemCreateResult(
            created=False,
            dry_run=True,
            work_item_id=None,
            payload_preview=payload,
        )

    client = get_client(ctx)
    path = f"/projects/{encode_path_segment(project_id)}/workitems"
    try:
        response = await client.post(path, json=cast(dict[str, object], payload))
    except PolarionAuthError as exc:
        raise PermissionError(
            "Cannot create work item -- check your POLARION_TOKEN permissions."
        ) from exc
    except PolarionNotFoundError as exc:
        raise ValueError(
            f"Project '{project_id}' not found. "
            "Use `list_projects` to discover valid project IDs."
        ) from exc
    except PolarionError as exc:
        raise RuntimeError(f"Failed to create work item: {exc.message}") from exc

    new_id = _extract_created_id(response)
    if new_id is None:
        raise RuntimeError(
            "Polarion accepted the create request but returned no work-item ID. "
            "The work item may or may not exist; verify with list_work_items."
        )

    return WorkItemCreateResult(
        created=True,
        dry_run=False,
        work_item_id=new_id,
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
    title: str | None = Field(
        default=None, description="New title (None to leave unchanged)."
    ),
    description_html: str | None = Field(
        default=None,
        max_length=MAX_BODY_HTML_LEN,
        description=(
            "New raw Polarion HTML body (round-trip shape from get_work_item); "
            "'' is a no-op."
        ),
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
    severity: str | None = Field(
        default=None, description="New severity classification."
    ),
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
        description="When True, return payload preview without calling Polarion.",
    ),
) -> WorkItemUpdateResult:
    """Update an existing Polarion work item.

    PATCHes the supplied fields and follows up with a GET so the caller
    can confirm the change in ``current``. ``None`` / empty string /
    empty list all mean ``leave unchanged`` — there is no way to clear
    a field via this tool in v1.

    ``description_html`` is RAW Polarion HTML, sent verbatim with no
    sanitization, so XSS/script filtering is delegated to Polarion's
    renderer — NEVER pass untrusted input. Pair with
    ``get_work_item(include_description_html=True)`` for the round-trip.
    For greenfield authoring use ``create_work_item(description=...)``
    (Markdown) — the two format paths never mix.

    ``hyperlinks`` and ``assignee_ids`` REPLACE the existing lists (pass
    the full list, not a delta). ``custom_fields`` is partial — omitted
    keys are preserved. Unknown custom-field IDs are silently stored as
    ghost attributes; pass keys from a prior read to avoid creating them.

    Prefer ``workflow_action`` over a raw ``status`` edit so the project's
    transition rules run. ``workflow_action`` and ``change_type_to`` MUST
    be paired with at least one body field — Polarion rejects empty PATCH
    bodies (HTTP 400 "At least one of the members is required"). Direct
    ``status`` / ``severity`` / ``resolution`` writes are NOT validated
    server-side: unknown ids are stored verbatim as ghost values.
    ``priority`` only coerces non-numeric inputs to the project default;
    numeric strings outside the enum set also store verbatim. Resolve
    valid ids first via ``list_work_item_enum_options(project_id,
    field_id, work_item_type)``.

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
        changes["custom_fields"] = cast(JsonValue, dict(custom_fields))
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

    # Polarion's PATCH endpoint rejects bodies with neither attributes
    # nor relationships ("At least one of the members is required"),
    # even when only the workflowAction / changeTypeTo query params are
    # used. Catch this at the tool layer with an actionable message
    # rather than letting Polarion 400.
    payload_data = cast(dict[str, JsonValue], payload["data"])
    if "attributes" not in payload_data and "relationships" not in payload_data:
        raise ValueError(
            "Polarion's PATCH endpoint requires at least one body field "
            "(attribute or relationship) even when triggering "
            "workflow_action or change_type_to. Pair the action with one "
            "of: title, description, status, priority, severity, due_date, "
            "initial_estimate, resolution, hyperlinks, or assignee_ids."
        )

    if dry_run:
        return WorkItemUpdateResult(
            updated=False,
            dry_run=True,
            current=None,
            changes=changes,
            payload_preview=payload,
        )

    client = get_client(ctx)
    base_path = (
        f"/projects/{encode_path_segment(project_id)}"
        f"/workitems/{encode_path_segment(work_item_id)}"
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
        # Mirror ``get_work_item(include_description_html=False)`` —
        # the body still travels over the wire (Polarion @all surfaces
        # customs), but we blank it here to keep the LLM-facing
        # ``current.description_html`` small for the common metadata-
        # only update.
        current = current.model_copy(update={"description_html": ""})

    return WorkItemUpdateResult(
        updated=True,
        dry_run=False,
        current=current,
        changes=changes,
        payload_preview=None,
    )


@mcp.tool(
    tags={"write"},
    timeout=60.0,
    annotations={
        # idempotentHint=False: the moveToDocument action endpoint is not
        # verified to be safe on repeat — a second call against an already-
        # moved work item may 400 instead of no-opping (per Polarion's heading-move
        # behaviour, see CLAUDE.md). Conservative until confirmed.
        "readOnlyHint": False,
        "destructiveHint": True,
        "idempotentHint": False,
        "openWorldHint": True,
    },
)
async def move_work_item_to_document(  # noqa: PLR0913
    ctx: Context,
    project_id: str = Field(description="Project containing the work item."),
    work_item_id: str = Field(
        min_length=1,
        description="Short ID of an EXISTING work item (e.g. 'MCPT-042').",
    ),
    target_space_id: str = Field(
        min_length=1,
        description="Target space ID (use '_default' for the default space).",
    ),
    target_document_name: str = Field(
        min_length=1,
        description="Target document name within ``target_space_id``.",
    ),
    previous_part_id: str | None = Field(
        default=None,
        description=(
            "Insert AFTER this part ID (mutually exclusive with ``next_part_id``)."
        ),
    ),
    next_part_id: str | None = Field(
        default=None,
        description=(
            "Insert BEFORE this part ID (mutually exclusive with ``previous_part_id``)."
        ),
    ),
    dry_run: bool = Field(
        default=False,
        description="When True, return payload preview without calling Polarion.",
    ),
) -> WorkItemMoveResult:
    """Move a work item into a Polarion document at a specific position.

    Calls the ``moveToDocument`` action endpoint, which updates the work item's
    ``module`` relationship, inserts a document part at the specified
    position, and assigns a proper ``outline_number`` — atomically. This
    is the ONLY supported way to attach a work item body to a document; editing
    ``homePageContent`` directly to inject a macro reference leaves the
    ``module`` relationship unset and produces an inconsistent state.

    Heading-type work items are rejected (HTTP 400 "Cannot move
    headings"); headings must be created inside their target document.
    If the work item is already in a different document, this detaches it from
    the source — the operation is a move, not a copy.

    Exactly one of ``previous_part_id`` (insert AFTER) / ``next_part_id``
    (insert BEFORE) must be provided. Discover part IDs with
    ``read_document_parts``.

    Args:
        ctx: MCP tool context (injected automatically).
        project_id: Project containing the work item.
        work_item_id: Short ID of an existing work item.
        target_space_id: Target space ID.
        target_document_name: Target document name.
        previous_part_id: Insert AFTER this part.
        next_part_id: Insert BEFORE this part.
        dry_run: When True, return payload preview only.

    Returns:
        WorkItemMoveResult with ``moved``, ``dry_run``, and
        ``payload_preview`` (populated on dry-run). Polarion returns 204
        on success — call ``read_document_parts`` for the new part ID.

    Raises:
        ValueError: Position not exactly one of two, heading work item,
            or work item / document / part not found.
        PermissionError: Token lacks permission.
        RuntimeError: Other Polarion API errors.
    """
    if (previous_part_id is None) == (next_part_id is None):
        raise ValueError(
            "Exactly one of previous_part_id or next_part_id must be "
            "provided. Use previous_part_id to insert AFTER an existing "
            "part, or next_part_id to insert BEFORE an existing part. "
            "Discover existing part IDs with `read_document_parts`."
        )

    payload = _build_move_to_document_payload(
        project_id=project_id,
        target_space_id=target_space_id,
        target_document_name=target_document_name,
        previous_part_id=previous_part_id,
        next_part_id=next_part_id,
    )

    if dry_run:
        return WorkItemMoveResult(
            moved=False,
            dry_run=True,
            payload_preview=payload,
        )

    client = get_client(ctx)
    path = (
        f"/projects/{encode_path_segment(project_id)}"
        f"/workitems/{encode_path_segment(work_item_id)}"
        "/actions/moveToDocument"
    )
    try:
        await client.post(path, json=cast(dict[str, object], payload))
    except PolarionAuthError as exc:
        raise PermissionError(
            "Cannot move work item -- check your POLARION_TOKEN permissions."
        ) from exc
    except PolarionNotFoundError as exc:
        raise ValueError(
            f"Work item '{work_item_id}' (project '{project_id}') or "
            f"target document '{target_document_name}' (space "
            f"'{target_space_id}') or referenced part not found. "
            "Verify with `get_work_item`, `list_documents`, and "
            "`read_document_parts`."
        ) from exc
    except PolarionError as exc:
        raise RuntimeError(f"Failed to move work item: {exc.message}") from exc

    return WorkItemMoveResult(
        moved=True,
        dry_run=False,
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
async def update_document(  # noqa: PLR0913
    ctx: Context,
    project_id: str = Field(description="Polarion project ID."),
    space_id: str = Field(
        min_length=1,
        description="Space ID (use '_default' for the default space).",
    ),
    document_name: str = Field(
        min_length=1,
        description="Document name within ``space_id``.",
    ),
    title: str | None = Field(
        default=None, description="New title (None to leave unchanged)."
    ),
    status: str | None = Field(
        default=None,
        description=(
            "New workflow status; prefer ``workflow_action`` for real transitions."
        ),
    ),
    type: str | None = Field(
        default=None, description="New document type (e.g. 'req_specification')."
    ),
    home_page_content_html: str | None = Field(
        default=None,
        max_length=MAX_BODY_HTML_LEN,
        description=(
            "New body as raw Polarion HTML (round-trip shape from get_document); "
            "'' is rejected."
        ),
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
            "Workflow action ID; must be paired with at least one attribute field."
        ),
    ),
    dry_run: bool = Field(
        default=False,
        description="When True, return payload preview without calling Polarion.",
    ),
) -> DocumentUpdateResult:
    """Update a Polarion document's metadata or body.

    PATCHes only the attributes you set; omitted fields are preserved
    server-side. Unlike ``update_work_item`` this does NOT follow up
    with a GET — call ``get_document`` if you need the refreshed state.

    ``home_page_content_html`` is the round-trip pair for
    ``get_document(include_homepage_content_html=True)``. The HTML is
    sent verbatim with no sanitization, so XSS/script filtering is
    delegated to Polarion's renderer — NEVER pass untrusted input.
    Empty string is rejected (would wipe the body and orphan every
    heading); pass ``'<p></p>'`` for a near-empty body.

    Body-write behaviour:

    - **Heading auto-create**: inline ``<h1>..<h4>`` tags become heading
      work items with ``module`` and ``outline_number`` set automatically.
      A bare ``<hN>Title</hN>`` alone is safe.
    - **Orphan headings**: removing an ``<hN>`` removes the part but
      leaves the heading work item behind (still ``module``-linked, no
      ``outline_number``).
    - **DO NOT inject anchorless ``<p>`` paragraphs**: ``<h3>X</h3>
      <p>Body</p>`` lets the PATCH succeed but the next
      ``read_document_parts`` returns HTTP 500. Polarion's stored
      paragraphs all carry ``id="polarion_..."`` anchors; raw ``<p>``
      breaks server-side part derivation. For body text, create a new
      work item and attach via ``create_work_item`` +
      ``move_work_item_to_document``.
    - **DO NOT inject work item macro references**: appending
      ``<div id="polarion_wiki macro name=module-workitem;params=id=NEW">``
      creates a ``workitem_<NEW>`` part visible in
      ``read_document_parts`` but leaves the work item's ``module`` relationship
      unset (``space_id=""``, ``outline_number=""``) — an inconsistent
      half-attached state. Always attach via
      ``move_work_item_to_document``.

    Workflow: prefer ``workflow_action`` over a raw ``status`` edit so
    project rules run. ``workflow_action`` MUST be paired with at least
    one attribute field — Polarion rejects empty PATCH bodies. Direct
    ``status`` / ``type`` writes are NOT validated server-side: unknown
    ids are stored verbatim as ghost values. Resolve valid ids first via
    ``list_document_enum_options(project_id, field_id, document_type)``.
    Unknown ``custom_fields`` IDs also become ghost attributes; pass
    keys from a prior ``get_document``.

    Args:
        ctx: MCP tool context (injected automatically).
        project_id: Polarion project ID.
        space_id: Space ID containing the document.
        document_name: Document name within ``space_id``.
        title: Optional new title.
        status: Optional new workflow status.
        type: Optional new document type.
        home_page_content_html: Optional new body as raw Polarion HTML.
        custom_fields: Optional partial custom-field update.
        workflow_action: Optional action ID; must be paired with a
            body field.
        dry_run: When True, return payload preview only.

    Returns:
        DocumentUpdateResult with ``updated``, ``dry_run``, and
        ``payload_preview`` (populated on dry-run; None on real update).

    Raises:
        ValueError: No fields supplied, action without body, empty
            ``home_page_content_html``, custom-field key collision, or
            document / space / project not found.
        PermissionError: Token lacks permission.
        RuntimeError: Other Polarion API errors.
    """
    if home_page_content_html == "":
        raise ValueError(
            "home_page_content_html='' would wipe the document body and "
            "orphan all heading work items. Pass at minimum '<p></p>' "
            "or omit the parameter to leave the body unchanged."
        )

    has_attrs = (
        title is not None
        or status is not None
        or type is not None
        or home_page_content_html is not None
        or bool(custom_fields)
    )
    if not has_attrs and not workflow_action:
        raise ValueError(
            "update_document requires at least one of: title, status, "
            "type, home_page_content_html, custom_fields, or "
            "workflow_action."
        )
    if not has_attrs and workflow_action:
        raise ValueError(
            "workflow_action alone is not supported -- Polarion rejects "
            "PATCH bodies with no attributes. Pair workflow_action with "
            "at least one of title, status, type, home_page_content_html, "
            "or custom_fields."
        )

    payload = _build_update_document_payload(
        project_id=project_id,
        space_id=space_id,
        document_name=document_name,
        title=title,
        status=status,
        type=type,
        home_page_content_html=home_page_content_html,
        custom_fields=custom_fields,
    )

    if dry_run:
        return DocumentUpdateResult(
            updated=False,
            dry_run=True,
            payload_preview=payload,
        )

    client = get_client(ctx)
    base_path = (
        f"/projects/{encode_path_segment(project_id)}"
        f"/spaces/{encode_path_segment(space_id)}"
        f"/documents/{encode_path_segment(document_name)}"
    )
    query_params: dict[str, str] = {}
    if workflow_action:
        query_params["workflowAction"] = workflow_action
    patch_path = f"{base_path}?{urlencode(query_params)}" if query_params else base_path

    try:
        await client.patch(patch_path, json=cast(dict[str, object], payload))
    except PolarionAuthError as exc:
        raise PermissionError(
            "Cannot update document -- check your POLARION_TOKEN permissions."
        ) from exc
    except PolarionNotFoundError as exc:
        raise ValueError(
            f"Document '{document_name}' (space '{space_id}', "
            f"project '{project_id}') not found. "
            "Use `list_documents` to discover valid IDs."
        ) from exc
    except PolarionError as exc:
        raise RuntimeError(f"Failed to update document: {exc.message}") from exc

    return DocumentUpdateResult(
        updated=True,
        dry_run=False,
        payload_preview=None,
    )


@mcp.tool(
    tags={"write"},
    timeout=60.0,
    annotations={
        # Pure additive operation per MCP spec -- creates a new document without
        # mutating existing data, so destructiveHint is False. Not idempotent
        # because retrying with the same module_name returns HTTP 409.
        "readOnlyHint": False,
        "destructiveHint": False,
        "idempotentHint": False,
        "openWorldHint": True,
    },
)
async def create_document(  # noqa: PLR0913
    ctx: Context,
    project_id: str = Field(min_length=1, description="Polarion project ID."),
    space_id: str = Field(
        min_length=1,
        description="Space ID (use '_default' for the default space).",
    ),
    module_name: str = Field(
        min_length=1,
        description=(
            "Polarion document identifier (e.g. 'MySpecV1'); "
            "must be unique within ``space_id`` and appears in the document URL."
        ),
    ),
    title: str = Field(
        min_length=1,
        description="Human-readable document title (required, non-empty).",
    ),
    type: str = Field(
        min_length=1,
        description="Document type (e.g. 'req_specification', 'generic').",
    ),
    status: str | None = Field(
        default=None,
        description=(
            "Optional initial workflow status (project default applies if omitted)."
        ),
    ),
    home_page_content: str | None = Field(
        default=None,
        max_length=MAX_BODY_HTML_LEN,
        description="Optional Markdown body; converted to sanitized HTML on write.",
    ),
    custom_fields: dict[str, object] | None = Field(  # noqa: B008
        default=None,
        description=(
            "Optional custom fields keyed by Polarion field ID; "
            "rich-text values must be ``{'type':'text/html','value':...}``."
        ),
    ),
    dry_run: bool = Field(
        default=False,
        description="When True, return payload preview without calling Polarion.",
    ),
) -> DocumentCreateResult:
    """Create a new Polarion document in a space.

    Greenfield document creation. The document starts empty (or with the
    optional ``home_page_content`` body) and headings / work item parts
    can be added later via ``update_document`` and
    ``move_work_item_to_document``.

    Format asymmetry: ``home_page_content`` here is Markdown (converted
    to sanitized HTML on write) because greenfield authoring is natural
    for LLMs. After creation the round-trip pair is
    ``get_document(include_homepage_content_html=True)`` ↔
    ``update_document(home_page_content_html=...)`` which speaks raw HTML
    verbatim. The two formats never mix.

    ``module_name`` is Polarion's persistent identifier within the space
    and is used in every subsequent URL (``get_document``,
    ``update_document``, etc.). It must be unique within ``space_id``;
    a duplicate name causes Polarion to return HTTP 409, surfaced here
    as ``RuntimeError``.

    Polarion does NOT validate enum membership server-side. Unknown
    ``type`` / ``status`` ids are stored verbatim as ghost values that
    look real on later reads but never match Lucene queries. Resolve
    valid ids first via ``list_document_enum_options(project_id,
    field_id, document_type)``. ``custom_fields`` is the same story:
    unknown field IDs silently persist as ghost attributes -- pass keys
    taken from a prior ``get_document``.

    Args:
        ctx: MCP tool context (injected automatically).
        project_id: Polarion project ID.
        space_id: Space ID containing the new document.
        module_name: Polarion document identifier (unique within space).
        title: Human-readable document title.
        type: Document type.
        status: Optional initial workflow status.
        home_page_content: Optional Markdown body.
        custom_fields: Optional custom-field dict.
        dry_run: When True, return payload preview only.

    Returns:
        DocumentCreateResult with ``created``, ``dry_run``,
        ``document_name`` (None on dry-run), and ``payload_preview``
        (populated on dry-run; None on real create).

    Raises:
        ValueError: Project or space not found, or custom-field key
            collides with a standard Polarion attribute.
        PermissionError: Token lacks permission.
        RuntimeError: Other Polarion API errors (including duplicate
            ``module_name``), or accepted-but-no-ID response.
    """
    home_page_content_html = (
        sanitize_html(markdown_to_html(home_page_content)) if home_page_content else ""
    )

    payload = _build_create_document_payload(
        module_name=module_name,
        title=title,
        type=type,
        home_page_content_html=home_page_content_html,
        status=status,
        custom_fields=custom_fields,
    )

    if dry_run:
        return DocumentCreateResult(
            created=False,
            dry_run=True,
            document_name=None,
            payload_preview=payload,
        )

    client = get_client(ctx)
    path = (
        f"/projects/{encode_path_segment(project_id)}"
        f"/spaces/{encode_path_segment(space_id)}/documents"
    )
    try:
        response = await client.post(path, json=cast(dict[str, object], payload))
    except PolarionAuthError as exc:
        raise PermissionError(
            "Cannot create document -- check your POLARION_TOKEN permissions."
        ) from exc
    except PolarionNotFoundError as exc:
        raise ValueError(
            f"Project '{project_id}' or space '{space_id}' not found. "
            "Use `list_projects` and `list_documents` to discover valid IDs."
        ) from exc
    except PolarionError as exc:
        raise RuntimeError(f"Failed to create document: {exc.message}") from exc

    new_name: str | None = None
    data = response.get("data")
    if isinstance(data, list) and data and isinstance(data[0], dict):
        _, extracted = split_module_id(safe_str(data[0].get("id", "")))
        new_name = extracted or None

    if new_name is None:
        raise RuntimeError(
            "Polarion accepted the create request but returned no document name. "
            "The document may or may not exist; verify with list_documents."
        )

    return DocumentCreateResult(
        created=True,
        dry_run=False,
        document_name=new_name,
        payload_preview=None,
    )
