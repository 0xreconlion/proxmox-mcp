"""Fixed-destination Cloudflare Worker email client with no automatic retries."""

from __future__ import annotations

import json
import os
import re
import stat
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from urllib.parse import urlparse

import httpx

from nodeops_mcp.validation import SECRET_LIKE, contains_control, require_aware, require_safe_id

_TOKEN = re.compile(r"^[A-Za-z0-9_-]{32,256}$")
MAX_EVENT_BYTES = 8_192
MAX_SUMMARY_BYTES = 1_200


class NotificationState(StrEnum):
    WARNING = "warning"
    CRITICAL = "critical"
    RESOLVED = "resolved"
    TEST = "test"


class DeliveryStatus(StrEnum):
    ACCEPTED = "accepted"
    DENIED = "denied"
    REJECTED = "rejected"
    UNAVAILABLE = "unavailable"


@dataclass(frozen=True, slots=True)
class NotificationEvent:
    event_id: str
    state: NotificationState
    rule_id: str
    target_type: str
    target_id: str
    observed_at: datetime
    summary: str
    runbook_ref: str

    def __post_init__(self) -> None:
        for field, value in (
            ("event_id", self.event_id),
            ("rule_id", self.rule_id),
            ("target_type", self.target_type),
            ("target_id", self.target_id),
            ("runbook_ref", self.runbook_ref),
        ):
            require_safe_id(value, field)
        require_aware(self.observed_at, "observed_at")
        if not self.summary.strip() or len(self.summary.encode("utf-8")) > MAX_SUMMARY_BYTES:
            raise ValueError("summary must be non-empty and at most 1200 UTF-8 bytes")
        if contains_control(self.summary) or SECRET_LIKE.search(self.summary):
            raise ValueError("summary contains control, URL, or secret-like text")

    def to_payload(self) -> dict[str, str]:
        observed_at = self.observed_at.astimezone(UTC).isoformat().replace("+00:00", "Z")
        return {
            "event_id": self.event_id,
            "state": self.state.value,
            "rule_id": self.rule_id,
            "target_type": self.target_type,
            "target_id": self.target_id,
            "observed_at": observed_at,
            "summary": self.summary,
            "runbook_ref": self.runbook_ref,
        }


@dataclass(frozen=True, slots=True)
class EmailWorkerConfig:
    worker_origin: str
    token_file: Path
    connect_timeout_seconds: float
    read_timeout_seconds: float
    max_response_bytes: int = 4_096

    def validate(self) -> None:
        parsed = urlparse(self.worker_origin)
        if parsed.scheme != "https" or not parsed.hostname:
            raise ValueError("email Worker origin must be one fixed HTTPS origin")
        if parsed.username or parsed.password or parsed.query or parsed.fragment:
            raise ValueError("email Worker origin cannot contain credentials, query, or fragment")
        if parsed.path not in ("", "/"):
            raise ValueError("email Worker origin cannot contain a path")
        if not self.token_file.is_absolute():
            raise ValueError("email Worker token path must be absolute")
        if not 0 < self.connect_timeout_seconds <= 30:
            raise ValueError("email Worker connect timeout must be between 0 and 30 seconds")
        if not 0 < self.read_timeout_seconds <= 30:
            raise ValueError("email Worker read timeout must be between 0 and 30 seconds")
        if not 256 <= self.max_response_bytes <= 16_384:
            raise ValueError("email Worker response limit must be between 256 and 16384 bytes")


@dataclass(frozen=True, slots=True)
class DeliveryResult:
    event_id: str
    status: DeliveryStatus
    attempted_at: datetime
    reason: str


class CloudflareEmailWorkerClient:
    """Submit one structured event to the fixed mailer without ambiguous retries."""

    def __init__(self, config: EmailWorkerConfig) -> None:
        config.validate()
        self._config = config
        self._endpoint = f"{config.worker_origin.rstrip('/')}/deliver"
        self._timeout = httpx.Timeout(
            connect=config.connect_timeout_seconds,
            read=config.read_timeout_seconds,
            write=config.read_timeout_seconds,
            pool=config.connect_timeout_seconds,
        )

    def _token(self) -> str:
        path = self._config.token_file
        try:
            metadata = path.lstat()
        except OSError as error:
            raise ValueError("email Worker token file is absent") from error
        if not stat.S_ISREG(metadata.st_mode) or metadata.st_uid != os.geteuid():
            raise ValueError("email Worker token file is absent")
        if stat.S_IMODE(metadata.st_mode) & 0o077:
            raise ValueError("email Worker token file must not grant group or other access")
        token = path.read_text(encoding="utf-8").strip()
        if not _TOKEN.fullmatch(token):
            raise ValueError("email Worker token has invalid format")
        return token

    def send(self, event: NotificationEvent) -> DeliveryResult:
        attempted_at = datetime.now(UTC)
        try:
            token = self._token()
            body = json.dumps(event.to_payload(), separators=(",", ":")).encode("utf-8")
            if len(body) > MAX_EVENT_BYTES:
                raise ValueError("notification event exceeded the reviewed request limit")
            with httpx.Client(
                timeout=self._timeout,
                follow_redirects=False,
            ) as client, client.stream(
                "POST",
                self._endpoint,
                headers={
                    "Authorization": f"Bearer {token}",
                    "Content-Type": "application/json",
                },
                content=body,
            ) as response:
                total = 0
                for chunk in response.iter_bytes():
                    total += len(chunk)
                    if total > self._config.max_response_bytes:
                        return DeliveryResult(
                            event.event_id,
                            DeliveryStatus.UNAVAILABLE,
                            attempted_at,
                            "response_too_large",
                        )
                if response.status_code == 204:
                    status = DeliveryStatus.ACCEPTED
                    reason = "worker_accepted"
                elif response.status_code in {401, 403}:
                    status = DeliveryStatus.DENIED
                    reason = "worker_denied"
                elif response.status_code == 400:
                    status = DeliveryStatus.REJECTED
                    reason = "worker_rejected"
                else:
                    status = DeliveryStatus.UNAVAILABLE
                    reason = "worker_unavailable"
        except (httpx.HTTPError, OSError, ValueError):
            status = DeliveryStatus.UNAVAILABLE
            reason = "delivery_unavailable"
        return DeliveryResult(event.event_id, status, attempted_at, reason)
