"""Internal shared helpers for the ``tools`` package (not public API).

Body fields pass through as raw HTML on the get/update round-trip; Markdown
conversion is reserved for synthesis paths in ``tools.documents``.
"""

from __future__ import annotations

import re
from typing import Final, Literal, TypedDict, cast
from urllib.parse import quote

from fastmcp import Context

from mcp_server_polarion.core.client import PolarionClient
from mcp_server_polarion.models import (
    DocumentComment,
    EnumOption,
    Hyperlink,
    JsonValue,
    WorkItemDetail,
    WorkItemLink,
    WorkItemSummary,
)

# Bulk-write cap. Polarion throttles ~3 req/s with no concurrency, so an
# unbounded batch is a rate-limit/payload hazard; 50 bounds one request.
MAX_BULK_ITEMS: Final[int] = 50


class WorkItemSummaryKwargs(TypedDict):
    """Kwargs shape produced by ``build_work_item_summary_kwargs``."""

    id: str
    title: str
    type: str
    status: str
    priority: str
    updated: str
    space_id: str
    document_name: str
    assignee_ids: list[str]


# Polarion enforces a hard cap of 100 server-side.
DEFAULT_PAGE_SIZE: Final[int] = 100

# Sparse fieldsets. Detail/document fetches use ``@all`` (this server inlines
# customs under ``attributes`` and drops ``customFields.*`` tokens);
# ``WORK_ITEM_PART_FIELDS`` stays tight (parts surface no customs).
WORK_ITEM_LIST_FIELDS: Final[str] = "title,type,status,priority,updated,module,assignee"
WORK_ITEM_DETAIL_FIELDS: Final[str] = "@all"
WORK_ITEM_PART_FIELDS: Final[str] = "title,type,status,description,outlineNumber"
DOCUMENT_DETAIL_FIELDS: Final[str] = "@all"
# Sparse fieldset filters relationships too, so author / parentComment /
# childComments must be named explicitly. Comments have no customs, so no `@all`.
DOCUMENT_COMMENT_LIST_FIELDS: Final[str] = (
    "created,resolved,text,author,parentComment,childComments"
)

# Standard attribute allowlist (Polarion REST OpenAPI schema). Anything in
# ``attributes`` outside this set is treated as a custom field, so a new
# standard attribute is misclassified until added here.
STANDARD_WORK_ITEM_ATTRIBUTES: Final[frozenset[str]] = frozenset(
    {
        "id",
        "type",
        "title",
        "description",
        "status",
        "priority",
        "severity",
        "resolution",
        "resolvedOn",
        "created",
        "updated",
        "outlineNumber",
        "dueDate",
        "plannedStart",
        "plannedEnd",
        "initialEstimate",
        "remainingEstimate",
        "timeSpent",
        "hyperlinks",
    }
)

# Standard document attributes (Polarion REST OpenAPI schema); document-side
# mirror of ``STANDARD_WORK_ITEM_ATTRIBUTES``.
STANDARD_DOCUMENT_ATTRIBUTES: Final[frozenset[str]] = frozenset(
    {
        "id",
        "title",
        "type",
        "status",
        "homePageContent",
        "moduleFolder",
        "moduleName",
        "outlineNumbering",
        "renderingLayouts",
        "structureLinkRole",
        "usesOutlineNumbering",
        "autoSuspect",
        "branchedWithInitializedFields",
        "branchedWithQuery",
        "derivedFields",
        "derivedFromLinkRole",
        "created",
        "updated",
    }
)


def get_client(ctx: Context) -> PolarionClient:
    """Extract the active ``PolarionClient`` from the lifespan context."""
    lifespan_ctx = ctx.lifespan_context
    if "polarion_client" not in lifespan_ctx:  # pragma: no cover
        msg = "polarion_client is missing from lifespan_context"
        raise TypeError(msg)

    client = lifespan_ctx["polarion_client"]
    if not isinstance(client, PolarionClient):  # pragma: no cover
        msg = (
            "polarion_client is not a PolarionClient instance"
            f" (got {type(client).__name__})"
        )
        raise TypeError(msg)
    return client


def safe_str(value: object) -> str:
    """Convert a value to ``str``, returning ``""`` for ``None``."""
    if value is None:
        return ""
    return str(value)


def extract_total_count(response: dict[str, object]) -> int:
    """Return ``meta.totalCount`` from a JSON:API response, or 0 if missing."""
    meta = response.get("meta")
    if isinstance(meta, dict):
        total = meta.get("totalCount", 0)
        if isinstance(total, int):
            return total
    return 0


def has_links_next(response: dict[str, object]) -> bool:
    """Return whether the JSON:API response carries a ``links.next`` key."""
    links = response.get("links")
    if isinstance(links, dict):
        return "next" in links
    return False


def compute_has_more(
    response: dict[str, object],
    total: int,
    page_number: int,
    page_size: int,
    items_count: int,
) -> bool:
    """Determine whether more pages exist after the current one.

    Uses ``total`` when it is reliable (> 0).  When ``total`` is 0
    (Polarion sometimes omits ``meta.totalCount``), falls back to
    ``links.next`` if present, otherwise to a heuristic based on
    whether the current page is full.

    Args:
        response: Decoded JSON:API response (used for ``links.next``).
        total: Resolved total count (may be 0 if unknown).
        page_number: Current 1-based page number.
        page_size: Requested page size.
        items_count: Number of items returned on this page.

    Returns:
        ``True`` if additional pages likely exist.
    """
    if total > 0:
        return total > page_number * page_size
    # totalCount unavailable — prefer links.next, else heuristic.
    if has_links_next(response):
        return True
    return items_count == page_size


def encode_path_segment(segment: str) -> str:
    """URL-encode a single path segment (e.g. a document name with spaces)."""
    return quote(segment, safe="")


# Thin guard, not a format validator: accepts ``[A-Za-z0-9_-]`` (covers
# ``MCPT-001``-style ids) before substituting into a Lucene ``linkedWorkItems:``.
_WORK_ITEM_ID_PATTERN: Final[re.Pattern[str]] = re.compile(r"^[A-Za-z0-9_-]+$")


def validate_work_item_id_for_lucene(work_item_id: str) -> None:
    """Reject work item IDs that would break a Lucene ``field:<id>`` clause.

    Lucene treats many punctuation chars as operators; an unescaped id in
    ``linkedWorkItems:<id>`` could reshape the query. Polarion ids never use
    those, so hard-reject anything outside ``[A-Za-z0-9_-]`` (``ValueError``).
    """
    if not _WORK_ITEM_ID_PATTERN.match(work_item_id):
        msg = (
            f"work_item_id '{work_item_id}' contains characters outside "
            "[A-Za-z0-9_-]; cannot embed safely in a Lucene query."
        )
        raise ValueError(msg)


def build_included_work_item_map(
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


def build_included_user_name_map(response: dict[str, object]) -> dict[str, str]:
    """Map user ID to display name from a response's included ``users`` resources."""
    user_map: dict[str, str] = {}
    included = response.get("included", [])
    if isinstance(included, list):
        for inc in included:
            if isinstance(inc, dict) and inc.get("type") == "users":
                attrs = inc.get("attributes", {})
                name = attrs.get("name", "") if isinstance(attrs, dict) else ""
                user_map[safe_str(inc.get("id", ""))] = safe_str(name)
    return user_map


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
    """Split a module ID ``{proj}/{space}/{doc}`` into (space_id, document_name).

    ``doc`` may contain ``/``. Returns ``("", "")`` if under three segments.
    """
    if not module_full_id:
        return ("", "")
    parts = module_full_id.split("/", 2)
    expected_segments = 3
    if len(parts) < expected_segments:
        return ("", "")
    return (parts[1], parts[2])


def extract_short_id(full_id: str) -> str:
    """Strip the project / path prefix from a JSON:API ID.

    For ``"projectId/MCPT-001"`` returns ``"MCPT-001"``.
    For ``"alice"`` (no slashes) returns ``"alice"`` unchanged.
    """
    if "/" not in full_id:
        return full_id
    return full_id.rsplit("/", maxsplit=1)[-1]


def build_work_item_summary_kwargs(
    item: dict[str, object],
) -> WorkItemSummaryKwargs:
    """Extract ``WorkItemSummary`` kwargs from a JSON:API resource.

    Shared by list and detail endpoints so ``WorkItemDetail`` stays a strict
    superset of ``WorkItemSummary``.
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


def extract_custom_fields(
    attributes: dict[str, object],
    standard: frozenset[str],
) -> dict[str, object]:
    """Return the inline custom-field subset of a JSON:API attributes dict.

    This server inlines customs as top-level ``attributes`` keys (no
    ``customFields`` container). Anything outside *standard* is a custom field,
    returned verbatim (primitives or ``{type,value}`` rich-text) so it
    round-trips unchanged.
    """
    return {k: v for k, v in attributes.items() if k not in standard}


def merge_custom_fields(
    attributes: dict[str, JsonValue],
    customs: dict[str, object] | None,
    standard: frozenset[str],
) -> None:
    """Merge caller custom-field key/values into *attributes* in place.

    Write-side counterpart of ``extract_custom_fields`` (customs inline at the
    top level). A key in *standard* raises ``ValueError`` (would shadow a tool
    parameter). ``None`` / ``{}`` are no-ops; individual ``None`` values
    skipped, other falsy values sent verbatim.

    Aliasing: stored by reference, no copy — callers must NOT mutate *customs*
    (or nested ``{type,value}`` dicts) before serialisation.
    """
    if not customs:
        return
    collisions = sorted(set(customs) & standard)
    if collisions:
        msg = (
            "custom_fields keys collide with standard Polarion attributes: "
            f"{collisions}. Use the matching standard tool parameter "
            "(e.g. ``title=``, ``status=``) instead of overriding via "
            "custom_fields."
        )
        raise ValueError(msg)
    for key, value in customs.items():
        if value is None:
            continue
        attributes[key] = cast(JsonValue, value)


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
    """Parse a JSON:API work-item resource into a ``WorkItemDetail``.

    Shared by ``get_work_item`` / ``update_work_item``. Expects fetch with
    ``WORK_ITEM_DETAIL_FIELDS`` + ``include=assignee``. Description passes
    through as raw HTML (no convert/sanitize) so it round-trips unchanged.
    ``fallback_id`` is used as ``id`` when ``item.id`` is missing.
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

    summary_kwargs = build_work_item_summary_kwargs(item)
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
    """Lift a ``linkedWorkItems:`` query result to a back-direction link.

    The query exposes no role/suspect, so ``role=None`` and ``suspect=False``.
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
        items.append(WorkItemSummary(**build_work_item_summary_kwargs(item)))
    return items


def build_document_comment(item: dict[str, object]) -> DocumentComment:
    """Build a ``DocumentComment`` from a JSON:API resource (short relationship IDs)."""
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

    return DocumentComment(
        id=extract_short_id(safe_str(item.get("id", ""))),
        created=safe_str(attributes.get("created", "")),
        resolved=bool(attributes.get("resolved", False)),
        text=text_value,
        text_format=text_format,
        author_id=extract_short_id(author_full) or None,
        parent_comment_id=extract_short_id(parent_full) or None,
        child_comment_ids=[extract_short_id(c) for c in child_full],
    )


def build_enum_option(entry: dict[str, object]) -> EnumOption:
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
    "DEFAULT_PAGE_SIZE",
    "DOCUMENT_COMMENT_LIST_FIELDS",
    "DOCUMENT_DETAIL_FIELDS",
    "MAX_BULK_ITEMS",
    "STANDARD_DOCUMENT_ATTRIBUTES",
    "STANDARD_WORK_ITEM_ATTRIBUTES",
    "WORK_ITEM_DETAIL_FIELDS",
    "WORK_ITEM_LIST_FIELDS",
    "WORK_ITEM_PART_FIELDS",
    "build_document_comment",
    "build_enum_option",
    "build_included_user_name_map",
    "build_included_work_item_map",
    "build_work_item_summary_kwargs",
    "compute_has_more",
    "encode_path_segment",
    "extract_custom_fields",
    "extract_relationship_id",
    "extract_relationship_ids",
    "extract_short_id",
    "extract_total_count",
    "get_client",
    "has_links_next",
    "merge_custom_fields",
    "parse_hyperlinks",
    "parse_work_item_detail",
    "parse_work_item_summaries",
    "safe_str",
    "split_module_id",
    "summary_to_back_link",
    "validate_work_item_id_for_lucene",
]
