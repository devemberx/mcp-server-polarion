"""Document write guards: enum args and custom-field keys/values."""

from __future__ import annotations

from mcp_server_polarion.core.client import PolarionClient
from mcp_server_polarion.core.exceptions import PolarionAuthError, PolarionError
from mcp_server_polarion.tools._shared.cache import (
    get_document_type_custom_keys,
    invalidate_document_type_custom_keys,
    store_document_type_custom_keys,
)
from mcp_server_polarion.tools._shared.custom_fields import (
    STANDARD_DOCUMENT_ATTRIBUTES,
)
from mcp_server_polarion.tools._shared.fields import DOCUMENT_DETAIL_FIELDS
from mcp_server_polarion.tools._shared.guard._custom_keys import check_custom_keys
from mcp_server_polarion.tools._shared.guard._errors import (
    unauthorized_write_block,
    unreachable_write_block,
)
from mcp_server_polarion.tools._shared.guard._http import (
    GUARD_PAGE_SIZE,
    paged_responses,
)
from mcp_server_polarion.tools._shared.guard.enums import (
    check_custom_field_enum_values,
    check_enum,
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
        await check_enum(client, project_id, "documents", "type", "~", type)
    if status is not None and status != "":
        await check_enum(
            client, project_id, "documents", "status", document_type, status
        )


async def _fetch_document_type_custom_keys(
    client: PolarionClient,
    project_id: str,
    document_type: str,
) -> frozenset[str]:
    """Sample the project's documents and return *document_type*'s key schema.

    Heading-discovery SQL + ``include=module`` surfaces each document's type and
    inline customs — works on every build, unlike ``GET /documents``. All types'
    schemas are stored (later writes hit cache); target type stored even when
    empty so a no-customs type fails closed without re-probing. Headingless
    documents are invisible to this sample.
    """
    path = f"/projects/{encode_path_segment(project_id)}/workitems"
    base_params: dict[str, str | int] = {
        "query": one_heading_per_document_sql(project_id),
        "include": "module",
        "fields[workitems]": "module",
        "fields[documents]": DOCUMENT_DETAIL_FIELDS,
        "page[size]": GUARD_PAGE_SIZE,
    }
    by_type: dict[str, set[str]] = {}
    try:
        async for response in paged_responses(client, path, base_params):
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
    except PolarionAuthError as exc:
        raise unauthorized_write_block("custom_fields keys", project_id) from exc
    except PolarionError as exc:
        raise unreachable_write_block("custom_fields keys", project_id, exc) from exc

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
        invalidate=lambda: invalidate_document_type_custom_keys(
            project_id, document_type
        ),
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
