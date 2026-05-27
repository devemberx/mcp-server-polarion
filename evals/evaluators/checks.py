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

# Block-level tags that Polarion rejects (next GET .../parts → 500) unless each
# carries a unique non-empty id. `<hN>` is included per this project's server
# config: headings added via update_document must also be anchored.
_BLOCK_TAGS: frozenset[str] = frozenset(
    {
        "h1",
        "h2",
        "h3",
        "h4",
        "h5",
        "h6",
        "p",
        "ul",
        "ol",
        "table",
        "div",
        "blockquote",
        "pre",
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


def check_enum_before_create(
    trajectory: Trajectory, _params: dict[str, Any]
) -> CheckResult:
    """create_work_item must be preceded by a list_work_item_enum_options call."""
    names = _names(trajectory)
    if "create_work_item" not in names:
        return True, "no work item created"
    first_create = names.index("create_work_item")
    if "list_work_item_enum_options" in names[:first_create]:
        return True, "enum options resolved before create"
    return False, "created a work item without first listing enum options"


def check_list_before_create_document(
    trajectory: Trajectory, _params: dict[str, Any]
) -> CheckResult:
    """create_document must be preceded by a list_documents uniqueness check."""
    names = _names(trajectory)
    if "create_document" not in names:
        return True, "no document created"
    first_create = names.index("create_document")
    if "list_documents" in names[:first_create]:
        return True, "documents listed before create"
    return False, "created a document without first listing existing documents"


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
