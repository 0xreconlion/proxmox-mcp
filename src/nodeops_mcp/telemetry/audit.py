"""Secret-safe structured audit events for journald/stderr collection."""

import json
import sys
from collections.abc import Mapping
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from typing import TextIO
from uuid import uuid4

FORBIDDEN_AUDIT_KEYS = frozenset(
    {
        "authorization",
        "cookie",
        "password",
        "prompt",
        "response_body",
        "token",
        "token_secret",
    }
)
ALLOWED_POLICY_RESULTS = frozenset({"allow", "deny"})
ALLOWED_OUTCOMES = frozenset({"ok", "partial", "unavailable", "denied"})


@dataclass(frozen=True, slots=True)
class AuditEvent:
    timestamp: str
    request_id: str
    actor_id: str
    tool: str
    target_id: str
    policy_result: str
    outcome: str
    latency_ms: int
    backend_status: int | None
    deployment_version: str

    def to_safe_mapping(self) -> Mapping[str, object]:
        event = asdict(self)
        if FORBIDDEN_AUDIT_KEYS.intersection(event):
            raise ValueError("forbidden audit field")
        return event


class JsonAuditLogger:
    """Emit one bounded JSON object per event; never accept prompts or headers."""

    def __init__(
        self,
        *,
        actor_id: str,
        deployment_version: str,
        stream: TextIO | None = None,
    ) -> None:
        if not actor_id or not deployment_version:
            raise ValueError("actor identity and deployment version are required")
        self._actor_id = actor_id
        self._deployment_version = deployment_version
        self._stream = stream if stream is not None else sys.stderr

    @staticmethod
    def _timestamp() -> str:
        return datetime.now(UTC).isoformat().replace("+00:00", "Z")

    def emit(
        self,
        *,
        tool: str,
        target_id: str,
        policy_result: str,
        outcome: str,
        latency_ms: int,
        request_id: str | None = None,
        backend_status: int | None = None,
    ) -> str:
        if policy_result not in ALLOWED_POLICY_RESULTS:
            raise ValueError("audit policy result is not in the finite contract")
        if outcome not in ALLOWED_OUTCOMES:
            raise ValueError("audit outcome is not in the finite contract")
        if backend_status is not None and not 100 <= backend_status <= 599:
            raise ValueError("audit backend status must be an HTTP status")
        bounded_latency = max(0, min(latency_ms, 86_400_000))
        event = AuditEvent(
            timestamp=self._timestamp(),
            request_id=request_id or str(uuid4()),
            actor_id=self._actor_id,
            tool=tool,
            target_id=target_id[:256],
            policy_result=policy_result,
            outcome=outcome,
            latency_ms=bounded_latency,
            backend_status=backend_status,
            deployment_version=self._deployment_version,
        )
        print(
            json.dumps(event.to_safe_mapping(), sort_keys=True, separators=(",", ":")),
            file=self._stream,
            flush=True,
        )
        return event.request_id
