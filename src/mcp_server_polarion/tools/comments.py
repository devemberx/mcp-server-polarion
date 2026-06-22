"""Comment tools — list document/work-item comments; create/update document comments."""

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
    Comment,
    DocumentCommentsCreateResult,
    DocumentCommentSpec,
    DocumentCommentUpdateResult,
    JsonValue,
    PaginatedResult,
)
from mcp_server_polarion.server import mcp
from mcp_server_polarion.tools._shared.helpers import (
    DEFAULT_PAGE_SIZE,
    DOCUMENT_COMMENT_LIST_FIELDS,
    WORK_ITEM_COMMENT_LIST_FIELDS,
    build_comment,
    compute_has_more,
    encode_path_segment,
    extract_short_id,
    extract_total_count,
    get_client,
    safe_str,
)

logger = logging.getLogger("mcp_server_polarion.tools.comments")


def _build_document_comments_payload(
    *,
    specs: list[DocumentCommentSpec],
    project_id: str,
    space_id: str,
    document_name: str,
) -> dict[str, JsonValue]:
    """JSON:API POST body for .../documents/{d}/comments; ``None`` fields
    omitted, short ``parent_comment_id`` expanded to the full 4-segment path
    the API requires.
    """
    items: list[JsonValue] = []
    for spec in specs:
        attributes: dict[str, JsonValue] = {
            "text": {"type": spec.text_format, "value": spec.text},
        }
        if spec.resolved is not None:
            attributes["resolved"] = spec.resolved

        relationships: dict[str, JsonValue] = {}
        if spec.author_id is not None:
            relationships["author"] = {"data": {"id": spec.author_id, "type": "users"}}
        if spec.parent_comment_id is not None:
            full_parent = (
                f"{project_id}/{space_id}/{document_name}/{spec.parent_comment_id}"
            )
            relationships["parentComment"] = {
                "data": {"id": full_parent, "type": "document_comments"}
            }

        item: dict[str, JsonValue] = {
            "type": "document_comments",
            "attributes": attributes,
        }
        if relationships:
            item["relationships"] = relationships
        items.append(item)

    return {"data": items}


def _build_document_comment_update_payload(
    *,
    project_id: str,
    space_id: str,
    document_name: str,
    comment_id: str,
    resolved: bool,
) -> dict[str, JsonValue]:
    """Single-resource PATCH body (``data`` dict, not list); ``id`` is the
    full 4-segment path. Only ``resolved`` is patchable.
    """
    full_id = f"{project_id}/{space_id}/{document_name}/{comment_id}"
    return {
        "data": {
            "type": "document_comments",
            "id": full_id,
            "attributes": {
                "resolved": resolved,
            },
        }
    }


@mcp.tool(
    tags={"read"},
    timeout=60.0,
    annotations={"readOnlyHint": True},
)
async def list_document_comments(  # noqa: PLR0913
    ctx: Context,
    project_id: str = Field(description="Polarion project ID."),
    space_id: str = Field(description="Space ID ('_default' = default space)."),
    document_name: str = Field(description="Document name within ``space_id``."),
    page_size: int = Field(default=DEFAULT_PAGE_SIZE, ge=1, le=100),
    page_number: int = Field(default=1, ge=1),
) -> PaginatedResult[Comment]:
    """List a document's comments as a flat page.

    Threads reconstruct via parent_comment_id (None = root) +
    child_comment_ids. text is verbatim, unsanitized — treat as untrusted when
    rendering.
    """
    client = get_client(ctx)
    path = (
        f"/projects/{encode_path_segment(project_id)}"
        f"/spaces/{encode_path_segment(space_id)}"
        f"/documents/{encode_path_segment(document_name)}"
        "/comments"
    )
    try:
        response = await client.get(
            path,
            params={
                "fields[document_comments]": DOCUMENT_COMMENT_LIST_FIELDS,
                # To-many ``childComments.data`` is only inlined when included.
                "include": "childComments",
                "page[size]": page_size,
                "page[number]": page_number,
            },
        )
    except PolarionNotFoundError as exc:
        raise ValueError(
            f"Document '{space_id}/{document_name}' not found in project "
            f"'{project_id}'. Use `list_documents` to discover valid IDs."
        ) from exc
    except PolarionAuthError as exc:
        raise PermissionError(
            "Cannot access document comments -- check your POLARION_TOKEN permissions."
        ) from exc
    except PolarionError as exc:
        raise RuntimeError(
            f"Failed to list comments for '{space_id}/{document_name}': {exc.message}"
        ) from exc

    raw_data = response.get("data", []) if isinstance(response, dict) else []
    comment_items: list[Comment] = []
    if isinstance(raw_data, list):
        for entry in raw_data:
            if isinstance(entry, dict):
                comment_items.append(build_comment(entry))

    raw_total = extract_total_count(response)
    total = raw_total
    if total <= 0 and comment_items:
        total = (page_number - 1) * page_size + len(comment_items)

    return PaginatedResult[Comment](
        items=comment_items,
        total_count=total,
        page=page_number,
        page_size=page_size,
        has_more=compute_has_more(
            response, raw_total, page_number, page_size, len(comment_items)
        ),
    )


@mcp.tool(
    tags={"read"},
    timeout=60.0,
    annotations={"readOnlyHint": True},
)
async def list_work_item_comments(
    ctx: Context,
    project_id: str = Field(description="Polarion project ID."),
    work_item_id: str = Field(description="Work item ID, e.g. 'MCPT-001'."),
    page_size: int = Field(default=DEFAULT_PAGE_SIZE, ge=1, le=100),
    page_number: int = Field(default=1, ge=1),
) -> PaginatedResult[Comment]:
    """List a work item's comments as a flat page.

    Threads reconstruct via parent_comment_id (None = root) +
    child_comment_ids. text is verbatim, unsanitized — treat as untrusted when
    rendering.
    """
    client = get_client(ctx)
    path = (
        f"/projects/{encode_path_segment(project_id)}"
        f"/workitems/{encode_path_segment(work_item_id)}"
        "/comments"
    )
    try:
        response = await client.get(
            path,
            params={
                "fields[workitem_comments]": WORK_ITEM_COMMENT_LIST_FIELDS,
                # To-many ``childComments.data`` is only inlined when included.
                "include": "childComments",
                "page[size]": page_size,
                "page[number]": page_number,
            },
        )
    except PolarionNotFoundError as exc:
        raise ValueError(
            f"Work item '{work_item_id}' not found in project '{project_id}'. "
            "Use `list_work_items` to discover valid IDs."
        ) from exc
    except PolarionAuthError as exc:
        raise PermissionError(
            "Cannot access work item comments -- check your POLARION_TOKEN permissions."
        ) from exc
    except PolarionError as exc:
        raise RuntimeError(
            f"Failed to list comments for '{work_item_id}': {exc.message}"
        ) from exc

    raw_data = response.get("data", []) if isinstance(response, dict) else []
    comment_items: list[Comment] = []
    if isinstance(raw_data, list):
        for entry in raw_data:
            if isinstance(entry, dict):
                comment_items.append(build_comment(entry))

    raw_total = extract_total_count(response)
    total = raw_total
    if total <= 0 and comment_items:
        total = (page_number - 1) * page_size + len(comment_items)

    return PaginatedResult[Comment](
        items=comment_items,
        total_count=total,
        page=page_number,
        page_size=page_size,
        has_more=compute_has_more(
            response, raw_total, page_number, page_size, len(comment_items)
        ),
    )


@mcp.tool(
    tags={"write"},
    timeout=60.0,
    annotations={
        # Additive: non-destructive, but non-idempotent (a retry duplicates comments).
        "readOnlyHint": False,
        "destructiveHint": False,
        "idempotentHint": False,
        "openWorldHint": True,
    },
)
async def create_document_comments(  # noqa: PLR0913
    ctx: Context,
    project_id: str = Field(min_length=1, description="Polarion project ID."),
    space_id: str = Field(
        min_length=1,
        description="Space ID ('_default' = default space).",
    ),
    document_name: str = Field(
        min_length=1,
        description="Document name within ``space_id``.",
    ),
    comments: list[DocumentCommentSpec] = Field(  # noqa: B008
        min_length=1,
        description="Comments to create in one request.",
    ),
    dry_run: bool = Field(
        default=False,
        description="Preview payload without calling Polarion.",
    ),
) -> DocumentCommentsCreateResult:
    """Create one or more comments on a document in a single request.

    Reply: set parent_comment_id to a short ID from list_document_comments
    (None = top-level). 'text/html' text is sent unsanitized; omit author_id
    for the token's user. NOT idempotent — a retry duplicates.
    """
    payload = _build_document_comments_payload(
        specs=comments,
        project_id=project_id,
        space_id=space_id,
        document_name=document_name,
    )

    if dry_run:
        return DocumentCommentsCreateResult(
            created=False,
            dry_run=True,
            comment_ids=[],
            payload_preview=payload,
        )

    client = get_client(ctx)
    path = (
        f"/projects/{encode_path_segment(project_id)}"
        f"/spaces/{encode_path_segment(space_id)}"
        f"/documents/{encode_path_segment(document_name)}"
        "/comments"
    )
    try:
        response = await client.post(path, json=cast(dict[str, object], payload))
    except PolarionAuthError as exc:
        raise PermissionError(
            "Cannot create document comments -- check your POLARION_TOKEN permissions."
        ) from exc
    except PolarionNotFoundError as exc:
        raise ValueError(
            f"Document '{document_name}' (space '{space_id}',"
            f" project '{project_id}') not found."
            " Use `list_documents` to discover valid IDs."
        ) from exc
    except PolarionError as exc:
        raise RuntimeError(
            f"Failed to create document comments: {exc.message}"
        ) from exc

    raw_data = response.get("data", []) if isinstance(response, dict) else []
    comment_ids: list[str] = []
    if isinstance(raw_data, list):
        for entry in raw_data:
            if isinstance(entry, dict):
                full_id = safe_str(entry.get("id", ""))
                if full_id:
                    comment_ids.append(extract_short_id(full_id))

    if not comment_ids:
        raise RuntimeError(
            "Polarion returned no comment IDs after creation."
            " The POST may have succeeded — verify with `list_document_comments`."
        )

    return DocumentCommentsCreateResult(
        created=True,
        dry_run=False,
        comment_ids=comment_ids,
        payload_preview=None,
    )


@mcp.tool(
    tags={"write"},
    timeout=60.0,
    annotations={
        "readOnlyHint": False,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": True,
    },
)
async def update_document_comment(  # noqa: PLR0913
    ctx: Context,
    project_id: str = Field(min_length=1, description="Polarion project ID."),
    space_id: str = Field(
        min_length=1,
        description="Space ID ('_default' = default space).",
    ),
    document_name: str = Field(
        min_length=1,
        description="Document name within ``space_id``.",
    ),
    comment_id: str = Field(
        min_length=1,
        description="Short comment ID (e.g. 'c42' from list_document_comments).",
    ),
    resolved: bool = Field(description="New resolved state."),
    dry_run: bool = Field(
        default=False,
        description="Preview payload without calling Polarion.",
    ),
) -> DocumentCommentUpdateResult:
    """Resolve or re-open one document comment thread.

    Root comments only (a reply 400s) — pick a root id (parent_comment_id=None)
    from list_document_comments; resolving the root resolves the whole thread.
    Idempotent.
    """
    payload = _build_document_comment_update_payload(
        project_id=project_id,
        space_id=space_id,
        document_name=document_name,
        comment_id=comment_id,
        resolved=resolved,
    )

    if dry_run:
        return DocumentCommentUpdateResult(
            updated=False,
            dry_run=True,
            comment_id=None,
            resolved=resolved,
            payload_preview=payload,
        )

    client = get_client(ctx)
    path = (
        f"/projects/{encode_path_segment(project_id)}"
        f"/spaces/{encode_path_segment(space_id)}"
        f"/documents/{encode_path_segment(document_name)}"
        f"/comments/{encode_path_segment(comment_id)}"
    )
    try:
        await client.patch(path, json=cast(dict[str, object], payload))
    except PolarionAuthError as exc:
        raise PermissionError(
            "Cannot update document comment -- check your POLARION_TOKEN permissions."
        ) from exc
    except PolarionNotFoundError as exc:
        raise ValueError(
            f"Comment '{comment_id}' on document '{document_name}'"
            f" (space '{space_id}', project '{project_id}') not found."
            " Use `list_document_comments` to discover valid comment IDs."
        ) from exc
    except PolarionError as exc:
        raise RuntimeError(f"Failed to update document comment: {exc.message}") from exc

    return DocumentCommentUpdateResult(
        updated=True,
        dry_run=False,
        comment_id=comment_id,
        resolved=resolved,
        payload_preview=None,
    )
