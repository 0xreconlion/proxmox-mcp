"""Typed owner-console state and product-state actions with no infrastructure authority."""

from __future__ import annotations

import json
import threading
from collections.abc import Callable, Iterable
from dataclasses import dataclass, replace
from datetime import datetime, timedelta
from enum import StrEnum

from nodeops_mcp.validation import SAFE_ACTOR, SAFE_KEY, SECRET_LIKE

MAX_ACTION_BYTES = 2_048
SNOOZE_SECONDS = frozenset({900, 3_600, 14_400, 86_400})


class ConsoleIncidentLifecycle(StrEnum):
    OPEN = "open"
    ACKNOWLEDGED = "acknowledged"
    RESOLVED = "resolved"


class ConsoleActionType(StrEnum):
    ACKNOWLEDGE = "acknowledge"
    SNOOZE = "snooze"


@dataclass(frozen=True, slots=True)
class ConsoleIncident:
    incident_id: str
    rule_id: str
    target_type: str
    target_id: str
    severity: str
    lifecycle: ConsoleIncidentLifecycle
    observed_at: datetime
    summary: str
    delivery_state: str
    recommendation_state: str
    runbook_ref: str
    acknowledged_at: datetime | None = None
    snoozed_until: datetime | None = None

    def __post_init__(self) -> None:
        for field, value in (
            ("incident_id", self.incident_id),
            ("rule_id", self.rule_id),
            ("target_type", self.target_type),
            ("target_id", self.target_id),
            ("delivery_state", self.delivery_state),
            ("recommendation_state", self.recommendation_state),
            ("runbook_ref", self.runbook_ref),
        ):
            if not SAFE_KEY.fullmatch(value):
                raise ValueError(f"{field} must be a bounded safe identifier")
        if self.severity not in {"warning", "critical"}:
            raise ValueError("console incident severity is invalid")
        for time_field, time_value in (
            ("observed_at", self.observed_at),
            ("acknowledged_at", self.acknowledged_at),
            ("snoozed_until", self.snoozed_until),
        ):
            if time_value is not None and (
                time_value.tzinfo is None or time_value.utcoffset() is None
            ):
                raise ValueError(f"{time_field} must be timezone-aware")
        if not self.summary.strip() or len(self.summary.encode("utf-8")) > 1_200:
            raise ValueError("console incident summary must be present and bounded")
        if SECRET_LIKE.search(self.summary):
            raise ValueError("console incident summary contains URL or secret-like text")

    def to_mapping(self) -> dict[str, object]:
        return {
            "incident_id": self.incident_id,
            "rule_id": self.rule_id,
            "target_type": self.target_type,
            "target_id": self.target_id,
            "severity": self.severity,
            "lifecycle": self.lifecycle.value,
            "observed_at": self.observed_at.isoformat(),
            "summary": self.summary,
            "delivery_state": self.delivery_state,
            "recommendation_state": self.recommendation_state,
            "runbook_ref": self.runbook_ref,
            "acknowledged_at": (
                self.acknowledged_at.isoformat() if self.acknowledged_at else None
            ),
            "snoozed_until": self.snoozed_until.isoformat() if self.snoozed_until else None,
        }


@dataclass(frozen=True, slots=True)
class ConsoleActionEvent:
    action_id: str
    action: ConsoleActionType
    incident_id: str
    actor: str
    occurred_at: datetime
    snooze_seconds: int | None = None

    def __post_init__(self) -> None:
        for field, value in (
            ("action_id", self.action_id),
            ("incident_id", self.incident_id),
        ):
            if not SAFE_KEY.fullmatch(value):
                raise ValueError(f"{field} must be a bounded safe identifier")
        if not SAFE_ACTOR.fullmatch(self.actor):
            raise ValueError("actor must be a bounded safe identity")
        if self.occurred_at.tzinfo is None or self.occurred_at.utcoffset() is None:
            raise ValueError("console action time must be timezone-aware")
        if self.action is ConsoleActionType.SNOOZE:
            if self.snooze_seconds not in SNOOZE_SECONDS:
                raise ValueError("console snooze duration is not approved")
        elif self.snooze_seconds is not None:
            raise ValueError("acknowledgement cannot contain a snooze duration")

    def encode(self) -> str:
        rendered = json.dumps(
            {
                "schema": 1,
                "event": "nodeops_console_action",
                "action_id": self.action_id,
                "action": self.action.value,
                "incident_id": self.incident_id,
                "actor": self.actor,
                "occurred_at": self.occurred_at.isoformat(),
                "snooze_seconds": self.snooze_seconds,
            },
            separators=(",", ":"),
            sort_keys=True,
        )
        if len(rendered.encode("utf-8")) > MAX_ACTION_BYTES:
            raise ValueError("console action event exceeded its reviewed limit")
        return rendered


class ConsoleIncidentRepository:
    """Thread-safe product-state store; actions never call infrastructure."""

    def __init__(self, incidents: Iterable[ConsoleIncident] = ()) -> None:
        self._lock = threading.Lock()
        self._incidents = {incident.incident_id: incident for incident in incidents}

    def list(self) -> tuple[ConsoleIncident, ...]:
        with self._lock:
            return tuple(
                sorted(
                    self._incidents.values(),
                    key=lambda item: item.observed_at,
                    reverse=True,
                )
            )

    def apply(
        self,
        event: ConsoleActionEvent,
        *,
        journal_write: Callable[[str], None],
    ) -> ConsoleIncident:
        with self._lock:
            incident = self._incidents.get(event.incident_id)
            if incident is None:
                raise LookupError("incident is not present")
            if incident.lifecycle is ConsoleIncidentLifecycle.RESOLVED:
                raise ValueError("resolved incidents cannot be changed")
            if event.occurred_at < incident.observed_at:
                raise ValueError("console action cannot predate the incident")
            if event.action is ConsoleActionType.ACKNOWLEDGE:
                updated = replace(
                    incident,
                    lifecycle=ConsoleIncidentLifecycle.ACKNOWLEDGED,
                    acknowledged_at=event.occurred_at,
                )
            else:
                assert event.snooze_seconds is not None
                updated = replace(
                    incident,
                    snoozed_until=event.occurred_at + timedelta(seconds=event.snooze_seconds),
                )
            journal_write(event.encode())
            self._incidents[event.incident_id] = updated
            return updated
