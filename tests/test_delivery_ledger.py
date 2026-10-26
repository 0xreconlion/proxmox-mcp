from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from nodeops_mcp.monitoring.incidents import IncidentKey
from nodeops_mcp.notifications import (
    DeliveryJournalEvent,
    DeliveryLedger,
    DeliveryPhase,
    DeliveryResult,
    DeliveryStatus,
    DispatchState,
    JournaledNotificationDispatcher,
    NotificationEvent,
    NotificationState,
    delivery_id_for,
)

NOW = datetime(2026, 10, 25, 17, 0, tzinfo=UTC)
INCIDENT = IncidentKey("backup_failure_v1", "qemu", "110")


def notification() -> NotificationEvent:
    return NotificationEvent(
        event_id="incident-01-critical",
        state=NotificationState.CRITICAL,
        rule_id=INCIDENT.rule_id,
        target_type=INCIDENT.target_type,
        target_id=INCIDENT.target_id,
        observed_at=NOW,
        summary="Two consecutive bounded backup outcomes failed.",
        runbook_ref="runbook:backup-failure",
    )


class Client:
    def __init__(self, status: DeliveryStatus = DeliveryStatus.ACCEPTED) -> None:
        self.status = status
        self.calls = 0
        self.intent_was_written = False

    def send(self, event: NotificationEvent) -> DeliveryResult:
        assert event == notification()
        assert self.intent_was_written
        self.calls += 1
        return DeliveryResult(event.event_id, self.status, NOW + timedelta(seconds=1), "worker_accepted")


def test_dispatch_writes_intent_before_one_send_and_replays_outcome() -> None:
    ledger = DeliveryLedger()
    lines: list[str] = []
    client = Client()

    def write(line: str) -> None:
        lines.append(line)
        if len(lines) == 1:
            client.intent_was_written = True

    dispatcher = JournaledNotificationDispatcher(client=client, journal_write=write, ledger=ledger)
    result = dispatcher.dispatch(notification(), INCIDENT, recorded_at=NOW)
    assert result.state is DispatchState.ACCEPTED
    assert client.calls == 1
    assert len(lines) == 2

    replayed = DeliveryLedger.replay(lines)
    entry = replayed.entries[result.delivery_id]
    assert entry.intent.phase is DeliveryPhase.INTENT
    assert entry.outcome is not None
    assert entry.outcome.status is DeliveryStatus.ACCEPTED
    assert "summary" not in lines[0] and "failed" not in lines[0]


def test_existing_intent_suppresses_ambiguous_retry_after_restart() -> None:
    event = notification()
    identifier = delivery_id_for(event, INCIDENT)
    intent = DeliveryJournalEvent(
        f"{identifier}:intent",
        identifier,
        DeliveryPhase.INTENT,
        INCIDENT.dedupe_key,
        event.event_id,
        NOW,
    )
    ledger = DeliveryLedger.replay([intent.encode()])
    client = Client()
    result = JournaledNotificationDispatcher(
        client=client,
        journal_write=lambda line: pytest.fail(f"unexpected write: {line}"),
        ledger=ledger,
    ).dispatch(event, INCIDENT, recorded_at=NOW + timedelta(minutes=1))
    assert result.state is DispatchState.SUPPRESSED_ALREADY_ATTEMPTED
    assert client.calls == 0


def test_duplicate_replay_is_idempotent_but_conflict_fails_closed() -> None:
    event = notification()
    identifier = delivery_id_for(event, INCIDENT)
    intent = DeliveryJournalEvent(
        f"{identifier}:intent",
        identifier,
        DeliveryPhase.INTENT,
        INCIDENT.dedupe_key,
        event.event_id,
        NOW,
    )
    ledger = DeliveryLedger.replay([intent.encode(), intent.encode()])
    assert len(ledger.entries) == 1

    conflicting = DeliveryJournalEvent(
        intent.event_id,
        identifier,
        DeliveryPhase.INTENT,
        INCIDENT.dedupe_key,
        event.event_id,
        NOW + timedelta(seconds=1),
    )
    with pytest.raises(ValueError, match="conflicting"):
        DeliveryLedger.replay([intent.encode(), conflicting.encode()])


def test_outcome_without_intent_or_with_changed_identity_fails_closed() -> None:
    event = notification()
    identifier = delivery_id_for(event, INCIDENT)
    outcome = DeliveryJournalEvent(
        f"{identifier}:outcome",
        identifier,
        DeliveryPhase.OUTCOME,
        INCIDENT.dedupe_key,
        event.event_id,
        NOW,
        DeliveryStatus.ACCEPTED,
        "worker_accepted",
    )
    with pytest.raises(ValueError, match="no durable intent"):
        DeliveryLedger.replay([outcome.encode()])


def test_outcome_journal_failure_leaves_durable_intent_and_prevents_resend() -> None:
    ledger = DeliveryLedger()
    client = Client()
    writes = 0

    def write(line: str) -> None:
        nonlocal writes
        writes += 1
        if writes == 1:
            client.intent_was_written = True
            return
        raise OSError("simulated journal failure")

    dispatcher = JournaledNotificationDispatcher(client=client, journal_write=write, ledger=ledger)
    with pytest.raises(OSError, match="journal"):
        dispatcher.dispatch(notification(), INCIDENT, recorded_at=NOW)
    assert client.calls == 1
    assert ledger.attempted(delivery_id_for(notification(), INCIDENT))
    retry = dispatcher.dispatch(notification(), INCIDENT, recorded_at=NOW + timedelta(minutes=5))
    assert retry.state is DispatchState.SUPPRESSED_ALREADY_ATTEMPTED
    assert client.calls == 1


def test_journal_schema_and_replay_are_bounded() -> None:
    with pytest.raises(ValueError, match="replay bound"):
        DeliveryLedger.replay([], max_events=0)
    with pytest.raises(ValueError, match="reviewed limit"):
        DeliveryJournalEvent.decode("x" * 2_049)
    with pytest.raises(ValueError, match="invalid schema"):
        DeliveryJournalEvent.decode('{"schema":1}')
