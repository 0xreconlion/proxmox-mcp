from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest

from proxmox_mcp.monitoring.incidents import (
    IncidentKey,
    IncidentLifecycle,
    IncidentRecord,
    IncidentSeverity,
)
from proxmox_mcp.monitoring.journal import IncidentJournalEvent, IncidentReplay

NOW = datetime(2026, 10, 25, 18, 0, tzinfo=UTC)
KEY = IncidentKey("storage_capacity_v1", "storage", "critical")


def record(*, at: datetime = NOW, reason: str = "incident_opened") -> IncidentRecord:
    return IncidentRecord(
        key=KEY,
        lifecycle=IncidentLifecycle.OPEN,
        severity=IncidentSeverity.CRITICAL,
        opened_at=NOW,
        changed_at=NOW,
        acknowledged_at=None,
        last_delivery_at=None,
        last_observation_at=at,
        reason=reason,
    )


def test_snapshot_round_trip_rebuilds_latest_incident() -> None:
    opened = IncidentJournalEvent.snapshot(record(), recorded_at=NOW)
    later_record = replace(
        record(at=NOW + timedelta(minutes=1)),
        last_delivery_at=NOW + timedelta(seconds=5),
        reason="reminder_cooldown_active",
    )
    later = IncidentJournalEvent.snapshot(later_record, recorded_at=NOW + timedelta(minutes=1))
    replay = IncidentReplay.replay([opened.encode(), later.encode()])
    assert replay.records[KEY.dedupe_key] == later_record


def test_exact_duplicate_is_idempotent_but_conflicting_duplicate_fails() -> None:
    event = IncidentJournalEvent.snapshot(record(), recorded_at=NOW)
    replay = IncidentReplay.replay([event.encode(), event.encode()])
    assert len(replay.records) == 1

    conflict = IncidentJournalEvent(
        event.event_id,
        NOW + timedelta(seconds=1),
        record(at=NOW + timedelta(seconds=1)),
    )
    with pytest.raises(ValueError, match="conflicting"):
        IncidentReplay.replay([event.encode(), conflict.encode()])


def test_regressive_observation_fails_closed() -> None:
    later_record = record(at=NOW + timedelta(minutes=2))
    later = IncidentJournalEvent.snapshot(later_record, recorded_at=NOW + timedelta(minutes=2))
    earlier = IncidentJournalEvent.snapshot(record(), recorded_at=NOW + timedelta(minutes=3))
    with pytest.raises(ValueError, match="regressed"):
        IncidentReplay.replay([later.encode(), earlier.encode()])


def test_unsafe_reason_and_invalid_journal_schema_are_rejected() -> None:
    with pytest.raises(ValueError, match="safe code"):
        IncidentJournalEvent.snapshot(record(reason="token=do-not-store"), recorded_at=NOW)
    with pytest.raises(ValueError, match="invalid schema"):
        IncidentJournalEvent.decode('{"schema":1}')


def test_replay_count_and_event_size_are_bounded() -> None:
    with pytest.raises(ValueError, match="replay bound"):
        IncidentReplay.replay([], max_events=0)
    with pytest.raises(ValueError, match="reviewed limit"):
        IncidentJournalEvent.decode("x" * 4_097)
