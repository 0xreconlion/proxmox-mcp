"""Bounded non-overlapping poll orchestration with no configured live collector."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from time import monotonic_ns


class PollStatus(StrEnum):
    COMPLETED = "completed"
    TIMED_OUT = "timed_out"
    FAILED = "failed"
    SKIPPED_OVERLAP = "skipped_overlap"


@dataclass(frozen=True, slots=True)
class PollLimits:
    timeout_seconds: float

    def __post_init__(self) -> None:
        if not 0 < self.timeout_seconds <= 120:
            raise ValueError("poll timeout must be between 0 and 120 seconds")


@dataclass(frozen=True, slots=True)
class PollResult:
    status: PollStatus
    started_at: datetime
    finished_at: datetime
    elapsed_ms: int
    reason: str

    def __post_init__(self) -> None:
        for field, value in (
            ("started_at", self.started_at),
            ("finished_at", self.finished_at),
        ):
            if value.tzinfo is None or value.utcoffset() is None:
                raise ValueError(f"{field} must be timezone-aware")
        if self.finished_at < self.started_at:
            raise ValueError("poll finish cannot precede start")
        if self.elapsed_ms < 0:
            raise ValueError("poll elapsed time cannot be negative")
        if not self.reason or len(self.reason) > 64:
            raise ValueError("poll reason must be present and bounded")


class PollCoordinator:
    """Run one caller-supplied poll at a time without scheduling or network authority."""

    def __init__(
        self,
        *,
        limits: PollLimits,
        clock: Callable[[], datetime] | None = None,
        monotonic_clock: Callable[[], int] = monotonic_ns,
    ) -> None:
        self._limits = limits
        self._clock = clock or (lambda: datetime.now(UTC))
        self._monotonic_clock = monotonic_clock
        self._running = False

    @property
    def running(self) -> bool:
        return self._running

    async def run_once(self, poll: Callable[[], Awaitable[None]]) -> PollResult:
        """Run a bounded poll or return a safe overlap result without queueing it."""

        started_at = self._clock()
        started_ns = self._monotonic_clock()
        if self._running:
            return PollResult(
                PollStatus.SKIPPED_OVERLAP,
                started_at,
                started_at,
                0,
                "previous_poll_still_running",
            )

        self._running = True
        status = PollStatus.COMPLETED
        reason = "poll_completed"
        try:
            async with asyncio.timeout(self._limits.timeout_seconds):
                await poll()
        except TimeoutError:
            status = PollStatus.TIMED_OUT
            reason = "poll_timeout"
        except Exception:  # noqa: BLE001
            # Collector exceptions are an untrusted failure domain. Their messages
            # are not copied into results, logs, or evidence by this coordinator.
            status = PollStatus.FAILED
            reason = "poll_failed"
        finally:
            self._running = False

        finished_at = self._clock()
        elapsed_ms = max(0, (self._monotonic_clock() - started_ns) // 1_000_000)
        return PollResult(status, started_at, finished_at, elapsed_ms, reason)
