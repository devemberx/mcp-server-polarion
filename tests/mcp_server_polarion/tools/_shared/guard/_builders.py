"""Polarion response builders shared by the guard test modules."""

from __future__ import annotations


def enum_response(ids: list[str]) -> dict[str, object]:
    """``getAvailableOptions`` response, one option per id."""
    return {
        "data": [{"id": i, "name": i} for i in ids],
        "meta": {"totalCount": len(ids)},
    }


def project_enum_response(enum_name: str, ids: list[str]) -> dict[str, object]:
    """Single-enumeration response: ``data`` = dict, options nested under."""
    return {
        "data": {
            "type": "enumerations",
            "id": enum_name,
            "attributes": {"options": [{"id": i, "name": i} for i in ids]},
        }
    }


def workitems_response(project_id: str, short_ids: list[str]) -> dict[str, object]:
    """JSON:API workitems list response (ids = ``project/short``)."""
    return {
        "data": [{"type": "workitems", "id": f"{project_id}/{i}"} for i in short_ids],
        "meta": {"totalCount": len(short_ids)},
    }


def attachments_response(
    short_ids: list[str], *, meta: bool = True
) -> dict[str, object]:
    """Document/work-item attachments list response (``@basic`` fieldset).
    Live: ``meta.totalCount`` absent on a normal (non-overshoot) page --
    ``meta=False`` mirrors that.
    """
    response: dict[str, object] = {
        "data": [
            {"type": "attachments", "id": i, "attributes": {"id": i}} for i in short_ids
        ]
    }
    if meta:
        response["meta"] = {"totalCount": len(short_ids)}
    return response
