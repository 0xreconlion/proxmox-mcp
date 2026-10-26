"""Transport-independent Proxmox MCP input and output contracts."""

from collections.abc import Mapping
from dataclasses import dataclass, field
from enum import StrEnum


class ResultStatus(StrEnum):
    OK = "ok"
    PARTIAL = "partial"
    UNAVAILABLE = "unavailable"
    DENIED = "denied"


class GuestType(StrEnum):
    QEMU = "qemu"
    LXC = "lxc"


@dataclass(frozen=True, slots=True)
class ToolError:
    code: str
    message: str
    field: str | None = None


@dataclass(frozen=True, slots=True)
class ResultEnvelope:
    status: ResultStatus
    source: str
    observed_at: str
    request_id: str
    data: Mapping[str, object] = field(default_factory=dict)
    errors: tuple[ToolError, ...] = ()


@dataclass(frozen=True, slots=True)
class GuestTarget:
    guest_type: GuestType
    guest_id: int

    def __post_init__(self) -> None:
        if self.guest_id <= 0:
            raise ValueError("guest_id must be positive")


@dataclass(frozen=True, slots=True)
class BoundedQuery:
    limit: int
    lookback_minutes: int

    def __post_init__(self) -> None:
        if not 1 <= self.limit <= 50:
            raise ValueError("limit must be between 1 and 50")
        if not 1 <= self.lookback_minutes <= 43_200:
            raise ValueError("lookback_minutes must be between 1 and 43200")


def envelope_to_dict(envelope: ResultEnvelope) -> dict[str, object]:
    """Convert the frozen internal envelope to an MCP structured-output mapping."""

    return {
        "status": envelope.status.value,
        "source": envelope.source,
        "observed_at": envelope.observed_at,
        "request_id": envelope.request_id,
        "data": dict(envelope.data),
        "errors": [
            {"code": error.code, "message": error.message, "field": error.field}
            for error in envelope.errors
        ],
    }
