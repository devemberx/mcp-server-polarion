"""Pre-write guards: Polarion persist unknown enum ids / custom-field keys as
silent ghosts (HTTP 200, invisible to UI and Lucene) — each guard fetch real
options and raise before write. Fail-closed — validation error block write
(auth → ``PermissionError``, else ``RuntimeError``); only *successful* empty
option set and 404 defer to Polarion. Caching in :mod:`...tools._shared.cache`.

Naming: ``guard_*`` = pure validators (return ``None``, raise on invalid);
``resolve_*``/``partition_*`` = fail-closed helpers whose validation result
caller also need as data.
"""

from __future__ import annotations

from mcp_server_polarion.tools._shared.guard.documents import (
    guard_document_custom_fields,
    guard_document_enums,
)
from mcp_server_polarion.tools._shared.guard.links import (
    guard_hyperlink_roles,
    guard_work_item_link_roles,
    guard_work_item_link_targets,
    partition_delete_links,
)
from mcp_server_polarion.tools._shared.guard.test_runs import (
    guard_test_run_custom_fields,
    guard_test_run_enums,
    guard_test_run_templates,
)
from mcp_server_polarion.tools._shared.guard.work_items import (
    guard_work_item_custom_fields,
    guard_work_item_enums,
    resolve_work_item_types,
)

__all__ = [
    "guard_document_custom_fields",
    "guard_document_enums",
    "guard_hyperlink_roles",
    "guard_test_run_custom_fields",
    "guard_test_run_enums",
    "guard_test_run_templates",
    "guard_work_item_custom_fields",
    "guard_work_item_enums",
    "guard_work_item_link_roles",
    "guard_work_item_link_targets",
    "partition_delete_links",
    "resolve_work_item_types",
]
