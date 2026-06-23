"""Pydantic models for MCP tool I/O, grouped by domain and re-exported here as
the single import surface. Class docstrings and ``Field(description=...)`` ship
in the JSON Schema — omit a description when name + type say everything.
"""

from __future__ import annotations

from mcp_server_polarion.models.comments import (
    Comment,
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
    "Comment",
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
