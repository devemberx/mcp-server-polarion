"""Write MCP tools for Polarion ALM.

Currently provides ``create_work_item``.  All write tools follow the
strict patterns documented in ``CLAUDE.md``: they convert Markdown
input to sanitized HTML, build minimal JSON:API payloads (skipping
unset fields rather than sending empty values), and map domain
exceptions to user-facing ones at the tool layer.
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
            "Free-form; Polarion validates server-side."
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
    document — for document-bound work items, use a future
    ``create_document_part`` tool that uses the ``/parts`` endpoint and
    supports outline positioning.

    Description handling: ``description`` is treated as Markdown,
    converted via ``markdown_to_html`` (CommonMark + GFM tables), and
    sanitized via ``sanitize_html`` before being stored as
    ``{"type": "text/html", "value": "..."}``. Dangerous link schemes
    such as ``javascript:`` are stripped automatically.

    Free-form fields (``status``, ``priority``, ``severity``) are
    validated by Polarion server-side. If you're unsure of the allowed
    values for the project, inspect an existing work item with
    ``get_work_item`` and reuse its values.

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
