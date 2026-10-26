"""Owner-only loopback console intended exclusively for Tailscale Serve."""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from importlib.resources import files
from typing import TextIO
from uuid import uuid4

from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import HTMLResponse, JSONResponse, PlainTextResponse, Response
from starlette.routing import Route
from starlette.types import ASGIApp

from nodeops_mcp.config import load_live_config
from nodeops_mcp.console.data import ConsoleDataSource, ConsoleSnapshot, LiveConsoleDataSource
from nodeops_mcp.console.models import (
    SNOOZE_SECONDS,
    ConsoleActionEvent,
    ConsoleActionType,
    ConsoleIncidentRepository,
)
from nodeops_mcp.security.limits import BoundedHttpMiddleware
from nodeops_mcp.service import build_live_service

MAX_CONSOLE_BODY_BYTES = 4_096
_SECURITY_HEADERS = {
    "Cache-Control": "no-store",
    "Content-Security-Policy": (
        "default-src 'none'; script-src 'self'; style-src 'self'; connect-src 'self'; "
        "base-uri 'none'; frame-ancestors 'none'; form-action 'self'"
    ),
    "Referrer-Policy": "no-referrer",
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
}


@dataclass(frozen=True, slots=True)
class ConsoleConfig:
    owner_login: str
    public_origin: str
    display_hostname: str = "NodeOps"
    trusted_proxy_hosts: frozenset[str] = frozenset({"127.0.0.1", "::1"})

    def validate(self) -> None:
        if not self.owner_login or any(character.isspace() for character in self.owner_login):
            raise ValueError("console owner login is invalid")
        if not self.public_origin.startswith("https://") or self.public_origin.endswith("/"):
            raise ValueError("console origin must be one exact HTTPS origin without trailing slash")
        if not self.display_hostname or len(self.display_hostname) > 64:
            raise ValueError("console display hostname is invalid")
        if not self.trusted_proxy_hosts or any("*" in host for host in self.trusted_proxy_hosts):
            raise ValueError("console trusted proxy hosts must be exact")


class JsonConsoleActionWriter:
    """Write one bounded safe action record for journald collection."""

    def __init__(self, stream: TextIO | None = None) -> None:
        self._stream = stream if stream is not None else sys.stderr

    def __call__(self, encoded: str) -> None:
        print(encoded, file=self._stream, flush=True)


def _headers(response: Response) -> Response:
    response.headers.update(_SECURITY_HEADERS)
    return response


def _authorized(request: Request, config: ConsoleConfig, *, require_origin: bool = False) -> bool:
    client = request.client
    if client is None or client.host not in config.trusted_proxy_hosts:
        return False
    if request.headers.get("tailscale-user-login", "").casefold() != config.owner_login.casefold():
        return False
    return not require_origin or request.headers.get("origin") == config.public_origin


async def _body_json(request: Request) -> dict[str, object]:
    media_type = request.headers.get("content-type", "").split(";", 1)[0].strip().lower()
    if media_type != "application/json":
        raise ValueError("invalid content type")
    body = await request.body()
    if len(body) > MAX_CONSOLE_BODY_BYTES:
        raise ValueError("request body too large")
    try:
        value = json.loads(body)
    except (json.JSONDecodeError, UnicodeDecodeError) as error:
        raise ValueError("invalid JSON") from error
    if not isinstance(value, dict):
        raise TypeError("request body must be an object")
    return value


def _asset(name: str) -> str:
    return files("nodeops_mcp.console.assets").joinpath(name).read_text(encoding="utf-8")


def build_console_app(
    *,
    config: ConsoleConfig,
    data_source: ConsoleDataSource,
    incidents: ConsoleIncidentRepository,
    journal_write: Callable[[str], None],
) -> ASGIApp:
    config.validate()

    async def page(request: Request) -> Response:
        if not _authorized(request, config):
            return _headers(PlainTextResponse("Forbidden", status_code=403))
        html = _asset("index.html").replace("{{HOSTNAME}}", config.display_hostname)
        return _headers(HTMLResponse(html))

    async def stylesheet(request: Request) -> Response:
        if not _authorized(request, config):
            return _headers(PlainTextResponse("Forbidden", status_code=403))
        return _headers(Response(_asset("styles.css"), media_type="text/css"))

    async def javascript(request: Request) -> Response:
        if not _authorized(request, config):
            return _headers(PlainTextResponse("Forbidden", status_code=403))
        return _headers(Response(_asset("app.js"), media_type="text/javascript"))

    async def state(request: Request) -> Response:
        if not _authorized(request, config):
            return _headers(PlainTextResponse("Forbidden", status_code=403))
        try:
            snapshot = await asyncio.to_thread(data_source.collect)
        except Exception:  # noqa: BLE001
            snapshot = ConsoleSnapshot(datetime.now(UTC), "collector_unavailable", ())
        payload = {
            "hostname": config.display_hostname,
            **snapshot.to_mapping(),
            "incidents": [incident.to_mapping() for incident in incidents.list()],
            "capability_boundary": "incident_state_only_no_infrastructure_mutation",
        }
        return _headers(JSONResponse(payload))

    async def action(request: Request) -> Response:
        if not _authorized(request, config, require_origin=True):
            return _headers(PlainTextResponse("Forbidden", status_code=403))
        incident_id = request.path_params["incident_id"]
        action_name = request.path_params["action"]
        if action_name not in {item.value for item in ConsoleActionType}:
            return _headers(PlainTextResponse("Not found", status_code=404))
        try:
            body = await _body_json(request)
            now = datetime.now(UTC)
            if action_name == ConsoleActionType.ACKNOWLEDGE.value:
                if body:
                    raise ValueError("acknowledgement body must be empty")
                event = ConsoleActionEvent(
                    f"console-{uuid4()}", ConsoleActionType.ACKNOWLEDGE,
                    incident_id, config.owner_login, now,
                )
            else:
                if set(body) != {"seconds"} or body["seconds"] not in SNOOZE_SECONDS:
                    raise ValueError("snooze duration is invalid")
                seconds = body["seconds"]
                if not isinstance(seconds, int):
                    raise ValueError("snooze duration is invalid")
                event = ConsoleActionEvent(
                    f"console-{uuid4()}", ConsoleActionType.SNOOZE,
                    incident_id, config.owner_login, now, seconds,
                )
            updated = incidents.apply(event, journal_write=journal_write)
        except LookupError:
            return _headers(PlainTextResponse("Not found", status_code=404))
        except (OSError, TypeError, ValueError):
            return _headers(PlainTextResponse("Invalid action", status_code=400))
        return _headers(JSONResponse({"incident": updated.to_mapping()}))

    app = Starlette(
        debug=False,
        routes=[
            Route("/console", page, methods=["GET"]),
            Route("/console/styles.css", stylesheet, methods=["GET"]),
            Route("/console/app.js", javascript, methods=["GET"]),
            Route("/console/api/state", state, methods=["GET"]),
            Route(
                "/console/api/incidents/{incident_id:str}/{action:str}",
                action,
                methods=["POST"],
            ),
        ],
    )
    return BoundedHttpMiddleware(
        app,
        max_concurrent_requests=4,
        max_requests=60,
        window_seconds=60,
    )


def _required(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        raise ValueError(f"required console setting is absent: {name}")
    return value


def main() -> None:
    parser = argparse.ArgumentParser(description="Run the private NodeOps owner console")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8421)
    args = parser.parse_args()
    if args.host not in {"127.0.0.1", "::1"}:
        raise ValueError("NodeOps console must bind to loopback for Tailscale Serve")
    live_config = load_live_config()
    console_config = ConsoleConfig(
        owner_login=_required("NODEOPS_CONSOLE_OWNER_LOGIN"),
        public_origin=_required("NODEOPS_CONSOLE_ORIGIN"),
        display_hostname=os.environ.get("NODEOPS_CONSOLE_HOSTNAME", "NodeOps").strip(),
    )
    app = build_console_app(
        config=console_config,
        data_source=LiveConsoleDataSource(build_live_service(live_config), live_config),
        incidents=ConsoleIncidentRepository(),
        journal_write=JsonConsoleActionWriter(),
    )
    import uvicorn

    uvicorn.run(app, host=args.host, port=args.port, log_level="info")


if __name__ == "__main__":
    main()
