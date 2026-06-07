"""Document comment tools — list, create, and update comments."""

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
    DocumentComment,
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
    build_document_comment,
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
    """Build the JSON:API request body for POST .../documents/{d}/comments.

    Builds one resource object per spec. Only attaches ``resolved`` when
    explicitly set (None = omit). ``author`` and ``parentComment``
    relationships are omitted when None. ``parent_comment_id`` is
    composed into the full path form ``proj/space/doc/commentId``
    required by the Polarion API, so callers pass the short ID from
    ``list_document_comments``.
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
    """Build the JSON:API PATCH body for one document comment.

    Produces a single-resource ``{"data": {...}}`` object (NOT a list — that
    is the POST/create shape). The ``id`` field is the full 4-segment path
    ``{project_id}/{space_id}/{document_name}/{comment_id}`` required by the
    Polarion PATCH endpoint. Only ``resolved`` is patchable per the API.
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
    space_id: str = Field(
        description="Space ID containing the document (e.g. '_default')."
    ),
    document_name: str = Field(description="Document name within the space."),
    page_size: int = Field(default=DEFAULT_PAGE_SIZE, ge=1, le=100),
    page_number: int = Field(default=1, ge=1),
) -> PaginatedResult[DocumentComment]:
    """List comments attached to a Polarion document.

    Comments come back as a flat page; reconstruct threads client-side via
    ``parent_comment_id`` (set on replies) and ``child_comment_ids`` (top-level
    comments have ``parent_comment_id`` of ``None``). ``text`` is verbatim, with
    ``text_format`` ``'text/html'`` or ``'text/plain'``; HTML is NOT sanitized
    (round-trips losslessly) — treat as untrusted input if rendering.

    Args:
        ctx: MCP tool context (injected automatically).
        project_id: Polarion project ID.
        space_id: Space ID (use '_default' for the default space).
        document_name: Document name within the space.
        page_size: Items per page (1-100, default 100).
        page_number: 1-based page number (default 1).

    Returns:
        PaginatedResult of ``DocumentComment`` items with ``id``,
        ``created``, ``resolved``, ``text``, ``text_format``, ``author_id``,
        ``parent_comment_id``, and ``child_comment_ids``.

    Raises:
        ValueError: Project, space, or document not found.
        PermissionError: Token lacks permission.
        RuntimeError: Other Polarion API errors.
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
    comment_items: list[DocumentComment] = []
    if isinstance(raw_data, list):
        for entry in raw_data:
            if isinstance(entry, dict):
                comment_items.append(build_document_comment(entry))

    raw_total = extract_total_count(response)
    total = raw_total
    if total <= 0 and comment_items:
        total = (page_number - 1) * page_size + len(comment_items)

    return PaginatedResult[DocumentComment](
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
        description="Space ID (use '_default' for the default space).",
    ),
    document_name: str = Field(
        min_length=1,
        description="Document name within ``space_id``.",
    ),
    comments: list[DocumentCommentSpec] = Field(  # noqa: B008
        min_length=1,
        description="One or more comments to create in a single request.",
    ),
    dry_run: bool = Field(
        default=False,
        description="When True, return payload preview without calling Polarion.",
    ),
) -> DocumentCommentsCreateResult:
    """Create one or more comments on a Polarion document in a single request.

    All ``comments`` post together; Polarion returns 201 with the new IDs.

    Thread model: ``parent_comment_id=None`` is a top-level comment; to reply,
    set it to the short ID from ``list_document_comments`` (e.g. ``'c42'``) and
    the tool composes the required 4-segment path ``proj/space/doc/c42``.
    ``text_format`` ``'text/plain'`` (default) stores verbatim; ``'text/html'``
    is sent as-is (no sanitization). Omit ``resolved`` to default to False, or
    pass True for a pre-resolved comment; omit ``author_id`` to use the token's
    user. NOT idempotent — retrying creates duplicates.

    Args:
        ctx: MCP tool context (injected automatically).
        project_id: Polarion project ID.
        space_id: Space ID (use '_default' for the default space).
        document_name: Document name within ``space_id``.
        comments: One or more ``DocumentCommentSpec`` objects, each with
            ``text``, ``text_format``, ``resolved``, ``author_id``, and
            ``parent_comment_id``.
        dry_run: When True, build and return the payload preview without
            calling Polarion.

    Returns:
        ``DocumentCommentsCreateResult`` with:

        - ``created`` — True on success; False on dry-run.
        - ``dry_run`` — mirrors the input flag.
        - ``comment_ids`` — short IDs (last path segment) of the created
          comments in Polarion's return order; empty on dry-run.
        - ``payload_preview`` — JSON:API body sent (or that would be
          sent); populated on dry-run, None after a real operation.

    Raises:
        ValueError: Project, space, or document not found.
        PermissionError: Token lacks permission to create comments.
        RuntimeError: Other Polarion API errors.
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
        description="Space ID (use '_default' for the default space).",
    ),
    document_name: str = Field(
        min_length=1,
        description="Document name within ``space_id``.",
    ),
    comment_id: str = Field(
        min_length=1,
        description=(
            "Short comment ID to update (e.g. 'c42' from ``list_document_comments``)."
        ),
    ),
    resolved: bool = Field(description="New resolved state for the comment."),
    dry_run: bool = Field(
        default=False,
        description="When True, return payload preview without calling Polarion.",
    ),
) -> DocumentCommentUpdateResult:
    """Resolve or re-open a single document comment.

    PATCHes ``{"resolved": <bool>}`` — the only patchable attribute. Use
    ``list_document_comments`` to find the short comment ID (last segment of the
    4-part path).

    Root comments only: Polarion accepts this PATCH only on top-level comments
    (``parent_comment_id is None``); on a reply it returns HTTP 400 ("Resolved
    field can be set only for root comments") → ``RuntimeError``, so filter
    first. Resolving the root marks the whole thread resolved. Idempotent.

    Args:
        ctx: MCP tool context (injected automatically).
        project_id: Polarion project ID.
        space_id: Space ID (use '_default' for the default space).
        document_name: Document name within ``space_id``.
        comment_id: Short comment ID (e.g. 'c42') from
            ``list_document_comments``.
        resolved: ``True`` to mark resolved; ``False`` to re-open.
        dry_run: When True, build and return the payload without
            calling Polarion.

    Returns:
        ``DocumentCommentUpdateResult`` with:

        - ``updated`` — True on success; False on dry-run.
        - ``dry_run`` — mirrors the input flag.
        - ``comment_id`` — the short ID patched; None on dry-run.
        - ``resolved`` — the value sent (or that would be sent).
        - ``payload_preview`` — JSON:API body; populated on dry-run,
          None after a real operation.

    Raises:
        ValueError: Project, space, document, or comment not found.
        PermissionError: Token lacks permission to update the comment.
        RuntimeError: Other Polarion API errors.
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
