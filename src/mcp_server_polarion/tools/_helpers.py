"""Shared helpers for MCP tool implementations.

Internal module used by ``tools.read`` (and future ``tools.write``).
The helpers defined here are for internal use within the ``tools``
package and are **not** part of the public package API.
"""

from __future__ import annotations

from typing import Final, TypedDict, cast
from urllib.parse import quote

from fastmcp import Context

from mcp_server_polarion.core.client import PolarionClient
from mcp_server_polarion.models import (
    Hyperlink,
    JsonValue,
    LinkedWorkItemSummary,
    WorkItemDetail,
    WorkItemSummary,
)
from mcp_server_polarion.utils import html_to_markdown


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


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# Default page size — Polarion caps at 100.
DEFAULT_PAGE_SIZE: Final[int] = 100

# Sparse fieldsets for list / detail endpoints.
WI_LIST_FIELDS: Final[str] = "title,type,status,priority,updated,module,assignee"
# Detail fetches use the bare ``@all`` token because this Polarion server
# inlines custom fields as top-level keys under ``attributes`` (e.g.
# ``attributes.riskLevel``, ``attributes.effortHours``) rather than nesting
# them under a ``customFields`` container. The ``customFields.@all`` /
# ``@custom`` / ``@additional`` tokens are silently dropped on this server —
# only ``@all`` surfaces the inline custom attributes. Relationships still
# come back, so existing module/assignee/author parsing keeps working.
WI_DETAIL_FIELDS: Final[str] = "@all"
# Same situation for documents — ``@all`` is the only token that exposes
# project-defined custom document attributes. The slight per-request cost
# (``homePageContent`` always travels over the wire) is paid in network
# bytes only; the tool layer still hides the body from the LLM when
# ``include_content=False``.
DOC_DETAIL_FIELDS: Final[str] = "@all"

# Canonical standard attribute names per the Polarion REST OpenAPI schema.
# Used by ``extract_custom_fields`` as an allowlist: anything appearing in
# a response's ``attributes`` that is NOT in this set is treated as a
# project-defined custom field. Sourced verbatim from the OpenAPI workitem
# schema; a future Polarion release that adds new standard attributes
# would misclassify them as custom until this set is updated.
STANDARD_WORKITEM_ATTRS: Final[frozenset[str]] = frozenset(
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

# Canonical standard document attribute names per the Polarion REST
# OpenAPI schema. Mirrors ``STANDARD_WORKITEM_ATTRS`` for the document
# resource type.
STANDARD_DOCUMENT_ATTRS: Final[frozenset[str]] = frozenset(
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

# ---------------------------------------------------------------------------
# Functions
# ---------------------------------------------------------------------------


def get_client(ctx: Context) -> PolarionClient:
    """Extract ``PolarionClient`` from the lifespan context.

    Args:
        ctx: FastMCP tool context.

    Returns:
        The active ``PolarionClient`` instance.
    """
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
    """Extract ``meta.totalCount`` from a JSON:API response.

    Args:
        response: Decoded JSON:API response.

    Returns:
        The total count, or 0 if the field is missing.
    """
    meta = response.get("meta")
    if isinstance(meta, dict):
        total = meta.get("totalCount", 0)
        if isinstance(total, int):
            return total
    return 0


def has_links_next(response: dict[str, object]) -> bool:
    """Check whether the JSON:API response contains a ``links.next`` key.

    Args:
        response: Decoded JSON:API response.

    Returns:
        ``True`` if the server indicates a next page exists.
    """
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
    """URL-encode a single path segment (e.g. document name with spaces).

    Args:
        segment: Raw path segment string.

    Returns:
        URL-encoded segment safe for use in URL paths.
    """
    return quote(segment, safe="")


def build_included_workitem_map(
    response: dict[str, object],
) -> dict[str, dict[str, object]]:
    """Build a lookup dict of included work items from a JSON:API response.

    Args:
        response: Decoded JSON:API response with an ``included`` array.

    Returns:
        Mapping from full work-item ID to the included resource dict.
    """
    wi_map: dict[str, dict[str, object]] = {}
    included = response.get("included", [])
    if isinstance(included, list):
        for inc in included:
            if isinstance(inc, dict) and inc.get("type") == "workitems":
                wi_map[safe_str(inc.get("id", ""))] = inc
    return wi_map


def extract_relationship_id(
    rels: dict[str, object],
    rel_name: str,
) -> str:
    """Extract the ``data.id`` of a named relationship.

    Args:
        rels: The ``relationships`` dict of a JSON:API resource.
        rel_name: Relationship key (e.g. ``'nextPart'``).

    Returns:
        The related resource ID, or ``""`` if absent.
    """
    rel = rels.get(rel_name, {})
    if isinstance(rel, dict):
        inner = rel.get("data")
        if isinstance(inner, dict):
            return safe_str(inner.get("id", ""))
    return ""


def extract_relationship_ids(
    rels: dict[str, object],
    rel_name: str,
) -> list[str]:
    """Extract the ``data[].id`` list of a to-many relationship.

    Args:
        rels: The ``relationships`` dict of a JSON:API resource.
        rel_name: Relationship key (e.g. ``'assignee'``).

    Returns:
        List of related resource IDs in declaration order. Empty list
        when the relationship is absent or its data array is empty.
    """
    rel = rels.get(rel_name, {})
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
    """Split a module relationship ID into (space_id, document_name).

    The Polarion module ID has the format
    ``{projectId}/{spaceId}/{documentName}`` where ``documentName`` may
    itself contain ``/`` segments. Returns ``("", "")`` when the ID does
    not have at least three segments.
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
    """Extract ``WorkItemSummary`` kwargs from a single JSON:API resource.

    Centralises the attribute + relationship parsing used by both list
    and detail endpoints so that ``WorkItemDetail`` stays a strict
    superset of ``WorkItemSummary``.

    Args:
        item: A single JSON:API resource object (``data`` element).

    Returns:
        Dict suitable for ``WorkItemSummary(**kwargs)`` /
        ``WorkItemDetail(**kwargs, description=..., project_id=...)``.
    """
    attrs = item.get("attributes", {})
    if not isinstance(attrs, dict):
        attrs = {}
    rels = item.get("relationships", {})
    if not isinstance(rels, dict):
        rels = {}

    module_id = extract_relationship_id(rels, "module")
    space_id, document_name = split_module_id(module_id)
    assignee_ids = [
        extract_short_id(uid) for uid in extract_relationship_ids(rels, "assignee")
    ]

    return {
        "id": extract_short_id(safe_str(item.get("id", ""))),
        "title": safe_str(attrs.get("title", "")),
        "type": safe_str(attrs.get("type", "")),
        "status": safe_str(attrs.get("status", "")),
        "priority": safe_str(attrs.get("priority", "")),
        "updated": safe_str(attrs.get("updated", "")),
        "space_id": space_id,
        "document_name": document_name,
        "assignee_ids": assignee_ids,
    }


def extract_custom_fields(
    attrs: dict[str, object],
    standard: frozenset[str],
) -> dict[str, object]:
    """Return the inline custom-field subset of a JSON:API attributes dict.

    This Polarion server inlines project-defined custom fields as
    top-level keys inside ``attributes`` (e.g. ``attributes.riskLevel``,
    ``attributes.effortHours``) — there is no ``customFields``
    container, and the ``customFields.@all`` / ``@custom`` sparse-fieldset
    tokens are silently ignored. Callers fetch with ``fields[...]=@all``
    so every populated attribute comes back, then pass the resulting
    ``attributes`` dict here together with the standard-attribute
    allowlist for the resource type (``STANDARD_WORKITEM_ATTRS`` or
    ``STANDARD_DOCUMENT_ATTRS``). Anything not in the allowlist is
    reported as a custom field.

    Values are returned verbatim — primitives (str / int / float / bool /
    list) and rich-text dicts of the form
    ``{"type": "text/html", "value": "<...>"}`` both pass through so the
    shape round-trips back to Polarion unchanged on a future PATCH.
    """
    return {k: v for k, v in attrs.items() if k not in standard}


def merge_custom_fields(
    attributes: dict[str, JsonValue],
    customs: dict[str, object] | None,
    standard: frozenset[str],
) -> None:
    """Merge caller-supplied custom-field key/values into a JSON:API attributes dict.

    Write-side counterpart of ``extract_custom_fields``. Polarion accepts
    custom field values inlined at the top level of ``attributes`` (the
    same shape it returns on read), so this helper drops each entry of
    ``customs`` directly into ``attributes`` in place — keeping the
    "mutate-in-place" style of the surrounding ``_build_*_payload``
    helpers.

    Validation:
    - Any key in ``customs`` that also appears in ``standard`` raises
      ``ValueError`` — colliding with a Polarion-defined standard
      attribute would silently overwrite (or be overwritten by) an
      explicit standard tool parameter. Callers should rename the field
      or use the matching standard parameter.
    - ``None`` and ``{}`` whole-dict inputs are no-ops.
    - Individual ``None`` values inside the dict are skipped, matching
      the rest of the codebase's "skip None/empty" convention. Falsy
      non-``None`` values (``""``, ``0``, ``False``, ``[]``) are sent
      through verbatim because they may be meaningful custom-field
      values. Polarion may interpret some of those as "clear"; clearing
      semantics are intentionally out of scope for this phase.

    Aliasing: values are stored under ``attributes`` by reference — no
    defensive copy. Callers must NOT mutate the ``customs`` dict (or any
    nested value such as a ``{type, value}`` rich-text dict) between
    handing it to this helper and the eventual ``client.post`` /
    ``client.patch`` serialisation, or the on-wire payload will see the
    later mutation.

    Args:
        attributes: The JSON:API ``attributes`` dict under construction.
            Mutated in place.
        customs: Custom-field key/value pairs supplied by the caller, or
            ``None`` to skip.
        standard: The standard-attribute allowlist for the resource type
            (``STANDARD_WORKITEM_ATTRS`` or ``STANDARD_DOCUMENT_ATTRS``).

    Raises:
        ValueError: When any key in ``customs`` is also in ``standard``.
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
    """Parse the ``attributes.hyperlinks`` field into ``Hyperlink`` models.

    Polarion returns hyperlinks as a list of dicts with ``role``,
    ``title``, and ``uri`` keys. Entries without a usable ``uri`` are
    skipped to keep the response signal clean for the LLM.
    """
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
    """Parse a single JSON:API work-item resource into a ``WorkItemDetail``.

    Shared by ``get_work_item`` and ``update_work_item`` (which issues a
    follow-up GET after the PATCH succeeds). Expects the resource to
    have been fetched with ``fields[workitems]=WI_DETAIL_FIELDS`` and
    ``include=assignee`` so that ``relationships.assignee.data`` is
    populated.

    Args:
        item: A single JSON:API resource object (the ``data`` element).
        project_id: Project that contains this work item.
        fallback_id: Used as ``WorkItemDetail.id`` when ``item.id`` is
            missing. Pass the caller-supplied work-item ID.

    Returns:
        A fully-populated ``WorkItemDetail`` model.
    """
    attrs = item.get("attributes", {})
    if not isinstance(attrs, dict):
        attrs = {}
    rels = item.get("relationships", {})
    if not isinstance(rels, dict):
        rels = {}

    desc_obj = attrs.get("description", {})
    desc_html = ""
    if isinstance(desc_obj, dict):
        desc_html = safe_str(desc_obj.get("value", ""))

    summary_kwargs = build_work_item_summary_kwargs(item)
    if not summary_kwargs["id"]:
        summary_kwargs["id"] = fallback_id

    return WorkItemDetail(
        **summary_kwargs,
        description=html_to_markdown(desc_html),
        project_id=project_id,
        author_id=extract_short_id(extract_relationship_id(rels, "author")),
        created=safe_str(attrs.get("created", "")),
        resolution=safe_str(attrs.get("resolution", "")),
        severity=safe_str(attrs.get("severity", "")),
        outline_number=safe_str(attrs.get("outlineNumber", "")),
        hyperlinks=parse_hyperlinks(attrs.get("hyperlinks")),
        custom_fields=extract_custom_fields(attrs, STANDARD_WORKITEM_ATTRS),
    )


def summary_to_back_linked(summary: WorkItemSummary) -> LinkedWorkItemSummary:
    """Convert a ``WorkItemSummary`` from a ``linkedWorkItems:`` query
    into a back-direction ``LinkedWorkItemSummary``.

    Polarion's ``linkedWorkItems:`` Lucene query returns a flat list of
    source work items without the originating link's role or suspect
    flag (the ``/backlinkedworkitems`` endpoint that would expose them
    is not available on this server, see CLAUDE.md). Both fields are
    therefore set to explicit "unknown" defaults — ``role=None`` (do
    NOT substitute a placeholder string; ``None`` signals to callers
    that the value is unknown, not absent) and ``suspect=False``.
    """
    return LinkedWorkItemSummary(
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
    """Parse a JSON:API ``data`` array into ``WorkItemSummary`` models.

    Args:
        data: The ``data`` field from a JSON:API response.

    Returns:
        List of parsed ``WorkItemSummary`` instances.
    """
    items: list[WorkItemSummary] = []
    if not isinstance(data, list):
        return items

    for item in data:
        if not isinstance(item, dict):
            continue
        items.append(WorkItemSummary(**build_work_item_summary_kwargs(item)))
    return items


__all__: list[str] = [
    "DEFAULT_PAGE_SIZE",
    "DOC_DETAIL_FIELDS",
    "STANDARD_DOCUMENT_ATTRS",
    "STANDARD_WORKITEM_ATTRS",
    "WI_DETAIL_FIELDS",
    "WI_LIST_FIELDS",
    "build_included_workitem_map",
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
    "summary_to_back_linked",
]
