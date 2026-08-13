"""Fail-closed write blocks: guard that cannot validate raise instead of
letting write through (unknown ids/keys persist as silent ghosts).
"""

from __future__ import annotations

import logging

from mcp_server_polarion.core.exceptions import PolarionAuthError, PolarionError

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


def unauthorized_write_block(
    what: str, project_id: str, exc: PolarionAuthError
) -> PermissionError:
    """Mirror tool layer ``PolarionAuthError -> PermissionError``
    (fixable token scope, not backend to retry). Carry Polarion detail —
    403 also mean workflow lock, which no token fix.
    """
    logger.warning(
        "guard blocking write: not authorized to validate %s for project=%s",
        what,
        project_id,
    )
    return PermissionError(
        f"Cannot validate {what} for project '{project_id}': the validation "
        f"request was refused ({exc.message}). Refusing the write -- check "
        f"POLARION_TOKEN permissions."
    )
