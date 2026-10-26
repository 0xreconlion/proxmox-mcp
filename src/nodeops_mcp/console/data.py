"""Fixed read-only console collection over the existing NodeOps service boundary."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from itertools import islice
from typing import Protocol

from nodeops_mcp.config import NodeOpsConfig
from nodeops_mcp.schemas import BoundedQuery, ResultEnvelope
from nodeops_mcp.service import NodeOpsService

MAX_DETAIL_ITEMS = 32
MAX_DETAIL_TEXT = 256


def _safe_value(value: object, *, depth: int = 0) -> object:
    if depth > 3:
        return "bounded"
    if value is None or isinstance(value, bool | int | float):
        return value
    if isinstance(value, str):
        return value[:MAX_DETAIL_TEXT]
    if isinstance(value, list | tuple):
        return [_safe_value(item, depth=depth + 1) for item in value[:MAX_DETAIL_ITEMS]]
    if isinstance(value, Mapping):
        result: dict[str, object] = {}
        for key, item in islice(value.items(), MAX_DETAIL_ITEMS):
            if isinstance(key, str):
                result[key[:64]] = _safe_value(item, depth=depth + 1)
        return result
    return "unsupported"


@dataclass(frozen=True, slots=True)
class ConsoleObservation:
    category: str
    target: str
    state: str
    source: str
    observed_at: str
    details: Mapping[str, object]

    def to_mapping(self) -> dict[str, object]:
        return {
            "category": self.category,
            "target": self.target,
            "state": self.state,
            "source": self.source,
            "observed_at": self.observed_at,
            "details": _safe_value(self.details),
        }


@dataclass(frozen=True, slots=True)
class ConsoleSnapshot:
    generated_at: datetime
    mode: str
    observations: tuple[ConsoleObservation, ...]

    def to_mapping(self) -> dict[str, object]:
        return {
            "generated_at": self.generated_at.isoformat(),
            "mode": self.mode,
            "observations": [item.to_mapping() for item in self.observations],
        }


class ConsoleDataSource(Protocol):
    def collect(self) -> ConsoleSnapshot: ...


class LiveConsoleDataSource:
    """Collect the fixed inventory without accepting any target from the browser."""

    def __init__(self, service: NodeOpsService, config: NodeOpsConfig) -> None:
        self._service = service
        self._config = config

    @staticmethod
    def _observation(category: str, target: str, result: ResultEnvelope) -> ConsoleObservation:
        state = result.status.value
        return ConsoleObservation(
            category,
            target,
            state,
            result.source,
            result.observed_at,
            result.data,
        )

    def collect(self) -> ConsoleSnapshot:
        node = min(self._config.node_allowlist)
        observations = [
            self._observation("node", node, self._service.get_node_health(node)),
            self._observation("guests", node, self._service.list_guests(limit=50)),
        ]
        for storage in sorted(self._config.storage_allowlist):
            observations.append(
                self._observation(
                    "storage",
                    storage,
                    self._service.get_storage_status(node=node, storage=storage),
                )
            )
        query = BoundedQuery(limit=20, lookback_minutes=10_080)
        observations.extend(
            (
                self._observation(
                    "tasks", node, self._service.get_recent_tasks(node=node, query=query)
                ),
                self._observation(
                    "backups", node, self._service.get_backup_status(node=node, query=query)
                ),
            )
        )
        return ConsoleSnapshot(datetime.now(UTC), "measurement_only", tuple(observations))
