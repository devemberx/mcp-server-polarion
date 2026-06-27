"""Host-global request pacing across MCP server processes.

Each MCP client connection spawns its own stdio server process, so the
per-process pacing in ``client.py`` does not bound the *aggregate* rate when
several clients (e.g. Copilot CLI and IDE) hit one Polarion server from one
machine. ``GlobalPacer`` adds a host-wide gate: a cross-process file lock held
across each request plus a shared wall-clock timestamp file that spaces request
*starts* to ``min_interval``. Holding the lock across the whole request (and a
write's post-delay) also extends Polarion's "no concurrent writes" rule past the
process boundary.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Final

from filelock import FileLock

logger: Final = logging.getLogger("mcp_server_polarion.core.global_pace")


class GlobalPacer:
    """Host-global request pacer backed by a file lock + shared timestamp file.

    A no-op that touches no disk when ``min_interval <= 0`` or ``lock_path`` is
    ``None`` — keeps unit tests fast and gives a clean opt-out.
    """

    def __init__(self, lock_path: str | None, min_interval: float) -> None:
        self._min_interval = min_interval
        if lock_path is not None and min_interval > 0:
            self._enabled = True
            # thread_local=False: acquire/release run on different to_thread
            # workers, so the lock must not be tied to the acquiring thread.
            self._lock: FileLock | None = FileLock(lock_path, thread_local=False)
            self._state_path: Path | None = Path(f"{lock_path}.state")
        else:
            self._enabled = False
            self._lock = None
            self._state_path = None

    @property
    def enabled(self) -> bool:
        """Whether cross-process pacing is active."""
        return self._enabled

    @asynccontextmanager
    async def hold(self) -> AsyncIterator[None]:
        """Hold the host-global lock across a request, pacing its start.

        Acquire blocks, so it runs in a thread to keep the event loop free. On a
        filesystem error pacing degrades to a no-op (worst case: per-process
        pacing only) rather than hanging the server.
        """
        if not self._enabled or self._lock is None:
            yield
            return
        try:
            await asyncio.to_thread(self._lock.acquire)
        except OSError as exc:
            logger.warning("Global pacing disabled; lock acquire failed: %s", exc)
            self._enabled = False
            yield
            return
        try:
            await self._pace()
            self._stamp()
            yield
        finally:
            await asyncio.to_thread(self._lock.release)

    async def _pace(self) -> None:
        """Sleep until ``min_interval`` since the last globally recorded start."""
        last = self._read_last_start()
        if last is None:
            return
        wait = self._min_interval - (time.time() - last)
        if wait > 0:
            await asyncio.sleep(wait)

    def _read_last_start(self) -> float | None:
        if self._state_path is None:  # pragma: no cover - set whenever enabled
            return None
        try:
            return float(self._state_path.read_text())
        except (OSError, ValueError):
            return None

    def _stamp(self) -> None:
        """Record the current wall-clock start; overwritten in place (no growth)."""
        if self._state_path is None:  # pragma: no cover - set whenever enabled
            return
        try:
            self._state_path.write_text(str(time.time()))
        except OSError as exc:
            logger.warning("Global pacing stamp failed: %s", exc)
