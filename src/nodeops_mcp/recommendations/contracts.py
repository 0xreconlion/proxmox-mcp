"""Typed, capability-free contracts for supplemental recommendations."""

from __future__ import annotations

import math
import re
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum

from nodeops_mcp.validation import require_aware, require_bounded_text, require_safe_id

Scalar = str | int | float | bool

MAX_FACTS = 32
MAX_VERIFICATION_STEPS = 12
MAX_UNAVAILABLE_ACTIONS = 16
MAX_INFERENCES = 8

_PROHIBITED_OUTPUT = re.compile(
    r"```|https?://|\b(?:sudo|curl|wget|ssh|scp|pvesh|qm|pct|systemctl)\b|"
    r"\bignore\s+(?:all\s+)?(?:previous|prior)\s+instructions\b|"
    r"\b(?:should|must|can|could|may)\s+(?:run|execute|restart|reboot|shutdown|delete|"
    r"remove|start|stop|enable|disable|suppress|acknowledge|resolve)\b|"
    r"(?:^|[.!?]\s+)(?:please\s+)?(?:run|execute|restart|reboot|shutdown|delete|"
    r"remove|start|stop|enable|disable|suppress|acknowledge|resolve)\b|"
    r"\b(?:authorization|bearer\s+|token\s*=|private key)\b",
    re.IGNORECASE | re.MULTILINE,
)


class RecommendationStatus(StrEnum):
    GENERATED = "generated"
    DETERMINISTIC_FALLBACK = "deterministic_fallback"
    TIMED_OUT = "timed_out"
    UNAVAILABLE = "unavailable"


class Confidence(StrEnum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"


def _reject_prohibited_output(value: str, field: str) -> None:
    if _PROHIBITED_OUTPUT.search(value):
        raise ValueError(f"{field} contains executable, mutating, or secret-like text")


@dataclass(frozen=True, slots=True)
class ObservedFact:
    """One bounded fact copied from deterministic incident evidence."""

    fact_id: str
    label: str
    value: Scalar
    unit: str
    source: str
    observed_at: datetime

    def __post_init__(self) -> None:
        require_safe_id(self.fact_id, "fact_id")
        require_bounded_text(self.label, "label", 128)
        require_bounded_text(self.unit, "unit", 48)
        require_safe_id(self.source, "source")
        require_aware(self.observed_at, "observed_at")
        if isinstance(self.value, str):
            require_bounded_text(self.value, "fact value", 256)
        elif isinstance(self.value, float) and not math.isfinite(self.value):
            raise ValueError("fact value must be finite")


@dataclass(frozen=True, slots=True)
class RecommendationRequest:
    """Evidence-only input; it contains no prompt, credential, or executable capability."""

    incident_id: str
    alert_id: str
    rule_id: str
    evidence_at: datetime
    facts: tuple[ObservedFact, ...]
    approved_verification_steps: tuple[str, ...]
    safe_manual_response: str
    runbook_ref: str
    unavailable_actions: tuple[str, ...]

    def __post_init__(self) -> None:
        for field, value in (
            ("incident_id", self.incident_id),
            ("alert_id", self.alert_id),
            ("rule_id", self.rule_id),
            ("runbook_ref", self.runbook_ref),
        ):
            require_safe_id(value, field)
        require_aware(self.evidence_at, "evidence_at")
        if not self.facts or len(self.facts) > MAX_FACTS:
            raise ValueError(f"facts must contain between 1 and {MAX_FACTS} items")
        fact_ids = [fact.fact_id for fact in self.facts]
        if len(set(fact_ids)) != len(fact_ids):
            raise ValueError("fact IDs must be unique")
        if (
            not self.approved_verification_steps
            or len(self.approved_verification_steps) > MAX_VERIFICATION_STEPS
        ):
            raise ValueError("approved verification steps are required and bounded")
        for step in self.approved_verification_steps:
            require_bounded_text(step, "approved verification step", 256)
        require_bounded_text(self.safe_manual_response, "safe manual response", 512)
        if not self.unavailable_actions or len(self.unavailable_actions) > MAX_UNAVAILABLE_ACTIONS:
            raise ValueError("unavailable actions are required and bounded")
        for action in self.unavailable_actions:
            require_safe_id(action, "unavailable action")


@dataclass(frozen=True, slots=True)
class RecommendationInference:
    """One labeled inference whose evidence references must resolve to request facts."""

    statement: str
    fact_ids: tuple[str, ...]
    confidence: Confidence
    uncertainty: str

    def __post_init__(self) -> None:
        require_bounded_text(self.statement, "inference statement", 600)
        _reject_prohibited_output(self.statement, "inference statement")
        if not self.fact_ids or len(self.fact_ids) > 8:
            raise ValueError("each inference must cite between 1 and 8 facts")
        for fact_id in self.fact_ids:
            require_safe_id(fact_id, "inference fact ID")
        require_bounded_text(self.uncertainty, "inference uncertainty", 400)
        _reject_prohibited_output(self.uncertainty, "inference uncertainty")


@dataclass(frozen=True, slots=True)
class RecommendationDraft:
    """Provider output before evidence and safety validation."""

    provider_id: str
    summary: str
    inferences: tuple[RecommendationInference, ...]

    def __post_init__(self) -> None:
        require_safe_id(self.provider_id, "provider_id")
        require_bounded_text(self.summary, "summary", 1_200)
        _reject_prohibited_output(self.summary, "summary")
        if not self.inferences or len(self.inferences) > MAX_INFERENCES:
            raise ValueError(f"inferences must contain between 1 and {MAX_INFERENCES} items")


@dataclass(frozen=True, slots=True)
class RecommendationResult:
    """Final supplemental result; deterministic incident state remains external."""

    status: RecommendationStatus
    incident_id: str
    alert_id: str
    rule_id: str
    evidence_at: datetime
    facts: tuple[ObservedFact, ...]
    summary: str
    inferences: tuple[RecommendationInference, ...]
    approved_verification_steps: tuple[str, ...]
    safe_manual_response: str
    runbook_ref: str
    unavailable_actions: tuple[str, ...]
    provider_id: str | None
    reason: str
