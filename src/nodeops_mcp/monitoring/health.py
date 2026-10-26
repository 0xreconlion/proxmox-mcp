"""Deterministic health-state evaluation for bounded numeric observations."""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum


class HealthState(StrEnum):
    HEALTHY = "healthy"
    WARNING = "warning"
    CRITICAL = "critical"
    UNKNOWN = "unknown"
    STALE = "stale"


class ThresholdDirection(StrEnum):
    HIGH = "high"
    LOW = "low"


@dataclass(frozen=True, slots=True)
class ThresholdRule:
    rule_id: str
    unit: str
    direction: ThresholdDirection
    warning_threshold: float
    critical_threshold: float
    clear_threshold: float
    warning_confirmation_seconds: int
    critical_confirmation_seconds: int
    stale_after_seconds: int

    def validate(self) -> None:
        values = (
            self.warning_threshold,
            self.critical_threshold,
            self.clear_threshold,
        )
        if not self.rule_id or not self.unit or not all(math.isfinite(value) for value in values):
            raise ValueError("rule identifiers, units, and finite thresholds are required")
        durations = (
            self.warning_confirmation_seconds,
            self.critical_confirmation_seconds,
            self.stale_after_seconds,
        )
        if any(duration <= 0 for duration in durations):
            raise ValueError("confirmation and freshness budgets must be positive")
        if self.direction is ThresholdDirection.HIGH:
            if not self.clear_threshold < self.warning_threshold < self.critical_threshold:
                raise ValueError("high rules require clear < warning < critical")
        elif not self.clear_threshold > self.warning_threshold > self.critical_threshold:
            raise ValueError("low rules require clear > warning > critical")


@dataclass(frozen=True, slots=True)
class NumericObservation:
    value: float | None
    observed_at: datetime | None
    condition_seconds: int
    error_code: str | None = None


@dataclass(frozen=True, slots=True)
class HealthResult:
    rule_id: str
    state: HealthState
    value: float | None
    unit: str
    observed_at: datetime | None
    evaluated_at: datetime
    reason: str
    candidate_state: HealthState | None = None


def _at_or_beyond(value: float, threshold: float, direction: ThresholdDirection) -> bool:
    if direction is ThresholdDirection.HIGH:
        return value >= threshold
    return value <= threshold


def _is_clear(value: float, threshold: float, direction: ThresholdDirection) -> bool:
    if direction is ThresholdDirection.HIGH:
        return value < threshold
    return value > threshold


def evaluate_numeric_health(
    rule: ThresholdRule,
    observation: NumericObservation,
    *,
    evaluated_at: datetime,
    previous_state: HealthState = HealthState.HEALTHY,
) -> HealthResult:
    """Evaluate one sample without inventing health from absent or old data."""

    rule.validate()
    if observation.value is None or observation.observed_at is None:
        return HealthResult(
            rule.rule_id,
            HealthState.UNKNOWN,
            observation.value,
            rule.unit,
            observation.observed_at,
            evaluated_at,
            observation.error_code or "observation_absent",
        )
    if observation.condition_seconds < 0:
        raise ValueError("condition duration cannot be negative")
    if not math.isfinite(observation.value):
        return HealthResult(
            rule.rule_id,
            HealthState.UNKNOWN,
            None,
            rule.unit,
            observation.observed_at,
            evaluated_at,
            "observation_not_finite",
        )
    age_seconds = (evaluated_at - observation.observed_at).total_seconds()
    if age_seconds < 0:
        return HealthResult(
            rule.rule_id,
            HealthState.UNKNOWN,
            observation.value,
            rule.unit,
            observation.observed_at,
            evaluated_at,
            "observation_from_future",
        )
    if age_seconds > rule.stale_after_seconds:
        return HealthResult(
            rule.rule_id,
            HealthState.STALE,
            observation.value,
            rule.unit,
            observation.observed_at,
            evaluated_at,
            "freshness_budget_exceeded",
        )

    state = HealthState.HEALTHY
    reason = "inside_healthy_range"
    candidate_state = None
    if _at_or_beyond(observation.value, rule.critical_threshold, rule.direction):
        candidate_state = HealthState.CRITICAL
        if observation.condition_seconds >= rule.critical_confirmation_seconds:
            state = HealthState.CRITICAL
            reason = "critical_threshold_confirmed"
            candidate_state = None
        elif previous_state in {HealthState.WARNING, HealthState.CRITICAL}:
            state = previous_state
            reason = "critical_threshold_pending_previous_state_retained"
        else:
            reason = "critical_threshold_pending"
    elif _at_or_beyond(observation.value, rule.warning_threshold, rule.direction):
        candidate_state = HealthState.WARNING
        if observation.condition_seconds >= rule.warning_confirmation_seconds:
            state = HealthState.WARNING
            reason = "warning_threshold_confirmed"
            candidate_state = None
        elif previous_state in {HealthState.WARNING, HealthState.CRITICAL}:
            state = previous_state
            reason = "warning_threshold_pending_previous_state_retained"
        else:
            reason = "warning_threshold_pending"
    elif previous_state in {HealthState.WARNING, HealthState.CRITICAL} and not _is_clear(
        observation.value, rule.clear_threshold, rule.direction
    ):
        state = previous_state
        reason = "clear_hysteresis_not_met"

    return HealthResult(
        rule.rule_id,
        state,
        observation.value,
        rule.unit,
        observation.observed_at,
        evaluated_at,
        reason,
        candidate_state,
    )


INITIAL_NUMERIC_RULES = (
    ThresholdRule(
        "node_cpu_v1",
        "fraction",
        ThresholdDirection.HIGH,
        0.85,
        0.95,
        0.75,
        600,
        300,
        180,
    ),
    ThresholdRule(
        "node_memory_v1",
        "fraction",
        ThresholdDirection.HIGH,
        0.85,
        0.95,
        0.80,
        600,
        300,
        180,
    ),
    ThresholdRule(
        "node_load_v1",
        "load_per_thread",
        ThresholdDirection.HIGH,
        1.0,
        1.5,
        0.75,
        900,
        600,
        180,
    ),
    ThresholdRule(
        "storage_capacity_v1",
        "fraction",
        ThresholdDirection.HIGH,
        0.80,
        0.90,
        0.75,
        900,
        300,
        900,
    ),
    ThresholdRule(
        "nodeops_disk_v1",
        "fraction",
        ThresholdDirection.HIGH,
        0.75,
        0.85,
        0.70,
        900,
        300,
        900,
    ),
)

if len({rule.rule_id for rule in INITIAL_NUMERIC_RULES}) != len(INITIAL_NUMERIC_RULES):
    raise RuntimeError("health rule IDs must be unique")
