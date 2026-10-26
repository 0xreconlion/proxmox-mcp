"""Bounded incident snapshots for deterministic state rebuild from journald."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime

from proxmox_mcp.monitoring.incidents import (
    IncidentKey,
    IncidentLifecycle,
    IncidentRecord,
    IncidentSeverity,
)
from proxmox_mcp.validation import SAFE_ID, SAFE_KEY

JOURNAL_SCHEMA_VERSION = 1
MAX_INCIDENT_JOURNAL_BYTES = 4_096


def _timestamp(value: datetime | None) -> str | None:
    return value.isoformat() if value is not None else None


def _parse_timestamp(value: object, field: str, *, optional: bool = False) -> datetime | None:
    if value is None and optional:
        return None
    if not isinstance(value, str):
        raise TypeError(f"{field} must be an ISO-8601 string")
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as error:
        raise ValueError(f"{field} must be an ISO-8601 string") from error
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError(f"{field} must include a timezone")
    return parsed


def _record_payload(record: IncidentRecord) -> dict[str, object]:
    if not SAFE_ID.fullmatch(record.reason):
        raise ValueError("incident journal reason must be a finite safe code")
    return {
        "rule_id": record.key.rule_id,
        "target_type": record.key.target_type,
        "target_id": record.key.target_id,
        "lifecycle": record.lifecycle.value,
        "severity": record.severity.value if record.severity is not None else None,
        "opened_at": _timestamp(record.opened_at),
        "changed_at": _timestamp(record.changed_at),
        "acknowledged_at": _timestamp(record.acknowledged_at),
        "last_delivery_at": _timestamp(record.last_delivery_at),
        "last_observation_at": _timestamp(record.last_observation_at),
        "reason": record.reason,
    }


@dataclass(frozen=True, slots=True)
class IncidentJournalEvent:
    event_id: str
    recorded_at: datetime
    record: IncidentRecord

    def __post_init__(self) -> None:
        if not SAFE_KEY.fullmatch(self.event_id):
            raise ValueError("incident journal event ID must be bounded and safe")
        if self.recorded_at.tzinfo is None or self.recorded_at.utcoffset() is None:
            raise ValueError("incident journal recorded_at must be timezone-aware")
        if self.recorded_at < self.record.last_observation_at:
            raise ValueError("incident journal event cannot predate its observation")
        _record_payload(self.record)

    @classmethod
    def snapshot(cls, record: IncidentRecord, *, recorded_at: datetime) -> IncidentJournalEvent:
        canonical = json.dumps(_record_payload(record), separators=(",", ":"), sort_keys=True)
        digest = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
        return cls(f"incident-{digest[:32]}", recorded_at, record)

    def encode(self) -> str:
        payload = {
            "schema": JOURNAL_SCHEMA_VERSION,
            "event_id": self.event_id,
            "recorded_at": self.recorded_at.isoformat(),
            "record": _record_payload(self.record),
        }
        encoded = json.dumps(payload, separators=(",", ":"), sort_keys=True)
        if len(encoded.encode("utf-8")) > MAX_INCIDENT_JOURNAL_BYTES:
            raise ValueError("incident journal event exceeded its reviewed limit")
        return encoded

    @classmethod
    def decode(cls, encoded: str) -> IncidentJournalEvent:
        if len(encoded.encode("utf-8")) > MAX_INCIDENT_JOURNAL_BYTES:
            raise ValueError("incident journal event exceeded its reviewed limit")
        try:
            payload = json.loads(encoded)
        except json.JSONDecodeError as error:
            raise ValueError("incident journal event is not valid JSON") from error
        if not isinstance(payload, dict) or set(payload) != {
            "schema",
            "event_id",
            "recorded_at",
            "record",
        }:
            raise ValueError("incident journal event has an invalid schema")
        if payload["schema"] != JOURNAL_SCHEMA_VERSION or not isinstance(payload["record"], dict):
            raise ValueError("incident journal schema version or record is invalid")
        raw = payload["record"]
        if set(raw) != {
            "rule_id", "target_type", "target_id", "lifecycle", "severity", "opened_at",
            "changed_at", "acknowledged_at", "last_delivery_at", "last_observation_at", "reason",
        }:
            raise ValueError("incident journal record has an invalid schema")
        try:
            key = IncidentKey(raw["rule_id"], raw["target_type"], raw["target_id"])
            lifecycle = IncidentLifecycle(raw["lifecycle"])
            severity = IncidentSeverity(raw["severity"]) if raw["severity"] is not None else None
            opened_at = _parse_timestamp(raw["opened_at"], "opened_at", optional=True)
            changed_at = _parse_timestamp(raw["changed_at"], "changed_at")
            acknowledged_at = _parse_timestamp(raw["acknowledged_at"], "acknowledged_at", optional=True)
            last_delivery_at = _parse_timestamp(raw["last_delivery_at"], "last_delivery_at", optional=True)
            last_observation_at = _parse_timestamp(raw["last_observation_at"], "last_observation_at")
            recorded_at = _parse_timestamp(payload["recorded_at"], "recorded_at")
            if changed_at is None or last_observation_at is None or recorded_at is None:
                raise ValueError("required incident timestamps are absent")
            record = IncidentRecord(
                key, lifecycle, severity, opened_at, changed_at, acknowledged_at,
                last_delivery_at, last_observation_at, raw["reason"],
            )
            return cls(payload["event_id"], recorded_at, record)
        except (TypeError, ValueError, KeyError) as error:
            raise ValueError("incident journal event contains invalid values") from error


class IncidentReplay:
    """Rebuild latest incident state and reject conflicting or regressive history."""

    def __init__(self) -> None:
        self._records: dict[str, IncidentRecord] = {}
        self._events: dict[str, IncidentJournalEvent] = {}

    @property
    def records(self) -> dict[str, IncidentRecord]:
        return dict(self._records)

    def apply(self, event: IncidentJournalEvent) -> None:
        previous_event = self._events.get(event.event_id)
        if previous_event is not None:
            if previous_event != event:
                raise ValueError("conflicting duplicate incident journal event")
            return
        key = event.record.key.dedupe_key
        previous = self._records.get(key)
        if previous is not None:
            if event.record.last_observation_at < previous.last_observation_at:
                raise ValueError("incident journal observation regressed")
            if event.record.changed_at < previous.changed_at:
                raise ValueError("incident journal state change regressed")
            if (previous.last_delivery_at is not None and event.record.last_delivery_at is not None
                    and event.record.last_delivery_at < previous.last_delivery_at):
                raise ValueError("incident journal delivery time regressed")
        self._records[key] = event.record
        self._events[event.event_id] = event

    @classmethod
    def replay(cls, encoded_events: Iterable[str], *, max_events: int = 100_000) -> IncidentReplay:
        if not 1 <= max_events <= 1_000_000:
            raise ValueError("incident replay bound is invalid")
        replay = cls()
        for index, encoded in enumerate(encoded_events, start=1):
            if index > max_events:
                raise ValueError("incident replay exceeded its event bound")
            replay.apply(IncidentJournalEvent.decode(encoded))
        return replay
