"""Explicit process-level HTTP limits for the private MCP endpoint."""

from __future__ import annotations

import asyncio
import math
from collections import deque
from collections.abc import Callable
from time import monotonic

from starlette.responses import PlainTextResponse
from starlette.types import ASGIApp, Receive, Scope, Send

MAX_REQUEST_BODY_BYTES = 65_536
MAX_CONCURRENT_REQUESTS = 8
RATE_LIMIT_REQUESTS = 120
RATE_LIMIT_WINDOW_SECONDS = 60.0
SESSION_IDLE_TIMEOUT_SECONDS = 300.0
MAX_SESSIONS = 16


class BoundedHttpMiddleware:
    """Reject excess HTTP work before it reaches authentication or MCP parsing.

    Proxmox MCP is an owner-only service, so a single bounded process-wide window is
    preferable to trusting source-IP or proxy headers. Lifespan and other ASGI
    scope types pass through without consuming request capacity.
    """

    def __init__(
        self,
        app: ASGIApp,
        *,
        max_concurrent_requests: int = MAX_CONCURRENT_REQUESTS,
        max_requests: int = RATE_LIMIT_REQUESTS,
        window_seconds: float = RATE_LIMIT_WINDOW_SECONDS,
        clock: Callable[[], float] = monotonic,
    ) -> None:
        if max_concurrent_requests <= 0 or max_requests <= 0:
            raise ValueError("HTTP concurrency and rate limits must be positive")
        if not math.isfinite(window_seconds) or window_seconds <= 0:
            raise ValueError("HTTP rate-limit window must be positive and finite")
        self._app = app
        self._max_concurrent_requests = max_concurrent_requests
        self._max_requests = max_requests
        self._window_seconds = window_seconds
        self._clock = clock
        self._state_lock = asyncio.Lock()
        self._request_times: deque[float] = deque()
        self._inflight = 0

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self._app(scope, receive, send)
            return

        now = self._clock()
        rejection: PlainTextResponse | None = None
        async with self._state_lock:
            while self._request_times and now - self._request_times[0] >= self._window_seconds:
                self._request_times.popleft()
            if len(self._request_times) >= self._max_requests:
                retry_after = max(
                    1,
                    math.ceil(self._window_seconds - (now - self._request_times[0])),
                )
                rejection = PlainTextResponse(
                    "request rate limit exceeded",
                    status_code=429,
                    headers={"Retry-After": str(retry_after)},
                )
            elif self._inflight >= self._max_concurrent_requests:
                rejection = PlainTextResponse(
                    "request concurrency limit exceeded",
                    status_code=503,
                    headers={"Retry-After": "1"},
                )
            else:
                self._request_times.append(now)
                self._inflight += 1

        if rejection is not None:
            await rejection(scope, receive, send)
            return

        try:
            await self._app(scope, receive, send)
        finally:
            async with self._state_lock:
                self._inflight -= 1
