"""Deterministic, provider-independent health and incident-state contracts."""

from proxmox_mcp.monitoring.health import (
    HealthResult,
    HealthState,
    NumericObservation,
    ThresholdDirection,
    ThresholdRule,
    evaluate_numeric_health,
)
from proxmox_mcp.monitoring.incidents import (
    DeliveryAction,
    IncidentDecision,
    IncidentKey,
    IncidentLifecycle,
    IncidentPolicy,
    IncidentRecord,
    IncidentSeverity,
    acknowledge_incident,
    mark_delivery,
    transition_incident,
)
from proxmox_mcp.monitoring.journal import IncidentJournalEvent, IncidentReplay
from proxmox_mcp.monitoring.polling import (
    PollCoordinator,
    PollLimits,
    PollResult,
    PollStatus,
)

__all__ = [
    "DeliveryAction",
    "HealthResult",
    "HealthState",
    "IncidentDecision",
    "IncidentJournalEvent",
    "IncidentKey",
    "IncidentLifecycle",
    "IncidentPolicy",
    "IncidentRecord",
    "IncidentReplay",
    "IncidentSeverity",
    "NumericObservation",
    "PollCoordinator",
    "PollLimits",
    "PollResult",
    "PollStatus",
    "ThresholdDirection",
    "ThresholdRule",
    "acknowledge_incident",
    "evaluate_numeric_health",
    "mark_delivery",
    "transition_incident",
]
