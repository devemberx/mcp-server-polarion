"""Pydantic models for MCP tool inputs and outputs.

Every tool accepts and returns Pydantic models — never raw ``dict``.
Class docstrings and ``Field(description=...)`` ship in the JSON Schema, so
keep them tight: omit a description when the field name + type say everything
(e.g. ``items``, ``page``, ``id``), and keep one only for non-obvious semantics
(units, empty-conditions, round-trip / read-only contracts).

Models are grouped by domain into submodules (``common``, ``projects``,
``documents``, ``work_items``, ``links``, ``comments``) and re-exported here so
``from mcp_server_polarion.models import X`` stays the single import surface.
"""

from __future__ import annotations

from mcp_server_polarion.models.comments import (
    DocumentComment,
    DocumentCommentsCreateResult,
    DocumentCommentSpec,
    DocumentCommentUpdateResult,
)
from mcp_server_polarion.models.common import (
    MAX_BODY_HTML_LEN,
    EnumOption,
    JsonValue,
    PaginatedResult,
)
from mcp_server_polarion.models.documents import (
    DocumentCreateResult,
    DocumentDetail,
    DocumentPart,
    DocumentReadResult,
    DocumentSummary,
    DocumentUpdateResult,
)
from mcp_server_polarion.models.links import (
    WorkItemLink,
    WorkItemLinkRef,
    WorkItemLinksCreateResult,
    WorkItemLinksDeleteResult,
    WorkItemLinkSpec,
    WorkItemLinkUpdateResult,
    WorkItemLinkUpdateSpec,
)
from mcp_server_polarion.models.projects import ProjectSummary
from mcp_server_polarion.models.work_items import (
    Hyperlink,
    SqlRecipeGallery,
    WorkItemCreateSpec,
    WorkItemDetail,
    WorkItemMoveResult,
    WorkItemRead,
    WorkItemsCreateResult,
    WorkItemSummary,
    WorkItemUpdateResult,
)

__all__: list[str] = [
    "MAX_BODY_HTML_LEN",
    "DocumentComment",
    "DocumentCommentSpec",
    "DocumentCommentUpdateResult",
    "DocumentCommentsCreateResult",
    "DocumentCreateResult",
    "DocumentDetail",
    "DocumentPart",
    "DocumentReadResult",
    "DocumentSummary",
    "DocumentUpdateResult",
    "EnumOption",
    "Hyperlink",
    "JsonValue",
    "PaginatedResult",
    "ProjectSummary",
    "SqlRecipeGallery",
    "WorkItemCreateSpec",
    "WorkItemDetail",
    "WorkItemLink",
    "WorkItemLinkRef",
    "WorkItemLinkSpec",
    "WorkItemLinkUpdateResult",
    "WorkItemLinkUpdateSpec",
    "WorkItemLinksCreateResult",
    "WorkItemLinksDeleteResult",
    "WorkItemMoveResult",
    "WorkItemRead",
    "WorkItemSummary",
    "WorkItemUpdateResult",
    "WorkItemsCreateResult",
]
