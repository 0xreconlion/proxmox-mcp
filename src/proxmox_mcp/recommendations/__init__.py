"""Bounded, disconnected supplemental recommendation scaffold."""

from proxmox_mcp.recommendations.contracts import (
    Confidence,
    ObservedFact,
    RecommendationDraft,
    RecommendationInference,
    RecommendationRequest,
    RecommendationResult,
    RecommendationStatus,
)
from proxmox_mcp.recommendations.service import (
    DisconnectedRecommendationProvider,
    RecommendationLimits,
    RecommendationProvider,
    RecommendationProviderUnavailable,
    RecommendationWorker,
)

__all__ = [
    "Confidence",
    "DisconnectedRecommendationProvider",
    "ObservedFact",
    "RecommendationDraft",
    "RecommendationInference",
    "RecommendationLimits",
    "RecommendationProvider",
    "RecommendationProviderUnavailable",
    "RecommendationRequest",
    "RecommendationResult",
    "RecommendationStatus",
    "RecommendationWorker",
]
