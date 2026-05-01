"""Write MCP tools for Polarion ALM.

Currently provides ``create_work_item`` and ``move_work_item_to_document``.
All write tools follow the strict patterns documented in ``CLAUDE.md``:
they convert Markdown input to sanitized HTML, build minimal request
payloads (skipping unset fields rather than sending empty values), and
map domain exceptions to user-facing ones at the tool layer.
"""

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
    Hyperlink,
    JsonValue,
    WorkItemCreateResult,
    WorkItemMoveResult,
)
from mcp_server_polarion.server import mcp
from mcp_server_polarion.tools._helpers import (
    encode_path_segment,
    extract_short_id,
    get_client,
    safe_str,
)
from mcp_server_polarion.utils import markdown_to_html, sanitize_html

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
) -> dict[str, JsonValue]:
    """Build the JSON:API request body for ``POST /projects/{p}/workitems``.

    Only attaches keys for values that are explicitly set — ``None``,
    empty strings, and empty lists are skipped so we never overwrite
    Polarion defaults with empty values on creation.
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


# ---------------------------------------------------------------------------
# Write tools
# ---------------------------------------------------------------------------


@mcp.tool(tags={"write"}, timeout=60.0)
async def create_work_item(  # noqa: PLR0913
    ctx: Context,
    project_id: str = Field(
        description=(
            "Polarion project ID (e.g. 'myproject'). "
            "Use ``list_projects`` to discover valid IDs."
        ),
    ),
    title: str = Field(
        min_length=1,
        description="Work item title (required, non-empty).",
    ),
    type: str = Field(
        min_length=1,
        description=(
            "Work item type, e.g. 'requirement', 'task', 'testCase', "
            "'defect'. Project-specific; refer to existing work items "
            "via ``get_work_item`` if unsure of allowed values."
        ),
    ),
    description: str | None = Field(
        default=None,
        description=(
            "Markdown body. Converted to Polarion-safe HTML on write. "
            "Pass None (or omit) to leave the description unset."
        ),
    ),
    status: str | None = Field(
        default=None,
        description=(
            "Initial workflow status (e.g. 'draft', 'open'). "
            "Server default applies when omitted. Project-specific."
        ),
    ),
    priority: str | None = Field(
        default=None,
        description=(
            "Priority value as a string (e.g. '50.0'). "
            "WARNING: this Polarion server version silently coerces "
            "unrecognised values to the project default. Inspect an "
            "existing work item with ``get_work_item`` to discover the "
            "project's actual priority values before relying on this."
        ),
    ),
    severity: str | None = Field(
        default=None,
        description=(
            "Severity classification, primarily for defects "
            "(e.g. 'major', 'critical'). Free-form."
        ),
    ),
    assignee_ids: list[str] | None = Field(  # noqa: B008
        default=None,
        description=(
            "Short user IDs (e.g. ['alice', 'bob']). "
            "Each is wrapped as {type:'users', id:'<id>'} in the "
            "assignee to-many relationship."
        ),
    ),
    due_date: str | None = Field(
        default=None,
        description="Due date in ISO-8601 format 'YYYY-MM-DD'.",
    ),
    initial_estimate: str | None = Field(
        default=None,
        description=("Polarion duration string, e.g. '5 1/2d', '1w 2d', '4h'."),
    ),
    hyperlinks: list[Hyperlink] | None = Field(  # noqa: B008
        default=None,
        description=(
            "External hyperlinks attached to the work item. "
            "Each must specify ``role`` and ``uri``; ``title`` is optional."
        ),
    ),
    dry_run: bool = Field(
        default=False,
        description=(
            "When True, build and return the JSON:API payload preview "
            "without calling Polarion. Useful for verifying the request "
            "shape before committing."
        ),
    ),
) -> WorkItemCreateResult:
    """Create a new Polarion work item in a project.

    Builds a JSON:API ``POST /projects/{projectId}/workitems`` request
    from the supplied fields, optionally previewing the payload with
    ``dry_run=True``.  The created work item is *not* attached to any
    document — to place it inside a document at a specific outline
    position, follow up with ``move_work_item_to_document``.

    Description handling: ``description`` is treated as Markdown,
    converted via ``markdown_to_html`` (CommonMark + GFM tables), and
    sanitized via ``sanitize_html`` before being stored as
    ``{"type": "text/html", "value": "..."}``. Dangerous link schemes
    such as ``javascript:`` are stripped automatically.

    Free-form fields (``type``, ``status``, ``priority``, ``severity``)
    are NOT strictly validated by this Polarion server version.
    Unrecognised ``priority`` values are silently coerced to the project
    default; arbitrary ``type`` strings are stored verbatim (which can
    create "ghost" work-item types that won't appear in any project
    schema). Always inspect an existing work item with ``get_work_item``
    and reuse its values to avoid corrupting the project's enum space.

    Args:
        ctx: MCP tool context (injected automatically).
        project_id: Polarion project ID.
        title: Work item title (required, non-empty).
        type: Work item type (e.g. 'requirement', 'task').
        description: Optional Markdown body.
        status: Optional initial workflow status.
        priority: Optional priority string.
        severity: Optional severity classification.
        assignee_ids: Optional list of short user IDs to assign.
        due_date: Optional ISO-8601 date 'YYYY-MM-DD'.
        initial_estimate: Optional Polarion duration string.
        hyperlinks: Optional list of ``Hyperlink`` objects.
        dry_run: When True, return payload preview without calling
            Polarion. Defaults to False.

    Returns:
        WorkItemCreateResult with:
        - ``created``: True on a successful real create, False when
          ``dry_run=True``.
        - ``dry_run``: Echo of the dry_run flag.
        - ``work_item_id``: Short ID of the created work item
          (e.g. 'MCPT-042'). None on dry-run.
        - ``payload_preview``: The JSON:API request body. Populated on
          dry-run; None after a successful real create.

    Raises:
        ValueError: If the project ID is not found.
        PermissionError: If the token lacks permissions to create work
            items in the project.
        RuntimeError: On other Polarion API errors, or if Polarion
            accepts the request but returns no work-item ID.
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


@mcp.tool(tags={"write"}, timeout=60.0)
async def move_work_item_to_document(  # noqa: PLR0913
    ctx: Context,
    project_id: str = Field(
        description=(
            "Polarion project ID containing the work item being moved. "
            "Use ``list_projects`` to discover valid IDs."
        ),
    ),
    work_item_id: str = Field(
        min_length=1,
        description=(
            "Short ID of an EXISTING work item to move into the target "
            "document (e.g. 'MCPT-042'). Use ``create_work_item`` first "
            "if the work item does not yet exist."
        ),
    ),
    target_space_id: str = Field(
        min_length=1,
        description=(
            "Space ID of the target document. Use '_default' for the "
            "default space. Discover with ``list_documents``."
        ),
    ),
    target_document_name: str = Field(
        min_length=1,
        description=("Name of the target document within ``target_space_id``."),
    ),
    previous_part_id: str | None = Field(
        default=None,
        description=(
            "Short part ID (e.g. 'workitem_MCPT-001', 'heading_MCPT-005') "
            "to insert the work item AFTER. Discover existing part IDs "
            "with ``get_document_parts``. Mutually exclusive with "
            "``next_part_id``; exactly one must be provided."
        ),
    ),
    next_part_id: str | None = Field(
        default=None,
        description=(
            "Short part ID to insert the work item BEFORE. Discover "
            "existing part IDs with ``get_document_parts``. Mutually "
            "exclusive with ``previous_part_id``; exactly one must be "
            "provided."
        ),
    ),
    dry_run: bool = Field(
        default=False,
        description=(
            "When True, build and return the request payload preview "
            "without calling Polarion."
        ),
    ),
) -> WorkItemMoveResult:
    """Move a work item into a Polarion document at a specific outline position.

    Calls Polarion's ``moveToDocument`` action endpoint
    (``POST /projects/{p}/workitems/{wi}/actions/moveToDocument``) which:

    - Updates the work item's ``module`` relationship to point at the
      target document. After the move, the work item is a NATIVE member
      of the document (its ``space_id`` / ``document_name`` will reflect
      the target), not an external reference.
    - Inserts a corresponding document part at the position specified
      by ``previous_part_id`` or ``next_part_id`` and assigns the work
      item a proper ``outline_number``.
    - Works for all NON-heading work-item types.

    LIMITATION: this Polarion server version rejects heading-type work
    items with HTTP 400 ("Cannot move headings"). Headings appear to be
    locked into the document that originally created them and cannot be
    relocated via the API. If you need a heading inside a particular
    document, the heading must be created inside that document at
    work-item creation time (a future ``module_id`` parameter on
    ``create_work_item`` is the planned path).

    Prerequisite: the work item must already exist. Use
    ``create_work_item`` first to create a free-floating work item.
    Note that if the work item is already attached to a different
    document, this tool detaches it from the source — the operation is
    a true move, not a copy.

    Position is specified by exactly ONE of ``previous_part_id``
    (insert AFTER that part) or ``next_part_id`` (insert BEFORE that
    part). Discover existing part IDs by calling ``get_document_parts``
    on the target document; pass the short ID
    (e.g. 'workitem_MCPT-001', 'heading_MCPT-005', 'polarion_1')
    unchanged.

    Args:
        ctx: MCP tool context (injected automatically).
        project_id: Project containing the work item to move.
        work_item_id: Short ID of an existing work item.
        target_space_id: Space ID of the target document.
        target_document_name: Name of the target document.
        previous_part_id: Insert AFTER this part. Mutually exclusive
            with ``next_part_id``.
        next_part_id: Insert BEFORE this part. Mutually exclusive with
            ``previous_part_id``.
        dry_run: When True, return payload preview without calling
            Polarion. Defaults to False.

    Returns:
        WorkItemMoveResult with:
        - ``moved``: True on a successful real move, False when
          ``dry_run=True``.
        - ``dry_run``: Echo of the dry_run flag.
        - ``payload_preview``: The request body. Populated on dry-run;
          None after a successful real move. The Polarion server
          returns 204 No Content on success, so no part ID is
          included; call ``get_document_parts`` on the target document
          if you need the new part's ID.

    Raises:
        ValueError: If neither or both of ``previous_part_id`` and
            ``next_part_id`` are provided, or if the work item, target
            document, or referenced part is not found.
        PermissionError: If the token lacks permissions to modify the
            work item or the target document.
        RuntimeError: On other Polarion API errors.
    """
    if (previous_part_id is None) == (next_part_id is None):
        raise ValueError(
            "Exactly one of previous_part_id or next_part_id must be "
            "provided. Use previous_part_id to insert AFTER an existing "
            "part, or next_part_id to insert BEFORE an existing part. "
            "Discover existing part IDs with `get_document_parts`."
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
            "`get_document_parts`."
        ) from exc
    except PolarionError as exc:
        raise RuntimeError(f"Failed to move work item: {exc.message}") from exc

    return WorkItemMoveResult(
        moved=True,
        dry_run=False,
        payload_preview=None,
    )
