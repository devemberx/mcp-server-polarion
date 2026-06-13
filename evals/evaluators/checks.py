"""Pure trajectory checks: f(trajectory) -> (passed, reason); no LLM, no I/O,
deterministic. Scope = LLM-behavioural rules unreachable by server-side guards."""

from __future__ import annotations

import json
import re
from collections.abc import Callable
from typing import Any

Trajectory = list[dict[str, Any]]
CheckResult = tuple[bool, str]

WRITE_TOOLS: frozenset[str] = frozenset(
    {
        "create_work_items",
        "update_work_item",
        "move_work_item_to_document",
        "move_work_item_from_document",
        "create_work_item_links",
        "delete_work_item_links",
        "update_work_item_link",
        "create_document",
        "update_document",
        "create_document_comments",
        "update_document_comment",
    }
)

# Reads whose result can change after a write: a repeat is legitimate once any
# write has run in between.
STATE_READ_TOOLS: frozenset[str] = frozenset(
    {
        "get_work_item",
        "read_work_item",
        "list_work_items",
        "get_document",
        "read_document",
        "read_document_parts",
        "list_documents",
        "list_document_comments",
        "list_work_item_links",
    }
)

# Reads invariant under the agent's own writes: an identical repeat is always
# redundant.
STABLE_READ_TOOLS: frozenset[str] = frozenset(
    {
        "list_projects",
        "list_work_item_enum_options",
        "list_document_enum_options",
        "get_sql_query_recipes",
    }
)


def _names(trajectory: Trajectory) -> list[str]:
    return [c.get("name", "") for c in trajectory]


def _args(call: dict[str, Any]) -> dict[str, Any]:
    return call.get("args", {}) or {}


def _short_id(value: object) -> str:
    """Trailing segment of a possibly project-qualified id ('P/X' -> 'X')."""
    return str(value).rsplit("/", maxsplit=1)[-1]


def _errored(call: dict[str, Any]) -> bool:
    result = call.get("result")
    return isinstance(result, dict) and "error" in result


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
        n for n in ("create_work_items", "move_work_item_to_document") if n in names
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
    """Identifier tuple for matching two calls on one target; ``work_item_id``
    normalized via ``_short_id``, ``document_name`` verbatim (may contain ``/``).
    """
    args = _args(call)
    return tuple(
        _short_id(args[k])
        if k == "work_item_id" and args.get(k) is not None
        else args.get(k)
        for k in keys
    )


def check_get_before_update(
    trajectory: Trajectory, _params: dict[str, Any]
) -> CheckResult:
    """Every ``update_*`` needs an earlier matching ``get_*`` — REPLACE-list and
    partial-PATCH semantics make blind writes clobber silently.
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


def check_resolve_root_comment(
    trajectory: Trajectory, params: dict[str, Any]
) -> CheckResult:
    """Resolution must set ``resolved=True`` on an observed root
    (``params["root_ids"]``). Reply-only resolves and never-observed ids fail;
    a stray reply attempt alongside a root resolve is tolerated (server 400s it).
    """
    root_ids = {str(r) for r in params.get("root_ids", [])}
    doc_keys = ("project_id", "space_id", "document_name")
    resolved_root = False
    resolved_non_root: str | None = None
    for i, call in enumerate(trajectory):
        if call.get("name") != "update_document_comment":
            continue
        target = _target_key(call, doc_keys)
        listed = any(
            earlier.get("name") == "list_document_comments"
            and _target_key(earlier, doc_keys) == target
            for earlier in trajectory[:i]
        )
        if not listed:
            return False, (
                "updated a comment without a prior list_document_comments on "
                "the same document -- the comment id was guessed, not observed"
            )
        args = _args(call)
        comment_id = _short_id(args.get("comment_id", ""))
        if comment_id in root_ids:
            if args.get("resolved") is True:
                resolved_root = True
        else:
            resolved_non_root = comment_id
    if resolved_non_root is not None and not resolved_root:
        return False, (
            f"resolved only comment '{resolved_non_root}', which is not a "
            f"thread root (roots: {sorted(root_ids)}) -- the thread was never "
            "actually resolved"
        )
    return True, "thread resolution reached an observed root comment"


def check_preserve_hyperlinks(
    trajectory: Trajectory, params: dict[str, Any]
) -> CheckResult:
    """A ``hyperlinks`` update must carry every URI in ``params["required_uris"]``
    — Polarion REPLACES the list, omissions silently delete.
    """
    target = _short_id(params.get("work_item_id", ""))
    required = [str(u) for u in params.get("required_uris", [])]
    for call in trajectory:
        if call.get("name") != "update_work_item":
            continue
        args = _args(call)
        if _short_id(args.get("work_item_id", "")) != target:
            continue
        hyperlinks = args.get("hyperlinks")
        if not hyperlinks:
            continue
        uris = {str(h.get("uri", "")) for h in hyperlinks if isinstance(h, dict)}
        missing = [u for u in required if u not in uris]
        if missing:
            return False, (
                f"update_work_item replaced hyperlinks on {target} without "
                f"pre-existing URI(s) {missing} -- the full list must be passed"
            )
    return True, "no hyperlink update dropped a pre-existing URI"


_BODY_WRITE_TO_SOURCE: dict[str, tuple[str, str, str, tuple[str, ...]]] = {
    "update_document": (
        "home_page_content_html",
        "get_document",
        "include_homepage_content_html",
        ("project_id", "space_id", "document_name"),
    ),
    "update_work_item": (
        "description_html",
        "get_work_item",
        "include_description_html",
        ("project_id", "work_item_id"),
    ),
}


def check_round_trip_source(
    trajectory: Trajectory, _params: dict[str, Any]
) -> CheckResult:
    """Body writes must source from ``get_*(include_*_html=True)`` on the same
    target — ``read_*`` synthesis Markdown collapses Polarion anchors.
    """
    for i, call in enumerate(trajectory):
        spec = _BODY_WRITE_TO_SOURCE.get(call.get("name", ""))
        if spec is None:
            continue
        body_arg, get_name, flag_arg, id_keys = spec
        if not _args(call).get(body_arg):
            continue
        target = _target_key(call, id_keys)
        sourced = any(
            earlier.get("name") == get_name
            and _target_key(earlier, id_keys) == target
            and bool(_args(earlier).get(flag_arg))
            for earlier in trajectory[:i]
        )
        if not sourced:
            return False, (
                f"{call.get('name')}({target}) wrote {body_arg} without a prior "
                f"{get_name}({flag_arg}=True) -- body was not round-trip sourced"
            )
    return True, "every body write was round-trip sourced"


def check_no_blind_detach(
    trajectory: Trajectory, params: dict[str, Any]
) -> CheckResult:
    """``move_work_item_from_document`` must not target ``params["floating_ids"]``
    — detaching a free-floating item 400s (not idempotent).
    """
    floating = {_short_id(x) for x in params.get("floating_ids", [])}
    for call in trajectory:
        if call.get("name") != "move_work_item_from_document":
            continue
        work_item_id = _short_id(_args(call).get("work_item_id", ""))
        if work_item_id in floating:
            return False, (
                f"called move_work_item_from_document on {work_item_id}, which "
                "is not in any document -- the action 400s instead of no-opping"
            )
    return True, "no detach was issued against a free-floating item"


def check_single_bulk_create(
    trajectory: Trajectory, params: dict[str, Any]
) -> CheckResult:
    """Items creatable in one bulk call must not be split across calls.

    Counts committed ``create_work_items`` calls (``dry_run`` previews and
    guard-rejected calls excluded). ``params["max_calls"]`` defaults to 1.
    """
    max_calls = int(params.get("max_calls", 1))
    committed = [
        call
        for call in trajectory
        if call.get("name") == "create_work_items"
        and not _args(call).get("dry_run")
        and not _errored(call)
    ]
    if len(committed) > max_calls:
        return False, (
            f"split creation into {len(committed)} create_work_items calls "
            f"(max {max_calls}) -- one bulk call accepts up to 50 items"
        )
    return True, "creation used a single bulk call"


def check_direct_read(trajectory: Trajectory, params: dict[str, Any]) -> CheckResult:
    """A known-id lookup must use ``get_*``/``read_*``, not a list scan."""
    target = _short_id(params.get("work_item_id", ""))
    if "list_work_items" in _names(trajectory):
        return False, (
            f"scanned with list_work_items although the id {target} was known "
            "-- get_work_item/read_work_item resolves it directly"
        )
    direct = any(
        call.get("name") in ("get_work_item", "read_work_item")
        and _short_id(_args(call).get("work_item_id", "")) == target
        for call in trajectory
    )
    if not direct:
        return False, f"never read work item {target} directly"
    return True, "resolved the known id with a direct read"


def check_no_duplicate_reads(
    trajectory: Trajectory, _params: dict[str, Any]
) -> CheckResult:
    """No identical re-read while nothing changed: state reads reset on any write,
    stable reads (enums, recipes, projects) never legitimately repeat.
    """
    seen_state: set[tuple[str, str]] = set()
    seen_stable: set[tuple[str, str]] = set()
    for call in trajectory:
        name = call.get("name", "")
        if name in WRITE_TOOLS:
            seen_state.clear()
            continue
        key = (name, json.dumps(_args(call), sort_keys=True, default=str))
        if name in STABLE_READ_TOOLS:
            if key in seen_stable:
                return False, (
                    f"repeated identical {name} call -- its options never "
                    "change within a task; reuse the first result"
                )
            seen_stable.add(key)
        elif name in STATE_READ_TOOLS:
            if key in seen_state:
                return False, (
                    f"repeated identical {name} call with no intervening "
                    "write -- reuse the first result"
                )
            seen_state.add(key)
    return True, "no redundant identical reads"


def check_scoped_query_uses_sql(
    trajectory: Trajectory, _params: dict[str, Any]
) -> CheckResult:
    """Document scoping must use ``SQL:(...)`` or ``read_document_parts`` — Lucene
    ``module``/``module.id`` field terms match nothing (not indexed).
    """
    field_re = re.compile(r"\bmodule(?:\.\w+)?\s*:", re.IGNORECASE)
    for call in trajectory:
        if call.get("name") != "list_work_items":
            continue
        query = str(_args(call).get("query") or "")
        if field_re.search(query) and not query.lstrip().lower().startswith("sql:"):
            return False, (
                f"list_work_items used Lucene query '{query}' -- module is not "
                "indexed; use the SQL:(...) prefix or read_document_parts"
            )
    return True, "no Lucene module query issued"


REGISTRY: dict[str, Callable[[Trajectory, dict[str, Any]], CheckResult]] = {
    "readonly": check_readonly,
    "no_update_document": check_no_update_document,
    "heading_to_doc": check_heading_to_doc,
    "get_before_update": check_get_before_update,
    "resolve_root_comment": check_resolve_root_comment,
    "preserve_hyperlinks": check_preserve_hyperlinks,
    "round_trip_source": check_round_trip_source,
    "no_blind_detach": check_no_blind_detach,
    "single_bulk_create": check_single_bulk_create,
    "direct_read": check_direct_read,
    "no_duplicate_reads": check_no_duplicate_reads,
    "scoped_query_uses_sql": check_scoped_query_uses_sql,
}
