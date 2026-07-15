"""Pydantic models for MCP tool I/O, grouped by domain, re-exported here as
single import surface. Class docstrings + ``Field(description=...)`` ship in
JSON Schema — omit description when name + type say everything.
"""

from __future__ import annotations

from mcp_server_polarion.models.comments import (
    Comment,
    CommentsCreateResult,
    CommentSpec,
    CommentUpdateResult,
    WorkItemCommentSpec,
)
from mcp_server_polarion.models.common import (
    MAX_BODY_HTML_LEN,
    JsonValue,
    PaginatedResult,
)
from mcp_server_polarion.models.documents import (
    DocumentCopyResult,
    DocumentCreateResult,
    DocumentDetail,
    DocumentPart,
    DocumentReadResult,
    DocumentSummary,
    DocumentUpdateResult,
)
from mcp_server_polarion.models.enum import EnumOption
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
from mcp_server_polarion.models.recipes import (
    HtmlRecipeGallery,
    SqlRecipeGallery,
)
from mcp_server_polarion.models.test_runs import (
    TestRecordCreateSpec,
    TestRecordsCreateResult,
    TestRecordSummary,
    TestRecordsUpdateResult,
    TestRecordUpdateSpec,
    TestRunCreateSpec,
    TestRunDetail,
    TestRunsCreateResult,
    TestRunSummary,
    TestRunsUpdateResult,
    TestRunUpdateSpec,
)
from mcp_server_polarion.models.work_items import (
    Hyperlink,
    WorkItemCreateSpec,
    WorkItemDetail,
    WorkItemMoveResult,
    WorkItemRead,
    WorkItemsCreateResult,
    WorkItemSummary,
    WorkItemsUpdateResult,
    WorkItemUpdateSpec,
)

__all__: list[str] = [
    "MAX_BODY_HTML_LEN",
    "Comment",
    "CommentSpec",
    "CommentUpdateResult",
    "CommentsCreateResult",
    "DocumentCopyResult",
    "DocumentCreateResult",
    "DocumentDetail",
    "DocumentPart",
    "DocumentReadResult",
    "DocumentSummary",
    "DocumentUpdateResult",
    "EnumOption",
    "HtmlRecipeGallery",
    "Hyperlink",
    "JsonValue",
    "PaginatedResult",
    "ProjectSummary",
    "SqlRecipeGallery",
    "TestRecordCreateSpec",
    "TestRecordSummary",
    "TestRecordUpdateSpec",
    "TestRecordsCreateResult",
    "TestRecordsUpdateResult",
    "TestRunCreateSpec",
    "TestRunDetail",
    "TestRunSummary",
    "TestRunUpdateSpec",
    "TestRunsCreateResult",
    "TestRunsUpdateResult",
    "WorkItemCommentSpec",
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
    "WorkItemUpdateSpec",
    "WorkItemsCreateResult",
    "WorkItemsUpdateResult",
]
