"""Bounded recommendation orchestration with no configured model connection."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Protocol

from nodeops_mcp.recommendations.contracts import (
    RecommendationDraft,
    RecommendationRequest,
    RecommendationResult,
    RecommendationStatus,
)


class RecommendationProviderUnavailable(RuntimeError):
    """Raised when the optional provider is deliberately not configured."""


class RecommendationProvider(Protocol):
    """Narrow provider seam: evidence in, structured text out, no actions."""

    async def generate(self, request: RecommendationRequest) -> RecommendationDraft: ...


@dataclass(frozen=True, slots=True)
class RecommendationLimits:
    timeout_seconds: float
    max_concurrency: int
    max_output_characters: int

    def __post_init__(self) -> None:
        if not 0 < self.timeout_seconds <= 120:
            raise ValueError("recommendation timeout must be between 0 and 120 seconds")
        if not 1 <= self.max_concurrency <= 8:
            raise ValueError("recommendation concurrency must be between 1 and 8")
        if not 256 <= self.max_output_characters <= 8_192:
            raise ValueError("recommendation output limit must be between 256 and 8192")


class DisconnectedRecommendationProvider:
    """Default scaffold provider; it performs no network, model, or tool call."""

    async def generate(self, request: RecommendationRequest) -> RecommendationDraft:
        del request
        raise RecommendationProviderUnavailable("provider_not_configured")


class RecommendationWorker:
    """Contain optional provider failures without changing deterministic incident state."""

    def __init__(
        self,
        *,
        provider: RecommendationProvider,
        limits: RecommendationLimits,
    ) -> None:
        self._provider = provider
        self._limits = limits
        self._semaphore = asyncio.Semaphore(limits.max_concurrency)

    @staticmethod
    def _fallback_summary(request: RecommendationRequest) -> str:
        return (
            f"Deterministic incident {request.alert_id} remains authoritative. "
            "Use only the approved verification steps and linked manual response."
        )

    def _fallback(
        self,
        request: RecommendationRequest,
        *,
        status: RecommendationStatus,
        reason: str,
    ) -> RecommendationResult:
        return RecommendationResult(
            status=status,
            incident_id=request.incident_id,
            alert_id=request.alert_id,
            rule_id=request.rule_id,
            evidence_at=request.evidence_at,
            facts=request.facts,
            summary=self._fallback_summary(request),
            inferences=(),
            approved_verification_steps=request.approved_verification_steps,
            safe_manual_response=request.safe_manual_response,
            runbook_ref=request.runbook_ref,
            unavailable_actions=request.unavailable_actions,
            provider_id=None,
            reason=reason,
        )

    def _validate_draft(
        self,
        request: RecommendationRequest,
        draft: RecommendationDraft,
    ) -> None:
        output_characters = len(draft.summary) + sum(
            len(inference.statement) + len(inference.uncertainty) for inference in draft.inferences
        )
        if output_characters > self._limits.max_output_characters:
            raise ValueError("provider output exceeded the reviewed character limit")
        fact_ids = {fact.fact_id for fact in request.facts}
        for inference in draft.inferences:
            if not set(inference.fact_ids).issubset(fact_ids):
                raise ValueError("provider cited evidence outside the request")

    async def generate(self, request: RecommendationRequest) -> RecommendationResult:
        async with self._semaphore:
            try:
                async with asyncio.timeout(self._limits.timeout_seconds):
                    draft = await self._provider.generate(request)
                self._validate_draft(request, draft)
            except TimeoutError:
                return self._fallback(
                    request,
                    status=RecommendationStatus.TIMED_OUT,
                    reason="provider_timeout",
                )
            except RecommendationProviderUnavailable:
                return self._fallback(
                    request,
                    status=RecommendationStatus.UNAVAILABLE,
                    reason="provider_unavailable",
                )
            # The provider is an optional failure domain. Its exceptions are data-plane
            # failures and must never suppress the deterministic incident path.
            except Exception:  # noqa: BLE001
                return self._fallback(
                    request,
                    status=RecommendationStatus.DETERMINISTIC_FALLBACK,
                    reason="provider_output_rejected",
                )

        return RecommendationResult(
            status=RecommendationStatus.GENERATED,
            incident_id=request.incident_id,
            alert_id=request.alert_id,
            rule_id=request.rule_id,
            evidence_at=request.evidence_at,
            facts=request.facts,
            summary=draft.summary,
            inferences=draft.inferences,
            approved_verification_steps=request.approved_verification_steps,
            safe_manual_response=request.safe_manual_response,
            runbook_ref=request.runbook_ref,
            unavailable_actions=request.unavailable_actions,
            provider_id=draft.provider_id,
            reason="provider_output_validated",
        )
