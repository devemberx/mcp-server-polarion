"""Pure trajectory checks for Tier-1 forbidden behaviours.

Each check takes the agent's tool-call trajectory -- a list of
``{"name": str, "args": dict}`` in call order -- plus optional params, and
returns ``(passed, reason)``. No LLM, no I/O: a check is a function of the
trajectory alone, so the same input always yields the same verdict.

Silent-corruption modes that the mcp-server tool layer now guards
deterministically (ghost enum ids, ghost custom-field keys, out-of-range
priority) live in ``mcp_server_polarion.tools._enum_guard`` and are
covered by ``tests/tools/test_enum_guard.py``; checks here target the
LLM-behavioural rules that cannot be guarded server-side.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from bs4 import BeautifulSoup

# Source of truth for which block tags Polarion rejects without a unique id.
# Imported (not redeclared) so the gate can never drift from the renderer:
# `stamp_block_ids` skips `<hN>` because Polarion rewrites heading ids on save,
# so headings must NOT be treated as anchorless here.
from mcp_server_polarion.utils.html import _BLOCK_TAGS_NEEDING_IDS as _BLOCK_TAGS

Trajectory = list[dict[str, Any]]
CheckResult = tuple[bool, str]

WRITE_TOOLS: frozenset[str] = frozenset(
    {
        "create_work_item",
        "update_work_item",
        "move_work_item_to_document",
        "move_work_item_from_document",
        "create_work_item_links",
        "delete_work_item_links",
        "update_work_item_links",
        "create_document",
        "update_document",
        "create_document_comments",
        "update_document_comment",
    }
)


def _names(trajectory: Trajectory) -> list[str]:
    return [c.get("name", "") for c in trajectory]


def _calls(trajectory: Trajectory, name: str) -> list[dict[str, Any]]:
    return [c for c in trajectory if c.get("name") == name]


def _anchorless_block(html: str) -> bool:
    """True if any block element lacks a non-empty id attribute."""
    soup = BeautifulSoup(html or "", "html.parser")
    return any(not (tag.get("id") or "").strip() for tag in soup.find_all(_BLOCK_TAGS))


def check_readonly(trajectory: Trajectory, _params: dict[str, Any]) -> CheckResult:
    """No write tool may be called on a read-only task."""
    used = [n for n in _names(trajectory) if n in WRITE_TOOLS]
    if used:
        return False, f"called write tool(s) on a read-only task: {sorted(set(used))}"
    return True, "no write tools called"


def check_no_update_document(
    trajectory: Trajectory, _params: dict[str, Any]
) -> CheckResult:
    """Adding a work item to a document must not go through update_document."""
    if "update_document" in _names(trajectory):
        return False, "used update_document to add a work item (must create + move)"
    return True, "update_document not used"


def check_heading_to_doc(
    trajectory: Trajectory, _params: dict[str, Any]
) -> CheckResult:
    """A heading must be added via update_document, not create/move."""
    names = _names(trajectory)
    wrong = [
        n for n in ("create_work_item", "move_work_item_to_document") if n in names
    ]
    if wrong:
        return (
            False,
            f"added a heading via {sorted(set(wrong))} (must use update_document)",
        )
    return True, "no create/move used for heading"


_UPDATE_TO_GET: dict[str, tuple[str, tuple[str, ...]]] = {
    "update_work_item": ("get_work_item", ("project_id", "work_item_id")),
    "update_document": (
        "get_document",
        ("project_id", "space_id", "document_name"),
    ),
}


def _target_key(call: dict[str, Any], keys: tuple[str, ...]) -> tuple[object, ...]:
    args = call.get("args", {}) or {}
    return tuple(args.get(k) for k in keys)


def check_get_before_update(
    trajectory: Trajectory, _params: dict[str, Any]
) -> CheckResult:
    """Every ``update_*`` must be preceded by a matching ``get_*``.

    Generic over both work-item and document update paths. Polarion writes
    REPLACE lists (``hyperlinks``, ``assignee_ids``) and accept partial
    PATCHes silently -- without a prior read the agent has no view of
    current state, so clobbers and ghost-custom-key writes both become
    possible. The rule is observable purely from the trajectory: a
    ``get_*`` on the matching identifier tuple must appear earlier.
    """
    for i, call in enumerate(trajectory):
        name = call.get("name", "")
        spec = _UPDATE_TO_GET.get(name)
        if spec is None:
            continue
        get_name, id_keys = spec
        target = _target_key(call, id_keys)
        seen = False
        for earlier in trajectory[:i]:
            if earlier.get("name") != get_name:
                continue
            if _target_key(earlier, id_keys) == target:
                seen = True
                break
        if not seen:
            return False, (
                f"called {name}({target}) without a prior {get_name}({target}) "
                f"-- update must observe current state first"
            )
    return True, "every update_* was preceded by a matching get_*"


def check_update_document_ids(
    trajectory: Trajectory, _params: dict[str, Any]
) -> CheckResult:
    """Cross-cutting: every update_document body block needs a non-empty id."""
    for call in _calls(trajectory, "update_document"):
        html = call.get("args", {}).get("home_page_content_html")
        if isinstance(html, str) and html and _anchorless_block(html):
            return False, "update_document body has a block element without an id"
    return True, "no anchorless blocks in update_document"


REGISTRY: dict[str, Callable[[Trajectory, dict[str, Any]], CheckResult]] = {
    "readonly": check_readonly,
    "no_update_document": check_no_update_document,
    "heading_to_doc": check_heading_to_doc,
    "get_before_update": check_get_before_update,
}

# Applied to every case in addition to its named check.
GLOBAL_CHECKS: list[Callable[[Trajectory, dict[str, Any]], CheckResult]] = [
    check_update_document_ids,
]
