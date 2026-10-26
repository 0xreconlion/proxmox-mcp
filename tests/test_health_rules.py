from datetime import UTC, datetime, timedelta

import pytest

from proxmox_mcp.monitoring.health import (
    HealthState,
    NumericObservation,
    ThresholdDirection,
    ThresholdRule,
    evaluate_numeric_health,
)

NOW = datetime(2026, 10, 24, 23, 0, tzinfo=UTC)
RULE = ThresholdRule(
    rule_id="storage_capacity_v1",
    unit="fraction",
    direction=ThresholdDirection.HIGH,
    warning_threshold=0.80,
    critical_threshold=0.90,
    clear_threshold=0.75,
    warning_confirmation_seconds=900,
    critical_confirmation_seconds=300,
    stale_after_seconds=900,
)


def observation(value: float | None, seconds: int = 0, age: int = 0) -> NumericObservation:
    return NumericObservation(value, NOW - timedelta(seconds=age), seconds)


def test_missing_and_old_observations_never_become_healthy() -> None:
    missing = evaluate_numeric_health(
        RULE,
        NumericObservation(None, None, 0, "backend_unavailable"),
        evaluated_at=NOW,
    )
    stale = evaluate_numeric_health(RULE, observation(0.10, age=901), evaluated_at=NOW)
    assert missing.state is HealthState.UNKNOWN
    assert missing.reason == "backend_unavailable"
    assert stale.state is HealthState.STALE


def test_thresholds_require_confirmation() -> None:
    pending = evaluate_numeric_health(RULE, observation(0.92, seconds=299), evaluated_at=NOW)
    confirmed = evaluate_numeric_health(RULE, observation(0.92, seconds=300), evaluated_at=NOW)
    assert pending.state is HealthState.HEALTHY
    assert pending.reason == "critical_threshold_pending"
    assert pending.candidate_state is HealthState.CRITICAL
    assert confirmed.state is HealthState.CRITICAL
    assert confirmed.candidate_state is None


def test_hysteresis_retains_incident_until_clear_boundary() -> None:
    retained = evaluate_numeric_health(
        RULE,
        observation(0.77),
        evaluated_at=NOW,
        previous_state=HealthState.WARNING,
    )
    cleared = evaluate_numeric_health(
        RULE,
        observation(0.70),
        evaluated_at=NOW,
        previous_state=HealthState.WARNING,
    )
    assert retained.state is HealthState.WARNING
    assert retained.reason == "clear_hysteresis_not_met"
    assert cleared.state is HealthState.HEALTHY


@pytest.mark.parametrize("value", [float("nan"), float("inf"), -float("inf")])
def test_non_finite_values_are_unknown(value: float) -> None:
    result = evaluate_numeric_health(RULE, observation(value), evaluated_at=NOW)
    assert result.state is HealthState.UNKNOWN
    assert result.value is None


def test_rule_validation_rejects_reversed_thresholds() -> None:
    invalid = ThresholdRule(
        "invalid",
        "fraction",
        ThresholdDirection.HIGH,
        warning_threshold=0.90,
        critical_threshold=0.80,
        clear_threshold=0.70,
        warning_confirmation_seconds=1,
        critical_confirmation_seconds=1,
        stale_after_seconds=1,
    )
    with pytest.raises(ValueError, match="clear < warning < critical"):
        invalid.validate()
