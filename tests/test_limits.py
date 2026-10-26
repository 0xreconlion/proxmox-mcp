from __future__ import annotations

import asyncio
from collections.abc import Callable
from typing import Any

import pytest
from starlette.types import Message, Receive, Scope, Send

from nodeops_mcp.security.limits import BoundedHttpMiddleware


def http_scope() -> Scope:
    return {
        "type": "http",
        "asgi": {"version": "3.0", "spec_version": "2.3"},
        "http_version": "1.1",
        "method": "POST",
        "scheme": "https",
        "path": "/mcp",
        "raw_path": b"/mcp",
        "query_string": b"",
        "root_path": "",
        "headers": [],
        "client": ("127.0.0.1", 1234),
        "server": ("nodeops.test", 8443),
        "state": {},
    }


async def receive() -> Message:
    return {"type": "http.request", "body": b"", "more_body": False}


async def invoke(app: Callable[[Scope, Receive, Send], Any]) -> list[Message]:
    messages: list[Message] = []

    async def send(message: Message) -> None:
        messages.append(message)

    await app(http_scope(), receive, send)
    return messages


@pytest.mark.anyio
async def test_rate_limit_is_finite_and_returns_retry_hint() -> None:
    async def downstream(scope: Scope, receive: Receive, send: Send) -> None:
        del scope, receive
        await send({"type": "http.response.start", "status": 204, "headers": []})
        await send({"type": "http.response.body", "body": b""})

    clock_value = [10.0]
    app = BoundedHttpMiddleware(
        downstream,
        max_concurrent_requests=1,
        max_requests=2,
        window_seconds=60,
        clock=lambda: clock_value[0],
    )
    assert (await invoke(app))[0]["status"] == 204
    assert (await invoke(app))[0]["status"] == 204
    rejected = await invoke(app)
    assert rejected[0]["status"] == 429
    assert (b"retry-after", b"60") in rejected[0]["headers"]
    clock_value[0] += 60
    assert (await invoke(app))[0]["status"] == 204


@pytest.mark.anyio
async def test_concurrency_limit_rejects_without_waiting() -> None:
    started = asyncio.Event()
    release = asyncio.Event()

    async def downstream(scope: Scope, receive: Receive, send: Send) -> None:
        del scope, receive
        started.set()
        await release.wait()
        await send({"type": "http.response.start", "status": 204, "headers": []})
        await send({"type": "http.response.body", "body": b""})

    app = BoundedHttpMiddleware(
        downstream,
        max_concurrent_requests=1,
        max_requests=10,
        window_seconds=60,
    )
    first = asyncio.create_task(invoke(app))
    await started.wait()
    rejected = await invoke(app)
    assert rejected[0]["status"] == 503
    release.set()
    assert (await first)[0]["status"] == 204


def test_invalid_limit_configuration_fails_closed() -> None:
    async def downstream(scope: Scope, receive: Receive, send: Send) -> None:
        del scope, receive, send

    with pytest.raises(ValueError):
        BoundedHttpMiddleware(downstream, max_concurrent_requests=0)
    with pytest.raises(ValueError):
        BoundedHttpMiddleware(downstream, window_seconds=float("inf"))
