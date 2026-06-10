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
    """Build the flat ``moveToDocument`` action body (not JSON:API).

    ``targetDocument`` plus at most one of ``previousPart`` / ``nextPart``
    (both omitted = append). At-most-one re-checked here to fail closed.
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
    project_id: str = Field(description="Polarion project ID."),
    work_item_id: str = Field(
        min_length=1,
        description="Work item ID (e.g. 'MCPT-042').",
    ),
    target_space_id: str = Field(
        min_length=1,
        description="Target space ID ('_default' for the default space).",
    ),
    target_document_name: str = Field(
        min_length=1,
        description="Target document name within ``target_space_id``.",
    ),
    previous_part_id: str | None = Field(
        default=None,
        description="Insert AFTER this part ID; exclusive with ``next_part_id``.",
    ),
    next_part_id: str | None = Field(
        default=None,
        description="Insert BEFORE this part ID; exclusive with ``previous_part_id``.",
    ),
    dry_run: bool = Field(
        default=False,
        description="Preview payload without calling Polarion.",
    ),
) -> WorkItemMoveResult:
    """Move an existing work item into a document at a given position.

    THE attach path: atomically sets ``module`` and inserts a part. Headings
    rejected (HTTP 400) — add headings via ``update_document`` ``<hN>``. An
    item already in a document is moved, not copied.

    At most one of ``previous_part_id`` (AFTER) / ``next_part_id`` (BEFORE);
    omit both to append. Part IDs from ``read_document_parts``.

    Auto-creates one link to the enclosing heading; a later same-role
    ``create_work_item_links`` returns 201 but is NOT persisted.
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
    project_id: str = Field(description="Polarion project ID."),
    work_item_id: str = Field(
        min_length=1,
        description="Work item ID (e.g. 'MCPT-042').",
    ),
    dry_run: bool = Field(
        default=False,
        description="Preview payload without calling Polarion.",
    ),
) -> WorkItemMoveResult:
    """Detach a work item from its document — the ONLY detach path.

    NOT idempotent: an already free-floating item returns HTTP 400 — first
    confirm it is in a document (``get_work_item``: non-empty ``space_id``)
    and skip the call when already detached. Item preserved, re-attachable
    via ``move_work_item_to_document``. Headings detachable too.
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
        # moveFromDocument takes no body.
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
