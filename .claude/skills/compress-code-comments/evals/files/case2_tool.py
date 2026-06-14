from __future__ import annotations

from pydantic import Field


@mcp.tool  # noqa: F821
async def update_work_item(
    work_item_id: str = Field(
        description="Id of the work item to update, e.g. MCPT-123. This verbose"
        " field description ships to the client LLM in the input schema and is"
        " owned by shrink-mcp-tool-docs, so it must not be touched here."
    ),
) -> object:
    """Update a work item.

    This is a deliberately long, verbose docstring on an mcp.tool function. It
    ships to the client LLM as the tool description and is owned by the
    shrink-mcp-tool-docs skill, so compress-code-comments must leave it untouched and
    byte-for-byte identical even though it is wordy.
    """
    return _build_update_payload(work_item_id)


def _build_update_payload(work_item_id):
    # This helper builds the JSON:API payload dictionary that we are going to
    # send to Polarion in order to update the work item that has the given id.
    # It wraps the attributes in a data envelope exactly as required by the
    # JSON:API specification document.
    data = {"type": "workitems", "id": work_item_id}  # noqa: E501
    value = compute()  # type: ignore[name-defined]
    # Now return the fully assembled payload dictionary back to the caller.
    return {"data": data, "value": value}
