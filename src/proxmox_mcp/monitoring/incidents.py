"""Pure deterministic incident lifecycle, deduplication, and cooldown decisions."""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime
from enum import StrEnum

from proxmox_mcp.monitoring.health import HealthResult, HealthState
from proxmox_mcp.validation import SAFE_ID, require_aware


class IncidentLifecycle(StrEnum):
    NORMAL = "normal"
    PENDING = "pending"
    OPEN = "open"
    ACKNOWLEDGED = "acknowledged"
    RESOLVED = "resolved"


class IncidentSeverity(StrEnum):
    WARNING = "warning"
    CRITICAL = "critical"


class DeliveryAction(StrEnum):
    NONE = "none"
    SEND_STATE_CHANGE = "send_state_change"
    SEND_REMINDER = "send_reminder"
    SUPPRESSED_MAINTENANCE = "suppressed_maintenance"


@dataclass(frozen=True, slots=True)
class IncidentKey:
    rule_id: str
    target_type: str
    target_id: str

    def __post_init__(self) -> None:
        for field, value in (
            ("rule_id", self.rule_id),
            ("target_type", self.target_type),
            ("target_id", self.target_id),
        ):
            if not SAFE_ID.fullmatch(value):
                raise ValueError(f"{field} must be a bounded safe identifier")

    @property
    def dedupe_key(self) -> str:
        return f"{self.rule_id}:{self.target_type}/{self.target_id}"


@dataclass(frozen=True, slots=True)
class IncidentPolicy:
    warning_reminder_seconds: int = 14_400
    critical_reminder_seconds: int = 3_600

    def __post_init__(self) -> None:
        if self.warning_reminder_seconds <= 0 or self.critical_reminder_seconds <= 0:
            raise ValueError("incident reminder cooldowns must be positive")

    def reminder_seconds(self, severity: IncidentSeverity) -> int:
        if severity is IncidentSeverity.CRITICAL:
            return self.critical_reminder_seconds
        return self.warning_reminder_seconds


DEFAULT_INCIDENT_POLICY = IncidentPolicy()


@dataclass(frozen=True, slots=True)
class IncidentRecord:
    key: IncidentKey
    lifecycle: IncidentLifecycle
    severity: IncidentSeverity | None
    opened_at: datetime | None
    changed_at: datetime
    acknowledged_at: datetime | None
    last_delivery_at: datetime | None
    last_observation_at: datetime
    reason: str

    def __post_init__(self) -> None:
        for field, required_time in (
            ("changed_at", self.changed_at),
            ("last_observation_at", self.last_observation_at),
        ):
            require_aware(required_time, field)
        for field, optional_time in (
            ("opened_at", self.opened_at),
            ("acknowledged_at", self.acknowledged_at),
            ("last_delivery_at", self.last_delivery_at),
        ):
            if optional_time is not None:
                require_aware(optional_time, field)
        if not self.reason or len(self.reason) > 128:
            raise ValueError("incident reason must be present and bounded")
        if self.lifecycle is IncidentLifecycle.NORMAL and self.severity is not None:
            raise ValueError("normal incidents cannot have severity")
        if self.lifecycle is IncidentLifecycle.PENDING and self.severity is None:
            raise ValueError("pending incidents require candidate severity")
        if self.lifecycle in {
            IncidentLifecycle.OPEN,
            IncidentLifecycle.ACKNOWLEDGED,
            IncidentLifecycle.RESOLVED,
        } and (self.severity is None or self.opened_at is None):
            raise ValueError("active or resolved incidents require severity and opened time")
        if self.lifecycle is IncidentLifecycle.ACKNOWLEDGED and self.acknowledged_at is None:
            raise ValueError("acknowledged incidents require acknowledgement time")


@dataclass(frozen=True, slots=True)
class IncidentDecision:
    record: IncidentRecord
    delivery_action: DeliveryAction
    delivery_reason: str


def _severity(state: HealthState | None) -> IncidentSeverity | None:
    if state is HealthState.CRITICAL:
        return IncidentSeverity.CRITICAL
    if state is HealthState.WARNING:
        return IncidentSeverity.WARNING
    return None


def _decision(
    record: IncidentRecord,
    action: DeliveryAction,
    reason: str,
    *,
    maintenance_active: bool,
) -> IncidentDecision:
    if maintenance_active and action is not DeliveryAction.NONE:
        return IncidentDecision(
            record,
            DeliveryAction.SUPPRESSED_MAINTENANCE,
            f"maintenance:{reason}",
        )
    return IncidentDecision(record, action, reason)


def _normal_record(key: IncidentKey, health: HealthResult) -> IncidentRecord:
    return IncidentRecord(
        key=key,
        lifecycle=IncidentLifecycle.NORMAL,
        severity=None,
        opened_at=None,
        changed_at=health.evaluated_at,
        acknowledged_at=None,
        last_delivery_at=None,
        last_observation_at=health.evaluated_at,
        reason=health.reason,
    )


def _reminder_action(
    record: IncidentRecord,
    *,
    evaluated_at: datetime,
    policy: IncidentPolicy,
) -> tuple[DeliveryAction, str]:
    if record.severity is None:
        return DeliveryAction.NONE, "no_active_severity"
    if record.last_delivery_at is None:
        return DeliveryAction.SEND_STATE_CHANGE, "initial_delivery_due"
    elapsed = (evaluated_at - record.last_delivery_at).total_seconds()
    if elapsed < 0:
        raise ValueError("evaluation time cannot precede the last delivery")
    if elapsed >= policy.reminder_seconds(record.severity):
        return DeliveryAction.SEND_REMINDER, "reminder_cooldown_elapsed"
    return DeliveryAction.NONE, "reminder_cooldown_active"


def transition_incident(
    key: IncidentKey,
    health: HealthResult,
    *,
    previous: IncidentRecord | None = None,
    policy: IncidentPolicy = DEFAULT_INCIDENT_POLICY,
    maintenance_active: bool = False,
) -> IncidentDecision:
    """Apply one health result without performing delivery or external mutation."""

    require_aware(health.evaluated_at, "health evaluated_at")
    if health.observed_at is not None:
        require_aware(health.observed_at, "health observed_at")
    if key.rule_id != health.rule_id:
        raise ValueError("incident key and health rule do not match")
    if previous is not None:
        if previous.key != key:
            raise ValueError("previous incident key does not match")
        if health.evaluated_at < max(previous.last_observation_at, previous.changed_at):
            raise ValueError("health observations must be monotonic")

    current = previous or _normal_record(key, health)
    confirmed_severity = _severity(health.state)
    candidate_severity = _severity(health.candidate_state)

    if confirmed_severity is not None:
        if current.lifecycle in {
            IncidentLifecycle.NORMAL,
            IncidentLifecycle.PENDING,
            IncidentLifecycle.RESOLVED,
        }:
            record = IncidentRecord(
                key,
                IncidentLifecycle.OPEN,
                confirmed_severity,
                health.evaluated_at,
                health.evaluated_at,
                None,
                current.last_delivery_at,
                health.evaluated_at,
                health.reason,
            )
            return _decision(
                record,
                DeliveryAction.SEND_STATE_CHANGE,
                "incident_opened",
                maintenance_active=maintenance_active,
            )

        if current.severity is not confirmed_severity:
            record = replace(
                current,
                lifecycle=IncidentLifecycle.OPEN,
                severity=confirmed_severity,
                changed_at=health.evaluated_at,
                acknowledged_at=None,
                last_observation_at=health.evaluated_at,
                reason=health.reason,
            )
            return _decision(
                record,
                DeliveryAction.SEND_STATE_CHANGE,
                "incident_severity_changed",
                maintenance_active=maintenance_active,
            )

        record = replace(
            current,
            last_observation_at=health.evaluated_at,
            reason=health.reason,
        )
        action, reason = _reminder_action(record, evaluated_at=health.evaluated_at, policy=policy)
        return _decision(record, action, reason, maintenance_active=maintenance_active)

    if candidate_severity is not None:
        if current.lifecycle in {IncidentLifecycle.OPEN, IncidentLifecycle.ACKNOWLEDGED}:
            record = replace(
                current,
                last_observation_at=health.evaluated_at,
                reason=health.reason,
            )
            action, reason = _reminder_action(
                record, evaluated_at=health.evaluated_at, policy=policy
            )
            return _decision(record, action, reason, maintenance_active=maintenance_active)
        record = IncidentRecord(
            key,
            IncidentLifecycle.PENDING,
            candidate_severity,
            None,
            health.evaluated_at,
            None,
            current.last_delivery_at,
            health.evaluated_at,
            health.reason,
        )
        return _decision(
            record,
            DeliveryAction.NONE,
            "confirmation_pending",
            maintenance_active=maintenance_active,
        )

    if health.state is HealthState.HEALTHY:
        if current.lifecycle in {IncidentLifecycle.OPEN, IncidentLifecycle.ACKNOWLEDGED}:
            record = replace(
                current,
                lifecycle=IncidentLifecycle.RESOLVED,
                changed_at=health.evaluated_at,
                last_observation_at=health.evaluated_at,
                reason=health.reason,
            )
            return _decision(
                record,
                DeliveryAction.SEND_STATE_CHANGE,
                "incident_resolved",
                maintenance_active=maintenance_active,
            )
        if current.lifecycle is IncidentLifecycle.PENDING:
            record = _normal_record(key, health)
            return _decision(
                record,
                DeliveryAction.NONE,
                "pending_condition_cleared",
                maintenance_active=maintenance_active,
            )
        record = replace(
            current,
            last_observation_at=health.evaluated_at,
            reason=health.reason,
        )
        return _decision(
            record,
            DeliveryAction.NONE,
            "no_incident_change",
            maintenance_active=maintenance_active,
        )

    # Unknown and stale observations remain visible in the health result but never
    # clear or downgrade incident state.
    record = replace(
        current,
        last_observation_at=health.evaluated_at,
        reason=health.reason,
    )
    if current.lifecycle in {IncidentLifecycle.OPEN, IncidentLifecycle.ACKNOWLEDGED}:
        action, reason = _reminder_action(record, evaluated_at=health.evaluated_at, policy=policy)
        return _decision(record, action, reason, maintenance_active=maintenance_active)
    return _decision(
        record,
        DeliveryAction.NONE,
        "nonhealthy_observation_did_not_clear",
        maintenance_active=maintenance_active,
    )


def acknowledge_incident(record: IncidentRecord, *, acknowledged_at: datetime) -> IncidentRecord:
    """Acknowledge an open incident without changing evidence or severity."""

    require_aware(acknowledged_at, "acknowledged_at")
    if record.lifecycle is not IncidentLifecycle.OPEN or record.opened_at is None:
        raise ValueError("only an open incident can be acknowledged")
    if acknowledged_at < record.opened_at:
        raise ValueError("acknowledgement cannot precede incident opening")
    return replace(
        record,
        lifecycle=IncidentLifecycle.ACKNOWLEDGED,
        changed_at=acknowledged_at,
        acknowledged_at=acknowledged_at,
        reason="operator_acknowledged",
    )


def mark_delivery(record: IncidentRecord, *, delivered_at: datetime) -> IncidentRecord:
    """Record a successful required-route delivery; routing itself stays external."""

    require_aware(delivered_at, "delivered_at")
    if delivered_at < record.changed_at:
        raise ValueError("delivery cannot precede the current state change")
    if record.last_delivery_at is not None and delivered_at < record.last_delivery_at:
        raise ValueError("delivery timestamps must be monotonic")
    return replace(record, last_delivery_at=delivered_at)
