"""Fail-closed write blocks: a guard that cannot validate raises instead of
letting the write through (unknown ids/keys persist as silent ghosts).
"""

from __future__ import annotations

import logging

from mcp_server_polarion.core.exceptions import PolarionError

logger = logging.getLogger("mcp_server_polarion.tools._shared.guard._errors")


def unreachable_write_block(
    what: str, project_id: str, exc: PolarionError
) -> RuntimeError:
    logger.warning(
        "guard blocking write: could not validate %s for project=%s (%s)",
        what,
        project_id,
        exc.message,
    )
    return RuntimeError(
        f"Cannot validate {what} for project '{project_id}': validation request "
        f"failed ({exc.message}). Refusing the write -- unknown ids/keys persist "
        f"as silent ghosts (invisible to UI/Lucene, never error). Retry once "
        f"Polarion is reachable."
    )


def unauthorized_write_block(what: str, project_id: str) -> PermissionError:
    """Mirrors the tool layer's ``PolarionAuthError -> PermissionError``
    (fixable token scope, not a backend to retry).
    """
    logger.warning(
        "guard blocking write: not authorized to validate %s for project=%s",
        what,
        project_id,
    )
    return PermissionError(
        f"Cannot validate {what} for project '{project_id}': POLARION_TOKEN lacks "
        f"permission for the validation request. Refusing the write -- check the "
        f"token's permissions."
    )
