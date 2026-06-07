"""Work item document-membership tools — move into / out of documents."""

from __future__ import annotations

import logging
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
    WorkItemMoveResult,
)
from mcp_server_polarion.server import mcp
from mcp_server_polarion.tools._shared.helpers import (
    encode_path_segment,
    get_client,
)

logger = logging.getLogger("mcp_server_polarion.tools.moves")


def _build_move_to_document_payload(
    *,
    project_id: str,
    target_space_id: str,
    target_document_name: str,
    previous_part_id: str | None,
    next_part_id: str | None,
) -> dict[str, JsonValue]:
    """Build the request body for the ``moveToDocument`` action endpoint.

    NOT JSON:API — a flat object with ``targetDocument`` plus at most one of
    ``previousPart`` / ``nextPart`` (both omitted = append at end). The
    at-most-one invariant is re-checked here to fail closed for direct callers.
    """
    if previous_part_id is not None and next_part_id is not None:
        msg = (
            "_build_move_to_document_payload accepts at most one of "
            "previous_part_id or next_part_id; both being set is rejected "
            "by Polarion."
        )
        raise ValueError(msg)

    target_doc = f"{project_id}/{target_space_id}/{target_document_name}"
    payload: dict[str, JsonValue] = {"targetDocument": target_doc}
    if previous_part_id is not None:
        payload["previousPart"] = f"{target_doc}/{previous_part_id}"
    elif next_part_id is not None:
        payload["nextPart"] = f"{target_doc}/{next_part_id}"
    return payload


@mcp.tool(
    tags={"write"},
    timeout=60.0,
    annotations={
        # Non-idempotent: re-moving an already-moved item may 400, not no-op.
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
            "Insert AFTER this part ID; mutually exclusive with ``next_part_id``. "
            "Omit both to append at the end of the target document."
        ),
    ),
    next_part_id: str | None = Field(
        default=None,
        description=(
            "Insert BEFORE this part ID; mutually exclusive with ``previous_part_id``. "
            "Omit both to append at the end of the target document."
        ),
    ),
    dry_run: bool = Field(
        default=False,
        description="When True, return payload preview without calling Polarion.",
    ),
) -> WorkItemMoveResult:
    """Move a work item into a Polarion document at a specific position.

    Calls the ``moveToDocument`` action endpoint, which atomically updates the
    work item's ``module`` relationship, inserts a document part at the
    position, and assigns an ``outline_number``. This is the ONLY supported way
    to attach a work item body to a document — editing ``homePageContent`` to
    inject a macro reference leaves ``module`` unset, an inconsistent state.

    Heading-type work items are rejected (HTTP 400) — headings must be created
    inside their target document. If the item is already in another document
    this detaches it from the source (a move, not a copy); to detach back to
    free-floating use ``move_work_item_from_document`` (``module`` cannot be
    cleared via PATCH).

    Provide AT MOST one of ``previous_part_id`` (insert AFTER) / ``next_part_id``
    (insert BEFORE); omit both to append at the end. Discover part IDs with
    ``read_document_parts``.

    Side effect: the server auto-creates an outgoing link from the moved item to
    its enclosing heading. The role is project-configurable (commonly
    ``parent`` / ``relates_to``) — inspect forward links on a known-attached
    item to find it. It appears in ``list_work_item_links(direction="forward")``
    after the move, is silently removed by ``move_work_item_from_document``, and
    collides with any same-role link from the same source (see the "phantom
    success" note on ``create_work_item_links``).

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
        ValueError: Both positions supplied, heading work item, or
            work item / document / part not found.
        PermissionError: Token lacks permission.
        RuntimeError: Other Polarion API errors.
    """
    if previous_part_id is not None and next_part_id is not None:
        raise ValueError(
            "Provide at most one of previous_part_id or next_part_id. "
            "Use previous_part_id to insert AFTER an existing part, or "
            "next_part_id to insert BEFORE; omit both to append at the "
            "end of the target document. Discover existing part IDs with "
            "`read_document_parts`."
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
        # Non-idempotent: a second moveFromDocument 400s (already free-floating).
        "readOnlyHint": False,
        "destructiveHint": True,
        "idempotentHint": False,
        "openWorldHint": True,
    },
)
async def move_work_item_from_document(
    ctx: Context,
    project_id: str = Field(description="Project containing the work item."),
    work_item_id: str = Field(
        min_length=1,
        description="Short ID of an EXISTING work item (e.g. 'MCPT-042').",
    ),
    dry_run: bool = Field(
        default=False,
        description="When True, return payload preview without calling Polarion.",
    ),
) -> WorkItemMoveResult:
    """Detach a work item from its document, returning it to free-floating state.

    Inverse of ``move_work_item_to_document``. Calls the ``moveFromDocument``
    action endpoint, which clears the work item's ``module`` relationship and
    removes its document part — the ONLY supported detach path (Polarion rejects
    PATCH on ``module``). The work item itself is preserved (history, links,
    attributes) and reappears as free-floating, visible to ``list_work_items``;
    re-attach via ``move_work_item_to_document``.

    NOT idempotent: calling it on an already free-floating item returns HTTP 400
    (``RuntimeError``). Heading-type items CAN be detached — the heading becomes
    a free-floating work item with ``space_id=""`` / ``outline_number=""``
    (orphan-like but intact).

    Args:
        ctx: MCP tool context (injected automatically).
        project_id: Project containing the work item.
        work_item_id: Short ID of an existing work item.
        dry_run: When True, return payload preview only.

    Returns:
        WorkItemMoveResult with ``moved``, ``dry_run``, and
        ``payload_preview`` (an empty dict on dry-run; None on real
        execution). Polarion returns 204 on success — confirm with
        ``get_work_item`` showing ``space_id=""``.

    Raises:
        ValueError: Project or work item not found.
        PermissionError: Token lacks permission.
        RuntimeError: Other Polarion API errors, including the 400
            returned when the work item is already free-floating.
    """
    if dry_run:
        return WorkItemMoveResult(
            moved=False,
            dry_run=True,
            payload_preview={},
        )

    client = get_client(ctx)
    path = (
        f"/projects/{encode_path_segment(project_id)}"
        f"/workitems/{encode_path_segment(work_item_id)}"
        "/actions/moveFromDocument"
    )
    try:
        # moveFromDocument takes no body per the Polarion REST API spec.
        await client.post(path)
    except PolarionAuthError as exc:
        raise PermissionError(
            "Cannot detach work item -- check your POLARION_TOKEN permissions."
        ) from exc
    except PolarionNotFoundError as exc:
        raise ValueError(
            f"Work item '{work_item_id}' in project '{project_id}' not found. "
            "Use `list_work_items` to discover valid IDs."
        ) from exc
    except PolarionError as exc:
        raise RuntimeError(f"Failed to detach work item: {exc.message}") from exc

    return WorkItemMoveResult(
        moved=True,
        dry_run=False,
        payload_preview=None,
    )
