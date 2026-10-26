"""Replayable delivery ledger and intent-before-send notification dispatch."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import Protocol

from nodeops_mcp.monitoring.incidents import IncidentKey
from nodeops_mcp.notifications.email import (
    DeliveryResult,
    DeliveryStatus,
    NotificationEvent,
)
from nodeops_mcp.validation import SAFE_KEY

JOURNAL_SCHEMA_VERSION = 1
MAX_JOURNAL_EVENT_BYTES = 2_048


class DeliveryPhase(StrEnum):
    INTENT = "intent"
    OUTCOME = "outcome"


class DispatchState(StrEnum):
    ACCEPTED = "accepted"
    DENIED = "denied"
    REJECTED = "rejected"
    UNAVAILABLE = "unavailable"
    SUPPRESSED_ALREADY_ATTEMPTED = "suppressed_already_attempted"


class NotificationClient(Protocol):
    def send(self, event: NotificationEvent) -> DeliveryResult: ...


@dataclass(frozen=True, slots=True)
class DeliveryJournalEvent:
    event_id: str
    delivery_id: str
    phase: DeliveryPhase
    incident_key: str
    notification_event_id: str
    recorded_at: datetime
    status: DeliveryStatus | None = None
    reason: str | None = None

    def __post_init__(self) -> None:
        for field, value in (
            ("event_id", self.event_id),
            ("delivery_id", self.delivery_id),
            ("incident_key", self.incident_key),
            ("notification_event_id", self.notification_event_id),
        ):
            if not SAFE_KEY.fullmatch(value):
                raise ValueError(f"{field} must be a bounded safe identifier")
        if self.recorded_at.tzinfo is None or self.recorded_at.utcoffset() is None:
            raise ValueError("recorded_at must be timezone-aware")
        if self.phase is DeliveryPhase.INTENT:
            if self.status is not None or self.reason is not None:
                raise ValueError("delivery intent cannot contain an outcome")
        elif self.status is None or self.reason is None or not SAFE_KEY.fullmatch(self.reason):
            raise ValueError("delivery outcome requires a finite safe status and reason")

    def encode(self) -> str:
        payload = {
            "schema": JOURNAL_SCHEMA_VERSION,
            "event_id": self.event_id,
            "delivery_id": self.delivery_id,
            "phase": self.phase.value,
            "incident_key": self.incident_key,
            "notification_event_id": self.notification_event_id,
            "recorded_at": self.recorded_at.isoformat(),
            "status": self.status.value if self.status is not None else None,
            "reason": self.reason,
        }
        encoded = json.dumps(payload, separators=(",", ":"), sort_keys=True)
        if len(encoded.encode("utf-8")) > MAX_JOURNAL_EVENT_BYTES:
            raise ValueError("delivery journal event exceeded its reviewed limit")
        return encoded

    @classmethod
    def decode(cls, encoded: str) -> DeliveryJournalEvent:
        if len(encoded.encode("utf-8")) > MAX_JOURNAL_EVENT_BYTES:
            raise ValueError("delivery journal event exceeded its reviewed limit")
        try:
            payload = json.loads(encoded)
        except json.JSONDecodeError as error:
            raise ValueError("delivery journal event is not valid JSON") from error
        expected = {
            "schema",
            "event_id",
            "delivery_id",
            "phase",
            "incident_key",
            "notification_event_id",
            "recorded_at",
            "status",
            "reason",
        }
        if not isinstance(payload, dict) or set(payload) != expected:
            raise ValueError("delivery journal event has an invalid schema")
        if payload["schema"] != JOURNAL_SCHEMA_VERSION:
            raise ValueError("delivery journal schema version is unsupported")
        try:
            phase = DeliveryPhase(payload["phase"])
            status = DeliveryStatus(payload["status"]) if payload["status"] is not None else None
            recorded_at = datetime.fromisoformat(payload["recorded_at"])
            return cls(
                event_id=payload["event_id"],
                delivery_id=payload["delivery_id"],
                phase=phase,
                incident_key=payload["incident_key"],
                notification_event_id=payload["notification_event_id"],
                recorded_at=recorded_at,
                status=status,
                reason=payload["reason"],
            )
        except (TypeError, ValueError, KeyError) as error:
            raise ValueError("delivery journal event contains invalid values") from error


@dataclass(frozen=True, slots=True)
class DeliveryLedgerEntry:
    intent: DeliveryJournalEvent
    outcome: DeliveryJournalEvent | None = None


class DeliveryLedger:
    """Rebuild delivery attempts from bounded structured journal records."""

    def __init__(self) -> None:
        self._entries: dict[str, DeliveryLedgerEntry] = {}
        self._events: dict[str, DeliveryJournalEvent] = {}

    @property
    def entries(self) -> dict[str, DeliveryLedgerEntry]:
        return dict(self._entries)

    def attempted(self, delivery_id: str) -> bool:
        return delivery_id in self._entries

    def apply(self, event: DeliveryJournalEvent) -> None:
        previous_event = self._events.get(event.event_id)
        if previous_event is not None:
            if previous_event != event:
                raise ValueError("conflicting duplicate delivery journal event")
            return
        entry = self._entries.get(event.delivery_id)
        if event.phase is DeliveryPhase.INTENT:
            if entry is not None:
                raise ValueError("delivery has more than one intent")
            self._entries[event.delivery_id] = DeliveryLedgerEntry(event)
        else:
            if entry is None:
                raise ValueError("delivery outcome has no durable intent")
            if entry.outcome is not None:
                raise ValueError("delivery has more than one outcome")
            if (
                event.incident_key != entry.intent.incident_key
                or event.notification_event_id != entry.intent.notification_event_id
                or event.recorded_at < entry.intent.recorded_at
            ):
                raise ValueError("delivery outcome does not match its intent")
            self._entries[event.delivery_id] = DeliveryLedgerEntry(entry.intent, event)
        self._events[event.event_id] = event

    @classmethod
    def replay(cls, encoded_events: Iterable[str], *, max_events: int = 100_000) -> DeliveryLedger:
        if not 1 <= max_events <= 1_000_000:
            raise ValueError("delivery replay bound is invalid")
        ledger = cls()
        for index, encoded in enumerate(encoded_events, start=1):
            if index > max_events:
                raise ValueError("delivery replay exceeded its event bound")
            ledger.apply(DeliveryJournalEvent.decode(encoded))
        return ledger


@dataclass(frozen=True, slots=True)
class DispatchResult:
    delivery_id: str
    state: DispatchState
    reason: str


def delivery_id_for(event: NotificationEvent, incident_key: IncidentKey) -> str:
    payload = {
        "incident_key": incident_key.dedupe_key,
        "notification": event.to_payload(),
    }
    digest = hashlib.sha256(
        json.dumps(payload, separators=(",", ":"), sort_keys=True).encode("utf-8")
    ).hexdigest()
    return f"delivery-{digest[:32]}"


class JournaledNotificationDispatcher:
    """Persist intent before one non-retried send and persist its finite outcome."""

    def __init__(
        self,
        *,
        client: NotificationClient,
        journal_write: Callable[[str], None],
        ledger: DeliveryLedger,
    ) -> None:
        self._client = client
        self._journal_write = journal_write
        self._ledger = ledger

    def dispatch(
        self,
        event: NotificationEvent,
        incident_key: IncidentKey,
        *,
        recorded_at: datetime,
    ) -> DispatchResult:
        delivery_id = delivery_id_for(event, incident_key)
        if self._ledger.attempted(delivery_id):
            return DispatchResult(
                delivery_id,
                DispatchState.SUPPRESSED_ALREADY_ATTEMPTED,
                "durable_attempt_already_exists",
            )
        intent = DeliveryJournalEvent(
            event_id=f"{delivery_id}:intent",
            delivery_id=delivery_id,
            phase=DeliveryPhase.INTENT,
            incident_key=incident_key.dedupe_key,
            notification_event_id=event.event_id,
            recorded_at=recorded_at,
        )
        self._journal_write(intent.encode())
        self._ledger.apply(intent)

        result = self._client.send(event)
        outcome = DeliveryJournalEvent(
            event_id=f"{delivery_id}:outcome",
            delivery_id=delivery_id,
            phase=DeliveryPhase.OUTCOME,
            incident_key=incident_key.dedupe_key,
            notification_event_id=event.event_id,
            recorded_at=result.attempted_at,
            status=result.status,
            reason=result.reason,
        )
        self._journal_write(outcome.encode())
        self._ledger.apply(outcome)
        return DispatchResult(delivery_id, DispatchState(result.status.value), result.reason)
