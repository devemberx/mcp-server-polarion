"""JSON:API response -> Pydantic model parsers, plus the relationship/id
extractors they build on. Read side only; description/text values pass through
as raw HTML so they round-trip unchanged.
"""

from __future__ import annotations

from typing import Literal, TypedDict

from mcp_server_polarion.models import (
    Comment,
    EnumOption,
    Hyperlink,
    PaginatedResult,
    TestRunSummary,
    WorkItemDetail,
    WorkItemLink,
    WorkItemSummary,
)
from mcp_server_polarion.tools._shared.custom_fields import (
    STANDARD_WORK_ITEM_ATTRIBUTES,
    extract_custom_fields,
)
from mcp_server_polarion.tools._shared.helpers import safe_str
from mcp_server_polarion.tools._shared.pagination import make_page


class WorkItemSummaryKwargs(TypedDict):
    """Kwargs shape produced by ``parse_work_item_summary_kwargs``."""

    id: str
    title: str
    type: str
    status: str
    priority: str
    updated: str
    space_id: str
    document_name: str
    assignee_ids: list[str]


def extract_relationship_id(
    relationships: dict[str, object],
    rel_name: str,
) -> str:
    """Return a named relationship's ``data.id``, or ``""`` if absent."""
    rel = relationships.get(rel_name, {})
    if isinstance(rel, dict):
        inner = rel.get("data")
        if isinstance(inner, dict):
            return safe_str(inner.get("id", ""))
    return ""


def extract_relationship_ids(
    relationships: dict[str, object],
    rel_name: str,
) -> list[str]:
    """Return a to-many relationship's ``data[].id`` list (declaration order)."""
    rel = relationships.get(rel_name, {})
    if not isinstance(rel, dict):
        return []
    data = rel.get("data")
    if not isinstance(data, list):
        return []
    ids: list[str] = []
    for entry in data:
        if isinstance(entry, dict):
            entry_id = safe_str(entry.get("id", ""))
            if entry_id:
                ids.append(entry_id)
    return ids


def split_module_id(module_full_id: str) -> tuple[str, str]:
    """Split ``{proj}/{space}/{doc}`` into (space_id, document_name); ``doc``
    may contain ``/``. ``("", "")`` if under three segments.
    """
    if not module_full_id:
        return ("", "")
    parts = module_full_id.split("/", 2)
    expected_segments = 3
    if len(parts) < expected_segments:
        return ("", "")
    return (parts[1], parts[2])


def extract_short_id(full_id: str) -> str:
    """Strip the path prefix from a JSON:API id (``"p/MCPT-001"`` → ``"MCPT-001"``)."""
    if "/" not in full_id:
        return full_id
    return full_id.rsplit("/", maxsplit=1)[-1]


def parse_included_work_item_map(
    response: dict[str, object],
) -> dict[str, dict[str, object]]:
    """Map full work-item ID to its included resource dict from a response."""
    work_item_map: dict[str, dict[str, object]] = {}
    included = response.get("included", [])
    if isinstance(included, list):
        for inc in included:
            if isinstance(inc, dict) and inc.get("type") == "workitems":
                work_item_map[safe_str(inc.get("id", ""))] = inc
    return work_item_map


def parse_included_user_name_map(response: dict[str, object]) -> dict[str, str]:
    """Map user ID to display name from a response's included ``users`` resources."""
    user_map: dict[str, str] = {}
    included = response.get("included", [])
    if isinstance(included, list):
        for inc in included:
            if isinstance(inc, dict) and inc.get("type") == "users":
                user_id = safe_str(inc.get("id", ""))
                if not user_id:
                    # "" key would join with absent-author "" → phantom editor.
                    continue
                attrs = inc.get("attributes", {})
                name = attrs.get("name", "") if isinstance(attrs, dict) else ""
                user_map[user_id] = safe_str(name)
    return user_map


def parse_work_item_summary_kwargs(
    item: dict[str, object],
) -> WorkItemSummaryKwargs:
    """``WorkItemSummary`` kwargs from a JSON:API resource; shared so
    ``WorkItemDetail`` stays a strict superset of ``WorkItemSummary``.
    """
    attributes = item.get("attributes", {})
    if not isinstance(attributes, dict):
        attributes = {}
    relationships = item.get("relationships", {})
    if not isinstance(relationships, dict):
        relationships = {}

    module_id = extract_relationship_id(relationships, "module")
    space_id, document_name = split_module_id(module_id)
    assignee_ids = [
        extract_short_id(uid)
        for uid in extract_relationship_ids(relationships, "assignee")
    ]

    return {
        "id": extract_short_id(safe_str(item.get("id", ""))),
        "title": safe_str(attributes.get("title", "")),
        "type": safe_str(attributes.get("type", "")),
        "status": safe_str(attributes.get("status", "")),
        "priority": safe_str(attributes.get("priority", "")),
        "updated": safe_str(attributes.get("updated", "")),
        "space_id": space_id,
        "document_name": document_name,
        "assignee_ids": assignee_ids,
    }


def parse_hyperlinks(value: object) -> list[Hyperlink]:
    """Parse ``attributes.hyperlinks`` into ``Hyperlink`` models (no-uri skipped)."""
    if not isinstance(value, list):
        return []
    links: list[Hyperlink] = []
    for entry in value:
        if not isinstance(entry, dict):
            continue
        uri = safe_str(entry.get("uri", ""))
        if not uri:
            continue
        links.append(
            Hyperlink(
                role=safe_str(entry.get("role", "")),
                title=safe_str(entry.get("title", "")),
                uri=uri,
            )
        )
    return links


def parse_work_item_detail(
    item: dict[str, object],
    *,
    project_id: str,
    fallback_id: str = "",
) -> WorkItemDetail:
    """JSON:API work-item resource → ``WorkItemDetail``. Expects
    ``WORK_ITEM_DETAIL_FIELDS`` + ``include=assignee``; description passes
    through as raw HTML so it round-trips unchanged.
    """
    attributes = item.get("attributes", {})
    if not isinstance(attributes, dict):
        attributes = {}
    relationships = item.get("relationships", {})
    if not isinstance(relationships, dict):
        relationships = {}

    desc_obj = attributes.get("description", {})
    desc_html = ""
    if isinstance(desc_obj, dict):
        desc_html = safe_str(desc_obj.get("value", ""))

    summary_kwargs = parse_work_item_summary_kwargs(item)
    if not summary_kwargs["id"]:
        summary_kwargs["id"] = fallback_id

    return WorkItemDetail(
        **summary_kwargs,
        description_html=desc_html,
        project_id=project_id,
        author_id=extract_short_id(extract_relationship_id(relationships, "author")),
        created=safe_str(attributes.get("created", "")),
        resolution=safe_str(attributes.get("resolution", "")),
        severity=safe_str(attributes.get("severity", "")),
        outline_number=safe_str(attributes.get("outlineNumber", "")),
        hyperlinks=parse_hyperlinks(attributes.get("hyperlinks")),
        custom_fields=extract_custom_fields(attributes, STANDARD_WORK_ITEM_ATTRIBUTES),
    )


def summary_to_back_link(summary: WorkItemSummary) -> WorkItemLink:
    """Lift a ``linkedWorkItems:`` query result to a back link; the query
    exposes no role/suspect → ``role=None``, ``suspect=False``.
    """
    return WorkItemLink(
        id=summary.id,
        title=summary.title,
        role=None,
        direction="back",
        suspect=False,
        type=summary.type,
        status=summary.status,
        space_id=summary.space_id,
        document_name=summary.document_name,
    )


def parse_work_item_summaries(
    data: object,
) -> list[WorkItemSummary]:
    """Parse a JSON:API ``data`` array into ``WorkItemSummary`` models."""
    items: list[WorkItemSummary] = []
    if not isinstance(data, list):
        return items

    for item in data:
        if not isinstance(item, dict):
            continue
        items.append(WorkItemSummary(**parse_work_item_summary_kwargs(item)))
    return items


class TestRunSummaryKwargs(TypedDict):
    """Kwargs shape produced by ``parse_test_run_summary_kwargs``."""

    id: str
    title: str
    type: str
    status: str
    finished_on: str
    updated: str
    author_name: str
    is_template: bool


def parse_test_run_summary_kwargs(
    item: dict[str, object],
    user_names: dict[str, str],
) -> TestRunSummaryKwargs:
    """``TestRunSummary`` kwargs from a JSON:API resource; ``user_names`` maps the
    full author id to a display name (from the response's included ``users``).
    """
    attributes = item.get("attributes", {})
    if not isinstance(attributes, dict):
        attributes = {}
    relationships = item.get("relationships", {})
    if not isinstance(relationships, dict):
        relationships = {}

    author_id = extract_relationship_id(relationships, "author")
    return {
        "id": extract_short_id(safe_str(item.get("id", ""))),
        "title": safe_str(attributes.get("title", "")),
        "type": safe_str(attributes.get("type", "")),
        "status": safe_str(attributes.get("status", "")),
        "finished_on": safe_str(attributes.get("finishedOn", "")),
        "updated": safe_str(attributes.get("updated", "")),
        "author_name": user_names.get(author_id, ""),
        "is_template": bool(attributes.get("isTemplate", False)),
    }


def parse_test_run_summaries(response: dict[str, object]) -> list[TestRunSummary]:
    """Parse a test-runs list response into ``TestRunSummary`` models. Takes the
    whole response (not just ``data``) to resolve author display names from the
    included ``users`` resources.
    """
    user_names = parse_included_user_name_map(response)
    data = response.get("data", [])
    items: list[TestRunSummary] = []
    if not isinstance(data, list):
        return items

    for item in data:
        if not isinstance(item, dict):
            continue
        items.append(TestRunSummary(**parse_test_run_summary_kwargs(item, user_names)))
    return items


def _parse_comment(item: dict[str, object]) -> Comment:
    """Build a ``Comment`` from a JSON:API resource (short relationship IDs)."""
    attributes_raw = item.get("attributes")
    attributes: dict[str, object] = (
        attributes_raw if isinstance(attributes_raw, dict) else {}
    )
    relationships_raw = item.get("relationships")
    relationships: dict[str, object] = (
        relationships_raw if isinstance(relationships_raw, dict) else {}
    )

    text_value = ""
    text_format: Literal["text/html", "text/plain"] = "text/html"
    text_attr = attributes.get("text")
    if isinstance(text_attr, dict):
        text_value = safe_str(text_attr.get("value", ""))
        raw_format = safe_str(text_attr.get("type", "text/html"))
        text_format = "text/plain" if raw_format == "text/plain" else "text/html"

    author_full = extract_relationship_id(relationships, "author")
    parent_full = extract_relationship_id(relationships, "parentComment")
    child_full = extract_relationship_ids(relationships, "childComments")

    return Comment(
        id=extract_short_id(safe_str(item.get("id", ""))),
        created=safe_str(attributes.get("created", "")),
        resolved=bool(attributes.get("resolved", False)),
        title=safe_str(attributes.get("title", "")),
        text=text_value,
        text_format=text_format,
        author_id=extract_short_id(author_full) or None,
        parent_comment_id=extract_short_id(parent_full) or None,
        child_comment_ids=[extract_short_id(c) for c in child_full],
    )


def parse_comments_page(
    response: dict[str, object], page_number: int, page_size: int
) -> PaginatedResult[Comment]:
    """Parse a JSON:API comments response into a ``PaginatedResult`` page; shared
    by the document- and work-item comment list tools.
    """
    raw_data = response.get("data", [])
    comment_items: list[Comment] = []
    if isinstance(raw_data, list):
        for entry in raw_data:
            if isinstance(entry, dict):
                comment_items.append(_parse_comment(entry))

    return make_page(comment_items, response, page_number, page_size)


def parse_enum_option(entry: dict[str, object]) -> EnumOption:
    """JSON:API enumeration entry → ``EnumOption``; non-bool flags coerce to False."""

    def _bool(key: str) -> bool:
        value = entry.get(key)
        return value if isinstance(value, bool) else False

    return EnumOption(
        id=safe_str(entry.get("id", "")),
        name=safe_str(entry.get("name", "")),
        description=safe_str(entry.get("description", "")),
        default=_bool("default"),
        hidden=_bool("hidden"),
        terminal=_bool("terminal"),
    )


__all__: list[str] = [
    "WorkItemSummaryKwargs",
    "extract_relationship_id",
    "extract_relationship_ids",
    "extract_short_id",
    "parse_comments_page",
    "parse_enum_option",
    "parse_hyperlinks",
    "parse_included_user_name_map",
    "parse_included_work_item_map",
    "parse_work_item_detail",
    "parse_work_item_summaries",
    "parse_work_item_summary_kwargs",
    "split_module_id",
    "summary_to_back_link",
]
