"""Test-run write guards: enums (``testing`` context), template resolution,
custom-field keys.
"""

from __future__ import annotations

from collections.abc import Iterable

from mcp_server_polarion.core.client import PolarionClient
from mcp_server_polarion.core.exceptions import (
    PolarionAuthError,
    PolarionError,
    PolarionNotFoundError,
)
from mcp_server_polarion.tools._shared.cache import (
    get_test_run_custom_keys,
    invalidate_test_run_custom_keys,
    store_test_run_custom_keys,
)
from mcp_server_polarion.tools._shared.custom_fields import (
    STANDARD_TEST_RUN_ATTRIBUTES,
)
from mcp_server_polarion.tools._shared.guard._common import (
    custom_keys_from_data_list,
    reject_unknown_custom_keys,
)
from mcp_server_polarion.tools._shared.guard._errors import (
    unauthorized_write_block,
    unreachable_write_block,
)
from mcp_server_polarion.tools._shared.guard._http import (
    GUARD_PAGE_SIZE,
    guarded_get,
    paged_responses,
)
from mcp_server_polarion.tools._shared.guard.enums import check_project_enum_roles
from mcp_server_polarion.tools._shared.helpers import (
    encode_path_segment,
    format_option_list,
)


async def guard_test_run_enums(
    client: PolarionClient,
    project_id: str,
    *,
    type: str | None = None,
    status: str | None = None,
) -> None:
    """Validate test-run ``type``/``status`` against the ``testing``-context
    project enumerations — testruns have no ``getAvailableOptions`` endpoint,
    and the ``~`` wildcard context does not resolve these enums.
    """
    await check_project_enum_roles(
        client,
        project_id,
        "testrun-type",
        [type] if type else [],
        field_label="test run type",
        discovery_hint="use list_test_runs to see values in use.",
        context="testing",
    )
    await check_project_enum_roles(
        client,
        project_id,
        "testrun-status",
        [status] if status else [],
        field_label="test run status",
        discovery_hint="use list_test_runs to see values in use.",
        context="testing",
    )


async def guard_test_run_templates(
    client: PolarionClient,
    project_id: str,
    template_ids: Iterable[str],
) -> None:
    """Resolve each template id before the write — Polarion doesn't validate
    relationship targets. Run instances are rejected too: ``isTemplate`` is
    served only (as ``true``) on templates, absent on instances.
    """
    for template_id in sorted({t for t in template_ids if t}):
        path = (
            f"/projects/{encode_path_segment(project_id)}"
            f"/testruns/{encode_path_segment(template_id)}"
        )
        try:
            response = await guarded_get(
                client,
                path,
                {"fields[testruns]": "id,isTemplate"},
                what="test run templates",
                project_id=project_id,
                propagate_not_found=True,
            )
        except PolarionNotFoundError as exc:
            raise ValueError(
                f"Test run template '{template_id}' not found in project "
                f"'{project_id}'. Use list_test_runs(templates=True) to "
                f"discover template ids."
            ) from exc

        data = response.get("data", {})
        attributes = data.get("attributes") if isinstance(data, dict) else None
        is_template = (
            attributes.get("isTemplate") if isinstance(attributes, dict) else None
        )
        if is_template is not True:
            raise ValueError(
                f"Test run '{template_id}' in project '{project_id}' is a run "
                f"instance, not a template. Use list_test_runs(templates=True) "
                f"to discover template ids."
            )


async def _fetch_test_run_custom_keys(
    client: PolarionClient,
    project_id: str,
) -> frozenset[str]:
    """Union of custom-field keys sampled from the project's runs and
    templates — testrun custom fields are project config (no type axis, no
    SQL needed). Cached even if empty.
    """
    path = f"/projects/{encode_path_segment(project_id)}/testruns"
    keys: set[str] = set()
    try:
        for templates in (False, True):
            base_params: dict[str, str | int] = {
                "fields[testruns]": "@all",
                "page[size]": GUARD_PAGE_SIZE,
            }
            if templates:
                base_params["templates"] = "true"
            async for response in paged_responses(client, path, base_params):
                keys.update(
                    custom_keys_from_data_list(response, STANDARD_TEST_RUN_ATTRIBUTES)
                )
    except PolarionAuthError as exc:
        raise unauthorized_write_block("custom_fields keys", project_id) from exc
    except PolarionError as exc:
        raise unreachable_write_block("custom_fields keys", project_id, exc) from exc

    result = frozenset(keys)
    store_test_run_custom_keys(project_id, result)
    return result


async def _check_test_run_custom_keys(
    client: PolarionClient,
    project_id: str,
    custom_fields: dict[str, object],
) -> None:
    """Test-run mirror of :func:`_check_work_item_custom_keys` (project scope)."""
    schema = get_test_run_custom_keys(project_id)
    fetched_fresh = schema is None
    if schema is None:
        schema = await _fetch_test_run_custom_keys(client, project_id)

    if all(key in schema for key in custom_fields):
        return

    # Unknown key may be admin-added since caching; refetch once before rejecting.
    if not fetched_fresh:
        invalidate_test_run_custom_keys(project_id)
        schema = await _fetch_test_run_custom_keys(client, project_id)

    if not schema:
        raise RuntimeError(
            f"Cannot verify custom_fields {format_option_list(custom_fields)} for "
            f"test runs in project '{project_id}': no existing test run has custom "
            f"fields populated, so the schema can't be sampled. Refusing the write "
            f"-- an unknown key ghosts silently (invisible to UI/Lucene). Do not "
            f"create runs to work around this; ask the user to confirm these "
            f"custom-field ids exist for test runs."
        )

    reject_unknown_custom_keys(
        custom_fields,
        schema,
        scope=f"test runs in project '{project_id}'",
        discovery_tool="sample of existing runs",
    )


async def guard_test_run_custom_fields(
    client: PolarionClient,
    project_id: str,
    custom_fields: dict[str, object],
) -> None:
    """Validate ``custom_fields`` keys before a test-run write. Keys only —
    testruns have no ``getAvailableOptions``, so enum-typed *values* defer to
    Polarion (wrong option ids there ghost; keys are the guardable axis).
    """
    if not custom_fields:
        return
    await _check_test_run_custom_keys(client, project_id, custom_fields)
