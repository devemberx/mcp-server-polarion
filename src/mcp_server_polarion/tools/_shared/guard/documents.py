"""Document write guards: enum args and custom-field keys/values."""

from __future__ import annotations

from collections import Counter
from collections.abc import Iterable, Sequence

from mcp_server_polarion.core.client import PolarionClient
from mcp_server_polarion.tools._shared.cache import (
    get_document_type_custom_keys,
    store_document_type_custom_keys,
)
from mcp_server_polarion.tools._shared.custom_fields import (
    STANDARD_DOCUMENT_ATTRIBUTES,
)
from mcp_server_polarion.tools._shared.fields import DOCUMENT_DETAIL_FIELDS
from mcp_server_polarion.tools._shared.guard._attachment_refs import (
    DOCUMENT_ATTACHMENT_SCHEME,
    guard_attachment_refs_many,
)
from mcp_server_polarion.tools._shared.guard._custom_keys import check_custom_keys
from mcp_server_polarion.tools._shared.guard._http import guarded_pages
from mcp_server_polarion.tools._shared.guard.enums import (
    check_custom_field_enum_values,
    check_field_value,
    fetch_field_options,
)
from mcp_server_polarion.tools._shared.helpers import (
    encode_path_segment,
    format_option_list,
)
from mcp_server_polarion.tools._shared.sql import one_heading_per_document_sql


async def guard_document_enums(
    client: PolarionClient,
    project_id: str,
    document_type: str,
    *,
    type: str | None = None,
    status: str | None = None,
) -> None:
    """Validate every supplied document enum arg against ``getAvailableOptions``."""
    if type is not None and type != "":
        await check_field_value(client, project_id, "documents", "type", "~", type)
    if status is not None and status != "":
        await check_field_value(
            client, project_id, "documents", "status", document_type, status
        )


async def guard_document_rendering_layout_types(
    client: PolarionClient,
    project_id: str,
    types: Sequence[str],
) -> None:
    """Validate ``renderingLayouts`` work item type ids against the
    type-agnostic work item ``type`` enum.

    Polarion store unknown or blank layout type verbatim (204, no error) —
    ghost entry render nothing. Duplicate type also accepted server-side but
    UI precedence undefined, so refuse instead of dedupe silently.

    Message name ``rendering_layout_types``, never bare ``type`` — both write
    tools carry own ``type`` parameter, so ``check_field_value`` wording send model
    to wrong argument. Batched fetch report every bad id at once; per-id loop
    cost one failed write each.
    """
    if not types:
        return
    blank = sum(1 for type_id in types if not type_id.strip())
    if blank:
        raise ValueError(
            f"rendering_layout_types contains {blank} blank id(s). "
            "Polarion stores a blank entry verbatim and it renders nothing -- "
            "pass a work item type ID or drop the entry."
        )
    duplicates = sorted(
        type_id for type_id, count in Counter(types).items() if count > 1
    )
    if duplicates:
        raise ValueError(
            f"rendering_layout_types repeats {format_option_list(duplicates)}. "
            f"Each work item type may appear once -- Polarion accepts duplicate "
            f"entries but which one wins in the UI is undefined."
        )
    options = await fetch_field_options(client, project_id, "workitems", "type", "~")
    # Empty mapping = successful no-options fetch; defer rather than false-positive.
    if not options:
        return
    unknown = sorted(set(types) - options.keys())
    if unknown:
        raise ValueError(
            f"rendering_layout_types has unknown work item type ID(s) "
            f"{format_option_list(unknown)} in project '{project_id}'. "
            f"Valid options: {format_option_list(options.keys())}. "
            f"Unknown ids ghost silently (their fields never render) -- call "
            f"list_work_item_enum_options first."
        )


async def _fetch_document_type_custom_keys(
    client: PolarionClient,
    project_id: str,
    document_type: str,
) -> frozenset[str]:
    """Sample project documents, return *document_type*'s key schema.

    Heading-discovery SQL + ``include=module`` surface each document's type +
    inline customs — work on every build, unlike ``GET /documents``. All
    types' schemas stored (later writes hit cache); target type stored even
    when empty so no-customs type fail closed without re-probe. Headingless
    documents invisible to this sample.
    """
    path = f"/projects/{encode_path_segment(project_id)}/workitems"
    base_params: dict[str, str | int] = {
        "query": one_heading_per_document_sql(project_id),
        "include": "module",
        "fields[workitems]": "module",
        "fields[documents]": DOCUMENT_DETAIL_FIELDS,
    }
    by_type: dict[str, set[str]] = {}
    async for _data, response in guarded_pages(
        client, path, base_params, what="custom_fields keys", project_id=project_id
    ):
        included = response.get("included", [])
        if not isinstance(included, list):
            continue
        for entry in included:
            if not isinstance(entry, dict) or entry.get("type") != "documents":
                continue
            attrs = entry.get("attributes")
            if not isinstance(attrs, dict):
                continue
            dtype = attrs.get("type")
            if not isinstance(dtype, str) or not dtype:
                continue
            keys = by_type.setdefault(dtype, set())
            keys.update(
                k
                for k in attrs
                if isinstance(k, str) and k not in STANDARD_DOCUMENT_ATTRIBUTES
            )

    by_type.setdefault(document_type, set())
    for dtype, keys in by_type.items():
        store_document_type_custom_keys(project_id, dtype, frozenset(keys))
    return frozenset(by_type[document_type])


async def _check_document_custom_keys(
    client: PolarionClient,
    project_id: str,
    document_type: str,
    custom_fields: dict[str, object],
) -> None:
    await check_custom_keys(
        custom_fields,
        get_cached=lambda: get_document_type_custom_keys(project_id, document_type),
        fetch=lambda: _fetch_document_type_custom_keys(
            client, project_id, document_type
        ),
        scope=f"document type '{document_type}'",
        discovery_tool="sample of existing documents",
        empty_schema_error=(
            f"Cannot verify custom_fields {format_option_list(custom_fields)} for "
            f"document type '{document_type}' in project '{project_id}': no existing "
            f"document of this type has custom fields populated, so the schema can't "
            f"be sampled. Refusing the write -- an unknown key ghosts silently "
            f"(invisible to UI/Lucene). Do not create or edit documents to work around "
            f"this; ask the user to confirm these custom-field ids exist for this type."
        ),
    )


async def guard_document_custom_fields(
    client: PolarionClient,
    project_id: str,
    document_type: str,
    custom_fields: dict[str, object],
) -> None:
    """Document-axis mirror of :func:`guard_work_item_custom_fields`."""
    if not custom_fields:
        return
    await _check_document_custom_keys(client, project_id, document_type, custom_fields)
    await check_custom_field_enum_values(
        client, project_id, "documents", document_type, custom_fields
    )


async def _guard_document_attachment_refs(  # noqa: PLR0913
    client: PolarionClient,
    project_id: str,
    space_id: str,
    document_name: str,
    htmls: Iterable[str],
    what: str,
) -> None:
    path = (
        f"/projects/{encode_path_segment(project_id)}"
        f"/spaces/{encode_path_segment(space_id)}"
        f"/documents/{encode_path_segment(document_name)}"
        "/attachments"
    )
    await guard_attachment_refs_many(
        client,
        htmls,
        path=path,
        resource_type="document_attachments",
        expected_scheme=DOCUMENT_ATTACHMENT_SCHEME,
        list_tool="list_document_attachments",
        what=what,
        project_id=project_id,
    )


async def guard_document_attachment_refs(
    client: PolarionClient,
    project_id: str,
    space_id: str,
    document_name: str,
    html: str,
) -> None:
    """Update path: block ``home_page_content_html`` refs to attachments
    that don't exist yet, or use the ``workitemimg:`` scheme (never resolves
    in a document body).
    """
    await _guard_document_attachment_refs(
        client,
        project_id,
        space_id,
        document_name,
        [html],
        what=f"Document '{space_id}/{document_name}'",
    )


async def guard_document_comment_attachment_refs(
    client: PolarionClient,
    project_id: str,
    space_id: str,
    document_name: str,
    htmls: Iterable[str],
) -> None:
    """Create path, batch: block comment ``text`` refs to attachments that
    don't exist yet, or use the ``workitemimg:`` scheme (never resolves in a
    document comment). One GET over the whole comment batch.
    """
    await _guard_document_attachment_refs(
        client,
        project_id,
        space_id,
        document_name,
        htmls,
        what=f"Comment(s) on document '{space_id}/{document_name}'",
    )
