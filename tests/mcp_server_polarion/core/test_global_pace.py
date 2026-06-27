"""``GlobalPacer`` tests: disabled no-op, shared-clock pacing, mutual exclusion."""

from __future__ import annotations

import asyncio
import time
from pathlib import Path

import pytest

from mcp_server_polarion.core.global_pace import GlobalPacer


class TestEnabled:
    """Enabled vs no-op construction."""

    def test_disabled_when_min_interval_zero(self, tmp_path: Path) -> None:
        """min_interval <= 0 disables, even with a path."""
        pacer = GlobalPacer(str(tmp_path / "pace.lock"), 0.0)
        assert pacer.enabled is False

    def test_disabled_when_path_none(self) -> None:
        """None path disables."""
        assert GlobalPacer(None, 0.33).enabled is False

    def test_enabled_with_path_and_interval(self, tmp_path: Path) -> None:
        """Path + positive interval enables."""
        assert GlobalPacer(str(tmp_path / "pace.lock"), 0.33).enabled is True


class TestDisabledNoOp:
    """Disabled pacer never touches disk."""

    async def test_hold_creates_no_files(self, tmp_path: Path) -> None:
        lock_path = tmp_path / "pace.lock"
        pacer = GlobalPacer(str(lock_path), 0.0)

        async with pacer.hold():
            pass

        assert list(tmp_path.iterdir()) == []


class TestPacing:
    """Starts spaced via the shared timestamp file."""

    async def test_second_acquirer_waits_min_interval(self, tmp_path: Path) -> None:
        """Second pacer on the same path paces from the first's start."""
        lock_path = str(tmp_path / "pace.lock")
        min_interval = 0.2
        first = GlobalPacer(lock_path, min_interval)
        second = GlobalPacer(lock_path, min_interval)

        start = time.monotonic()
        async with first.hold():
            pass
        async with second.hold():
            elapsed = time.monotonic() - start

        # 0.9 slack: sleep may wake slightly early.
        assert elapsed >= min_interval * 0.9, (
            f"second hold started {elapsed:.3f}s after the first; "
            f"expected ≥ {min_interval * 0.9:.3f}s (shared-clock pacing)."
        )

    async def test_first_acquirer_does_not_wait(self, tmp_path: Path) -> None:
        """No prior timestamp → first hold returns promptly."""
        pacer = GlobalPacer(str(tmp_path / "pace.lock"), 0.5)

        start = time.monotonic()
        async with pacer.hold():
            pass

        assert time.monotonic() - start < 0.5

    async def test_backward_clock_step_caps_wait(self, tmp_path: Path) -> None:
        """Future timestamp (backward clock step) caps wait at min_interval."""
        lock_path = tmp_path / "pace.lock"
        min_interval = 0.2
        pacer = GlobalPacer(str(lock_path), min_interval)
        # Future stamp → now - last hugely negative.
        Path(f"{lock_path}.state").write_text(str(time.time() + 3600))

        start = time.monotonic()
        async with pacer.hold():
            pass

        # Unclamped: ~3600s sleep; clamped stays near min_interval.
        assert time.monotonic() - start <= min_interval + 0.5

    async def test_state_file_stays_bounded(self, tmp_path: Path) -> None:
        """Timestamp file overwritten in place, never grows."""
        lock_path = tmp_path / "pace.lock"
        pacer = GlobalPacer(str(lock_path), 0.0001)
        state_path = Path(f"{lock_path}.state")

        for _ in range(20):
            async with pacer.hold():
                pass

        assert state_path.stat().st_size < 64


class TestMutualExclusion:
    """File lock serializes holders within and across processes."""

    async def test_holds_do_not_overlap(self, tmp_path: Path) -> None:
        """Two pacers on one path never overlap critical sections."""
        lock_path = str(tmp_path / "pace.lock")
        in_flight = 0
        max_in_flight = 0

        async def _hold(pacer: GlobalPacer) -> None:
            nonlocal in_flight, max_in_flight
            async with pacer.hold():
                in_flight += 1
                max_in_flight = max(max_in_flight, in_flight)
                await asyncio.sleep(0.05)
                in_flight -= 1

        await asyncio.gather(
            _hold(GlobalPacer(lock_path, 0.0001)),
            _hold(GlobalPacer(lock_path, 0.0001)),
        )

        assert max_in_flight == 1


class TestCrashSafety:
    """Leftover lock file must not deadlock next acquire."""

    async def test_stale_lock_file_does_not_deadlock(self, tmp_path: Path) -> None:
        lock_path = tmp_path / "pace.lock"
        lock_path.write_text("")  # pre-existing file, not an OS-held lock

        pacer = GlobalPacer(str(lock_path), 0.0001)
        async with pacer.hold():
            pass  # acquires without hanging


class TestErrorHandling:
    """Filesystem failures degrade gracefully, no hang/crash."""

    async def test_acquire_failure_degrades_to_noop(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Lock-acquire OSError disables pacing, still runs body."""
        pacer = GlobalPacer(str(tmp_path / "pace.lock"), 0.1)

        def _boom() -> None:
            raise OSError("filesystem unavailable")

        monkeypatch.setattr(pacer._lock, "acquire", _boom)

        ran = False
        async with pacer.hold():
            ran = True

        assert ran is True
        assert pacer.enabled is False

    async def test_stamp_failure_is_swallowed(self, tmp_path: Path) -> None:
        """Failed timestamp write doesn't break the held request."""
        pacer = GlobalPacer(str(tmp_path / "pace.lock"), 0.1)
        # Parent dir absent → write_text OSError, must be swallowed.
        pacer._state_path = tmp_path / "missing" / "pace.lock.state"

        async with pacer.hold():
            pass
