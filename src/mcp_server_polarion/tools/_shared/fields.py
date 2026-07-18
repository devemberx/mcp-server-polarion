"""Sparse-fieldset and bulk-write constants shared across tool modules."""

from __future__ import annotations

from typing import Final

# Bulk-write cap: Polarion throttles ~3 req/s, no concurrency.
MAX_BULK_ITEMS: Final[int] = 50

# Detail fetches need ``@all`` — explicit field lists drop inline customs.
WORK_ITEM_LIST_FIELDS: Final[str] = "title,type,status,priority,updated,module,author"
WORK_ITEM_DETAIL_FIELDS: Final[str] = "@all"
WORK_ITEM_PART_FIELDS: Final[str] = "title,type,status,description,outlineNumber"
# camelCase finishedOn/isTemplate/groupId = Polarion attr names; author +
# template keep relationship blocks alive under sparse fieldset.
TEST_RUN_LIST_FIELDS: Final[str] = (
    "title,type,status,finishedOn,updated,author,isTemplate,groupId,template"
)
TEST_RUN_DETAIL_FIELDS: Final[str] = "@all"
# testCase/executedBy/defect keep relationship blocks alive under sparse
# fieldset.
TEST_RECORD_LIST_FIELDS: Final[str] = (
    "executed,duration,result,iteration,testCase,executedBy,defect"
)
# Single-record GET: comment/testCaseRevision added to list fields; relationship
# names still listed explicit (not @all) to survive sparse fieldset.
TEST_RECORD_DETAIL_FIELDS: Final[str] = (
    "executed,duration,result,iteration,comment,testCaseRevision,testCase,"
    "executedBy,defect"
)
DOCUMENT_DETAIL_FIELDS: Final[str] = "@all"
# Sparse fieldset filters relationships too — name them explicitly.
DOCUMENT_ATTACHMENT_LIST_FIELDS: Final[str] = "id,fileName,title,updated,length,author"
DOCUMENT_COMMENT_LIST_FIELDS: Final[str] = (
    "created,resolved,text,author,parentComment,childComments"
)
# Work item comments add ``title``; document comments have none.
WORK_ITEM_COMMENT_LIST_FIELDS: Final[str] = (
    "created,resolved,title,text,author,parentComment,childComments"
)

__all__: list[str] = [
    "DOCUMENT_ATTACHMENT_LIST_FIELDS",
    "DOCUMENT_COMMENT_LIST_FIELDS",
    "DOCUMENT_DETAIL_FIELDS",
    "MAX_BULK_ITEMS",
    "TEST_RECORD_DETAIL_FIELDS",
    "TEST_RECORD_LIST_FIELDS",
    "TEST_RUN_DETAIL_FIELDS",
    "TEST_RUN_LIST_FIELDS",
    "WORK_ITEM_COMMENT_LIST_FIELDS",
    "WORK_ITEM_DETAIL_FIELDS",
    "WORK_ITEM_LIST_FIELDS",
    "WORK_ITEM_PART_FIELDS",
]
