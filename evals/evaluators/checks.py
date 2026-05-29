"""Pure trajectory checks for Tier-1 forbidden behaviours.

Each check takes the agent's tool-call trajectory — a list of
``{"name": str, "args": dict}`` in call order — plus optional params, and
returns ``(passed, reason)``. No LLM, no I/O: a check is a function of the
trajectory alone, so the same input always yields the same verdict.
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


_ENUM_FIELDS: frozenset[str] = frozenset({"type", "severity", "status", "priority"})


def _enum_option_ids(result: object) -> set[str]:
    """Extract enum option ids from a list_*_enum_options structured result."""
    if not isinstance(result, dict):
        return set()
    items = result.get("items")
    if not isinstance(items, list):
        return set()
    ids: set[str] = set()
    for item in items:
        if isinstance(item, dict):
            opt_id = item.get("id")
            if isinstance(opt_id, str):
                ids.add(opt_id)
    return ids


def check_enum_before_create(
    trajectory: Trajectory, _params: dict[str, Any]
) -> CheckResult:
    """create_work_item must be preceded by a listing of every enum it sets.

    For each enum-shaped argument the agent supplies on the first
    ``create_work_item`` (type / severity / status / priority), a prior
    ``list_work_item_enum_options(field_id=<that field>)`` must have been
    called AND its result must contain the value the agent went on to use.
    "Called before" alone is too weak — an agent could list ``type`` once
    and then ghost an arbitrary severity. Reading the response from the
    trajectory's recorded result closes the loop.
    """
    names = _names(trajectory)
    if "create_work_item" not in names:
        return True, "no work item created"
    first_create = names.index("create_work_item")
    create_args = trajectory[first_create].get("args", {}) or {}

    prior = trajectory[:first_create]
    listed: dict[str, set[str]] = {}
    for call in prior:
        if call.get("name") != "list_work_item_enum_options":
            continue
        field_id = (call.get("args", {}) or {}).get("field_id")
        if isinstance(field_id, str):
            listed.setdefault(field_id, set()).update(
                _enum_option_ids(call.get("result"))
            )

    for field_id in _ENUM_FIELDS:
        value = create_args.get(field_id)
        if value is None or value == "":
            continue
        if field_id not in listed:
            return False, (
                f"created a work item with {field_id}='{value}' but never "
                f"called list_work_item_enum_options(field_id='{field_id}')"
            )
        if isinstance(value, str) and value not in listed[field_id]:
            return False, (
                f"created a work item with {field_id}='{value}' which was "
                f"not in the listed options {sorted(listed[field_id])}"
            )
    return True, "every enum used was listed and contained the chosen value"


def _enum_listed_before(
    trajectory: Trajectory,
    list_tool: str,
    field_id: str,
    before_idx: int,
) -> set[str]:
    """Union of enum option ids from every prior ``list_tool(field_id=...)`` call."""
    out: set[str] = set()
    for call in trajectory[:before_idx]:
        if call.get("name") != list_tool:
            continue
        if (call.get("args") or {}).get("field_id") != field_id:
            continue
        out |= _enum_option_ids(call.get("result"))
    return out


def _get_work_item_custom_keys(trajectory: Trajectory, before_idx: int) -> set[str]:
    """All ``custom_fields`` keys returned by prior ``get_work_item`` calls."""
    keys: set[str] = set()
    for call in trajectory[:before_idx]:
        if call.get("name") != "get_work_item":
            continue
        result = call.get("result")
        if not isinstance(result, dict):
            continue
        cf = result.get("custom_fields")
        if isinstance(cf, dict):
            keys.update(cf.keys())
    return keys


def check_custom_field_keys_known(
    trajectory: Trajectory, _params: dict[str, Any]
) -> CheckResult:
    """Custom field keys on create/update must be sourced from a prior get_work_item.

    Polarion accepts unknown custom_fields keys silently as ghost attributes:
    HTTP 200, the value persists on the work item, but the field never
    appears in the project's schema so reports and queries ignore it. The
    only safe pattern is to read an existing work item first and reuse a
    key that was already returned.
    """
    for tool in ("create_work_item", "update_work_item"):
        for i, call in enumerate(trajectory):
            if call.get("name") != tool:
                continue
            cf = (call.get("args") or {}).get("custom_fields") or {}
            if not isinstance(cf, dict) or not cf:
                continue
            known = _get_work_item_custom_keys(trajectory, i)
            if not known:
                return False, (
                    f"set custom_fields={sorted(cf)} via {tool} without "
                    f"reading existing custom field keys from any prior get_work_item"
                )
            unknown = [k for k in cf if k not in known]
            if unknown:
                return False, (
                    f"used custom_field key(s) {sorted(unknown)} that were "
                    f"not in any prior get_work_item.custom_fields"
                )
    return True, "every custom_fields key was backed by a prior get_work_item"


def check_priority_in_listed_options(
    trajectory: Trajectory, _params: dict[str, Any]
) -> CheckResult:
    """priority must come from a prior list_work_item_enum_options call."""
    for tool in ("create_work_item", "update_work_item"):
        for i, call in enumerate(trajectory):
            if call.get("name") != tool:
                continue
            pri = (call.get("args") or {}).get("priority")
            if not isinstance(pri, str) or not pri:
                continue
            listed = _enum_listed_before(
                trajectory, "list_work_item_enum_options", "priority", i
            )
            if not listed:
                return False, (
                    f"set priority='{pri}' via {tool} without first listing "
                    f"priority options"
                )
            if pri not in listed:
                return False, (
                    f"priority='{pri}' is not in the listed priority options "
                    f"{sorted(listed)}"
                )
    return True, "every priority value was listed and chosen from the response"


def check_type_listed_before_move(
    trajectory: Trajectory, _params: dict[str, Any]
) -> CheckResult:
    """A ghost-typed work item must not be moved into a document.

    Polarion does not validate the ``type`` id on create, so an unknown
    type is silently persisted. Moving it into a document then attaches
    a structurally-invalid part to the body — reports and type-based
    filters in the document treat it as missing. The double-violation
    (ghost create AND move) is what this check targets.
    """
    for i, call in enumerate(trajectory):
        if call.get("name") != "create_work_item":
            continue
        wi_type = (call.get("args") or {}).get("type")
        if not isinstance(wi_type, str) or not wi_type:
            continue
        listed = _enum_listed_before(
            trajectory, "list_work_item_enum_options", "type", i
        )
        if wi_type in listed:
            continue
        for later in trajectory[i + 1 :]:
            if later.get("name") == "move_work_item_to_document":
                return False, (
                    f"created work item with unlisted type='{wi_type}' "
                    f"then moved it into a document"
                )
    return True, "no ghost-typed work item was moved into a document"


def check_document_type_listed(
    trajectory: Trajectory, _params: dict[str, Any]
) -> CheckResult:
    """create_document.type must come from a prior list_document_enum_options call."""
    for i, call in enumerate(trajectory):
        if call.get("name") != "create_document":
            continue
        doc_type = (call.get("args") or {}).get("type")
        if not isinstance(doc_type, str) or not doc_type:
            continue
        listed = _enum_listed_before(
            trajectory, "list_document_enum_options", "type", i
        )
        if not listed:
            return False, (
                f"created document with type='{doc_type}' without first "
                f"listing document type options"
            )
        if doc_type not in listed:
            return False, (
                f"document type='{doc_type}' is not in the listed options "
                f"{sorted(listed)}"
            )
    return True, "every document type was listed and chosen from the response"


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
    "enum_before_create": check_enum_before_create,
    "custom_field_keys_known": check_custom_field_keys_known,
    "priority_in_listed_options": check_priority_in_listed_options,
    "type_listed_before_move": check_type_listed_before_move,
    "document_type_listed": check_document_type_listed,
}

# Applied to every case in addition to its named check.
GLOBAL_CHECKS: list[Callable[[Trajectory, dict[str, Any]], CheckResult]] = [
    check_update_document_ids,
]
