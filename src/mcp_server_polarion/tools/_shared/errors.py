"""Single mapping of ``PolarionAuthError`` to ``PermissionError``.

Polarion answer 403 with one message for two unrelated causes: token scope,
and workflow state locking the resource (document past ``draft``). Server
detail alone say "limited permissions" for both, so surface it verbatim AND —
where caller know document coordinate — name the document status.
"""

from __future__ import annotations

import logging

from mcp_server_polarion.core.client import PolarionClient
from mcp_server_polarion.core.exceptions import PolarionAuthError, PolarionError
from mcp_server_polarion.tools._shared.helpers import encode_path_segment, safe_str

logger = logging.getLogger("mcp_server_polarion.tools._shared.errors")


def auth_error(
    action: str, exc: PolarionAuthError, *, document_hint: str = ""
) -> PermissionError:
    """``PermissionError`` carrying Polarion detail verbatim.

    Token advice only without *document_hint* — hint mean cause already
    pinned, and token swap cannot lift workflow lock.
    """
    if document_hint:
        return PermissionError(f"Cannot {action} -- {exc.message}{document_hint}")
    return PermissionError(
        f"Cannot {action} -- {exc.message} If the detail does not explain the "
        "block, check POLARION_TOKEN permissions."
    )


async def document_status_hint(
    client: PolarionClient,
    project_id: str,
    space_id: str,
    document_name: str,
) -> str:
    """Status sentence for *document*, empty when unresolvable.

    Best-effort: run on auth-failure path only, so any error here swallow —
    masking caller's 403 with lookup noise leave model worse off. Which status
    lock write = deployment workflow config, not API-readable, so word it
    "can block", never assert.
    """
    path = (
        f"/projects/{encode_path_segment(project_id)}"
        f"/spaces/{encode_path_segment(space_id)}"
        f"/documents/{encode_path_segment(document_name)}"
    )
    try:
        response = await client.get(path, params={"fields[documents]": "status"})
    except PolarionError as exc:
        logger.debug("document status hint unavailable for %s: %s", path, exc.message)
        return ""
    data = response.get("data", {})
    attributes = data.get("attributes", {}) if isinstance(data, dict) else {}
    status = (
        safe_str(attributes.get("status", "")) if isinstance(attributes, dict) else ""
    )
    if not status:
        return ""
    return (
        f" Target document '{space_id}/{document_name}' status is '{status}'; "
        "a document workflow can block writes in that status -- check the "
        "document in Polarion."
    )
