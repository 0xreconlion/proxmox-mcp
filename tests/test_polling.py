import asyncio
from datetime import UTC, datetime, timedelta

import pytest

from proxmox_mcp.monitoring.polling import PollCoordinator, PollLimits, PollStatus


class Clock:
    def __init__(self) -> None:
        self.now = datetime(2026, 10, 25, 5, 0, tzinfo=UTC)
        self.ticks = 0

    def wall(self) -> datetime:
        current = self.now
        self.now += timedelta(milliseconds=10)
        return current

    def monotonic(self) -> int:
        current = self.ticks
        self.ticks += 10_000_000
        return current


def coordinator(*, timeout: float = 1) -> PollCoordinator:
    clock = Clock()
    return PollCoordinator(
        limits=PollLimits(timeout),
        clock=clock.wall,
        monotonic_clock=clock.monotonic,
    )


def test_successful_poll_returns_bounded_result() -> None:
    called = False

    async def exercise() -> None:
        nonlocal called

        async def poll() -> None:
            nonlocal called
            called = True

        result = await coordinator().run_once(poll)
        assert result.status is PollStatus.COMPLETED
        assert result.reason == "poll_completed"
        assert result.elapsed_ms == 10

    asyncio.run(exercise())
    assert called


def test_timeout_is_contained() -> None:
    async def poll() -> None:
        await asyncio.sleep(0.05)

    result = asyncio.run(coordinator(timeout=0.01).run_once(poll))
    assert result.status is PollStatus.TIMED_OUT
    assert result.reason == "poll_timeout"


def test_collector_exception_text_is_not_returned() -> None:
    async def poll() -> None:
        raise RuntimeError("secret-like upstream detail")

    result = asyncio.run(coordinator().run_once(poll))
    assert result.status is PollStatus.FAILED
    assert result.reason == "poll_failed"
    assert "upstream" not in result.reason


def test_overlapping_poll_is_skipped_without_queueing() -> None:
    async def exercise() -> None:
        started = asyncio.Event()
        release = asyncio.Event()
        runner = coordinator()

        async def slow_poll() -> None:
            started.set()
            await release.wait()

        first = asyncio.create_task(runner.run_once(slow_poll))
        await started.wait()
        assert runner.running
        overlap = await runner.run_once(slow_poll)
        assert overlap.status is PollStatus.SKIPPED_OVERLAP
        release.set()
        assert (await first).status is PollStatus.COMPLETED
        assert not runner.running

    asyncio.run(exercise())


def test_cancellation_releases_overlap_guard() -> None:
    async def exercise() -> None:
        started = asyncio.Event()
        runner = coordinator()

        async def blocked_poll() -> None:
            started.set()
            await asyncio.Event().wait()

        task = asyncio.create_task(runner.run_once(blocked_poll))
        await started.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert not runner.running

        async def next_poll() -> None:
            return None

        assert (await runner.run_once(next_poll)).status is PollStatus.COMPLETED

    asyncio.run(exercise())


@pytest.mark.parametrize("timeout", [0, -1, 121])
def test_invalid_poll_timeout_fails_closed(timeout: float) -> None:
    with pytest.raises(ValueError, match="timeout"):
        PollLimits(timeout)
