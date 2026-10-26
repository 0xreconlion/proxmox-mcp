from datetime import UTC, datetime, timedelta

import pytest

from nodeops_mcp.monitoring.health import HealthResult, HealthState
from nodeops_mcp.monitoring.incidents import (
    DeliveryAction,
    IncidentKey,
    IncidentLifecycle,
    IncidentPolicy,
    IncidentSeverity,
    acknowledge_incident,
    mark_delivery,
    transition_incident,
)

NOW = datetime(2026, 10, 25, 4, 0, tzinfo=UTC)
KEY = IncidentKey("storage_capacity_v1", "storage", "critical")
POLICY = IncidentPolicy(warning_reminder_seconds=60, critical_reminder_seconds=30)


def health(
    state: HealthState,
    *,
    at: datetime = NOW,
    reason: str = "fixture",
    candidate: HealthState | None = None,
) -> HealthResult:
    return HealthResult(
        rule_id=KEY.rule_id,
        state=state,
        value=0.91,
        unit="fraction",
        observed_at=at,
        evaluated_at=at,
        reason=reason,
        candidate_state=candidate,
    )


def test_pending_open_acknowledged_and_resolved_lifecycle() -> None:
    pending = transition_incident(
        KEY,
        health(HealthState.HEALTHY, candidate=HealthState.WARNING),
        policy=POLICY,
    )
    assert pending.record.lifecycle is IncidentLifecycle.PENDING
    assert pending.delivery_action is DeliveryAction.NONE

    opened_at = NOW + timedelta(seconds=60)
    opened = transition_incident(
        KEY,
        health(HealthState.WARNING, at=opened_at),
        previous=pending.record,
        policy=POLICY,
    )
    assert opened.record.lifecycle is IncidentLifecycle.OPEN
    assert opened.record.severity is IncidentSeverity.WARNING
    assert opened.delivery_action is DeliveryAction.SEND_STATE_CHANGE

    delivered = mark_delivery(opened.record, delivered_at=opened_at)
    acknowledged = acknowledge_incident(
        delivered, acknowledged_at=opened_at + timedelta(seconds=5)
    )
    assert acknowledged.lifecycle is IncidentLifecycle.ACKNOWLEDGED

    resolved_at = opened_at + timedelta(seconds=120)
    resolved = transition_incident(
        KEY,
        health(HealthState.HEALTHY, at=resolved_at),
        previous=acknowledged,
        policy=POLICY,
    )
    assert resolved.record.lifecycle is IncidentLifecycle.RESOLVED
    assert resolved.delivery_action is DeliveryAction.SEND_STATE_CHANGE


@pytest.mark.parametrize("state", [HealthState.UNKNOWN, HealthState.STALE])
def test_unknown_and_stale_never_clear_an_open_incident(state: HealthState) -> None:
    opened = transition_incident(KEY, health(HealthState.CRITICAL), policy=POLICY).record
    delivered = mark_delivery(opened, delivered_at=NOW)
    decision = transition_incident(
        KEY,
        health(state, at=NOW + timedelta(seconds=10)),
        previous=delivered,
        policy=POLICY,
    )
    assert decision.record.lifecycle is IncidentLifecycle.OPEN
    assert decision.record.severity is IncidentSeverity.CRITICAL
    assert decision.delivery_action is DeliveryAction.NONE


def test_severity_change_reopens_acknowledged_incident_and_delivers() -> None:
    warning = transition_incident(KEY, health(HealthState.WARNING), policy=POLICY).record
    warning = mark_delivery(warning, delivered_at=NOW)
    warning = acknowledge_incident(warning, acknowledged_at=NOW + timedelta(seconds=1))
    escalated = transition_incident(
        KEY,
        health(HealthState.CRITICAL, at=NOW + timedelta(seconds=5)),
        previous=warning,
        policy=POLICY,
    )
    assert escalated.record.lifecycle is IncidentLifecycle.OPEN
    assert escalated.record.severity is IncidentSeverity.CRITICAL
    assert escalated.record.acknowledged_at is None
    assert escalated.delivery_action is DeliveryAction.SEND_STATE_CHANGE


def test_reminders_respect_severity_cooldown() -> None:
    opened = transition_incident(KEY, health(HealthState.CRITICAL), policy=POLICY).record
    delivered = mark_delivery(opened, delivered_at=NOW)
    early = transition_incident(
        KEY,
        health(HealthState.CRITICAL, at=NOW + timedelta(seconds=29)),
        previous=delivered,
        policy=POLICY,
    )
    due = transition_incident(
        KEY,
        health(HealthState.CRITICAL, at=NOW + timedelta(seconds=30)),
        previous=early.record,
        policy=POLICY,
    )
    assert early.delivery_action is DeliveryAction.NONE
    assert due.delivery_action is DeliveryAction.SEND_REMINDER


def test_maintenance_suppresses_delivery_but_not_incident_state() -> None:
    decision = transition_incident(
        KEY,
        health(HealthState.CRITICAL),
        policy=POLICY,
        maintenance_active=True,
    )
    assert decision.record.lifecycle is IncidentLifecycle.OPEN
    assert decision.delivery_action is DeliveryAction.SUPPRESSED_MAINTENANCE


def test_dedupe_key_is_stable_and_bounded() -> None:
    assert KEY.dedupe_key == "storage_capacity_v1:storage/critical"
    with pytest.raises(ValueError, match="target_id"):
        IncidentKey("storage_capacity_v1", "storage", "*")


def test_mismatched_rule_and_nonmonotonic_observation_fail_closed() -> None:
    with pytest.raises(ValueError, match="do not match"):
        transition_incident(
            IncidentKey("other_rule", "storage", "critical"),
            health(HealthState.WARNING),
            policy=POLICY,
        )
    opened = transition_incident(KEY, health(HealthState.WARNING), policy=POLICY).record
    with pytest.raises(ValueError, match="monotonic"):
        transition_incident(
            KEY,
            health(HealthState.WARNING, at=NOW - timedelta(seconds=1)),
            previous=opened,
            policy=POLICY,
        )

    acknowledged = acknowledge_incident(opened, acknowledged_at=NOW + timedelta(seconds=5))
    with pytest.raises(ValueError, match="monotonic"):
        transition_incident(
            KEY,
            health(HealthState.WARNING, at=NOW + timedelta(seconds=2)),
            previous=acknowledged,
            policy=POLICY,
        )
