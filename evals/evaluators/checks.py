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


def check_no_move_heading(
    trajectory: Trajectory, params: dict[str, Any]
) -> CheckResult:
    """A heading-type work item must never be moved into a document."""
    heading_ids = set(params.get("heading_ids", []))
    for call in _calls(trajectory, "move_work_item_to_document"):
        wi = call.get("args", {}).get("work_item_id")
        if wi in heading_ids:
            return False, f"moved heading-type work item '{wi}' into a document"
    return True, "no heading moved into a document"


def check_no_resolve_reply(
    trajectory: Trajectory, params: dict[str, Any]
) -> CheckResult:
    """update_document_comment must not target a reply comment."""
    reply_ids = set(params.get("reply_comment_ids", []))
    for call in _calls(trajectory, "update_document_comment"):
        cid = call.get("args", {}).get("comment_id")
        if cid in reply_ids:
            return False, f"patched reply comment '{cid}' (only root comments allowed)"
    return True, "no reply comment patched"


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


def _document_summaries(result: object) -> set[tuple[str, str]]:
    """Extract (space_id, document_name) pairs from a list_documents result."""
    if not isinstance(result, dict):
        return set()
    items = result.get("items")
    if not isinstance(items, list):
        return set()
    pairs: set[tuple[str, str]] = set()
    for item in items:
        if isinstance(item, dict):
            space = item.get("space_id")
            doc = item.get("document_name")
            if isinstance(space, str) and isinstance(doc, str):
                pairs.add((space, doc))
    return pairs


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


def check_list_before_create_document(
    trajectory: Trajectory, _params: dict[str, Any]
) -> CheckResult:
    """create_document must be preceded by a list_documents discovery.

    The discovery's response is also inspected: if the (space_id,
    document_name) the agent is about to create already appears in the
    listed pairs, the agent ignored the duplicate warning and the check
    fails. "Called list_documents" alone passes the buggy "list then
    create anyway" path; this guards against it.
    """
    names = _names(trajectory)
    if "create_document" not in names:
        return True, "no document created"
    first_create = names.index("create_document")
    create_args = trajectory[first_create].get("args", {}) or {}

    prior = trajectory[:first_create]
    existing: set[tuple[str, str]] = set()
    list_calls = 0
    for call in prior:
        if call.get("name") != "list_documents":
            continue
        list_calls += 1
        existing |= _document_summaries(call.get("result"))

    if list_calls == 0:
        return False, "created a document without first listing existing documents"

    space = create_args.get("space_id")
    doc = create_args.get("document_name")
    if isinstance(space, str) and isinstance(doc, str) and (space, doc) in existing:
        return False, (
            f"created document '{doc}' in space '{space}' even though it "
            f"already appeared in the prior list_documents response"
        )
    return True, "documents listed before create and target name is unique"


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
    "no_move_heading": check_no_move_heading,
    "no_resolve_reply": check_no_resolve_reply,
    "enum_before_create": check_enum_before_create,
    "list_before_create_document": check_list_before_create_document,
}

# Applied to every case in addition to its named check.
GLOBAL_CHECKS: list[Callable[[Trajectory, dict[str, Any]], CheckResult]] = [
    check_update_document_ids,
]
