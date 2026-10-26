"""Policy-enforced service layer shared by MCP transports."""

import os
from collections.abc import Callable, Mapping
from datetime import UTC, datetime
from time import monotonic_ns
from uuid import NAMESPACE_URL, uuid4, uuid5

from nodeops_mcp import fixtures
from nodeops_mcp.adapters.proxmox import (
    AdapterResult,
    HttpxProxmoxAdapter,
    ProxmoxAdapterError,
    ProxmoxReadAdapter,
)
from nodeops_mcp.config import NodeOpsConfig
from nodeops_mcp.schemas import (
    BoundedQuery,
    GuestTarget,
    ResultEnvelope,
    ResultStatus,
    ToolError,
)
from nodeops_mcp.security.authorization import AuthorizationPolicy
from nodeops_mcp.telemetry.audit import JsonAuditLogger

#: Audit target label for tools that span every allowlisted node rather than one.
_ALL_NODES = "*all-nodes*"


class NodeOpsService:
    """Authorize fixed operations before invoking the selected read-only adapter."""

    def __init__(
        self,
        *,
        adapter: ProxmoxReadAdapter,
        policy: AuthorizationPolicy,
        source: str,
        observed_at: str | Callable[[], str],
        deterministic_request_ids: bool = False,
        audit_logger: JsonAuditLogger | None = None,
    ) -> None:
        self._adapter = adapter
        self._policy = policy
        self._source = source
        self._observed_at = observed_at
        self._deterministic_request_ids = deterministic_request_ids
        self._audit = audit_logger

    @property
    def allowed_nodes(self) -> tuple[str, ...]:
        """Allowlisted node names, sorted, for generating the advertised tool schema."""

        return tuple(sorted(self._policy.allowed_nodes))

    @property
    def allowed_storage(self) -> tuple[str, ...]:
        """Allowlisted storage names, sorted, for generating the advertised tool schema."""

        return tuple(sorted(self._policy.allowed_storage))

    @staticmethod
    def _latency_ms(started_ns: int) -> int:
        return max(0, (monotonic_ns() - started_ns) // 1_000_000)

    def _require(
        self,
        tool: str,
        target: str,
        check: Callable[[], None],
        started_ns: int,
    ) -> None:
        try:
            check()
        except PermissionError:
            if self._audit is not None:
                self._audit.emit(
                    tool=tool,
                    target_id=target,
                    policy_result="deny",
                    outcome="denied",
                    latency_ms=self._latency_ms(started_ns),
                )
            raise

    def _request_id(self, tool: str, target: str) -> str:
        if self._deterministic_request_ids:
            return str(uuid5(NAMESPACE_URL, f"nodeops-lab:{tool}:{target}"))
        return str(uuid4())

    def _observed(self) -> str:
        return self._observed_at() if callable(self._observed_at) else self._observed_at

    def _result(
        self,
        tool: str,
        target: str,
        result: AdapterResult[Mapping[str, object]],
        started_ns: int,
    ) -> ResultEnvelope:
        envelope = ResultEnvelope(
            status=ResultStatus.PARTIAL if result.errors else ResultStatus.OK,
            source=self._source,
            observed_at=self._observed(),
            request_id=self._request_id(tool, target),
            data=result.value,
            errors=result.errors,
        )
        if self._audit is not None:
            self._audit.emit(
                tool=tool,
                target_id=target,
                policy_result="allow",
                outcome=envelope.status.value,
                latency_ms=self._latency_ms(started_ns),
                request_id=envelope.request_id,
                backend_status=200,
            )
        return envelope

    def _failure(
        self, tool: str, target: str, error: ProxmoxAdapterError, started_ns: int
    ) -> ResultEnvelope:
        envelope = ResultEnvelope(
            status=error.status,
            source=self._source,
            observed_at=self._observed(),
            request_id=self._request_id(tool, target),
            errors=(ToolError(error.code, error.safe_message),),
        )
        if self._audit is not None:
            self._audit.emit(
                tool=tool,
                target_id=target,
                policy_result="allow",
                outcome=envelope.status.value,
                latency_ms=self._latency_ms(started_ns),
                request_id=envelope.request_id,
                backend_status=error.backend_status,
            )
        return envelope

    def list_guests(self, *, limit: int) -> ResultEnvelope:
        tool = "list_guests"
        started_ns = monotonic_ns()
        self._require(tool, _ALL_NODES, lambda: self._policy.require_tool(tool), started_ns)
        if not 1 <= limit <= 50:
            raise ValueError("limit must be between 1 and 50")
        try:
            result = self._adapter.list_guests(limit=limit)
        except ProxmoxAdapterError as exc:
            return self._failure(tool, _ALL_NODES, exc, started_ns)
        return self._result(
            tool,
            _ALL_NODES,
            AdapterResult(
                {"guests": result.value, "count": len(result.value)}, result.errors
            ),
            started_ns,
        )

    def get_guest_status(self, target: GuestTarget) -> ResultEnvelope:
        tool = "get_guest_status"
        target_id = f"{target.guest_type.value}/{target.guest_id}"
        started_ns = monotonic_ns()
        self._require(tool, target_id, lambda: self._policy.require_tool(tool), started_ns)
        self._require(tool, target_id, lambda: self._policy.require_guest(target), started_ns)
        try:
            result = self._adapter.get_guest_status(target)
        except ProxmoxAdapterError as exc:
            return self._failure(tool, target_id, exc, started_ns)
        return self._result(tool, target_id, result, started_ns)

    def get_node_health(self, node: str) -> ResultEnvelope:
        tool = "get_node_health"
        started_ns = monotonic_ns()
        self._require(tool, node, lambda: self._policy.require_tool(tool), started_ns)
        self._require(tool, node, lambda: self._policy.require_node(node), started_ns)
        try:
            result = self._adapter.get_node_health(node)
        except ProxmoxAdapterError as exc:
            return self._failure(tool, node, exc, started_ns)
        return self._result(tool, node, result, started_ns)

    def get_storage_status(self, *, node: str, storage: str) -> ResultEnvelope:
        tool = "get_storage_status"
        target = f"{node}/{storage}"
        started_ns = monotonic_ns()
        self._require(tool, target, lambda: self._policy.require_tool(tool), started_ns)
        self._require(tool, target, lambda: self._policy.require_node(node), started_ns)
        self._require(tool, target, lambda: self._policy.require_storage(storage), started_ns)
        try:
            result = self._adapter.get_storage_status(node, storage)
        except ProxmoxAdapterError as exc:
            return self._failure(tool, target, exc, started_ns)
        return self._result(tool, target, result, started_ns)

    def get_recent_tasks(self, *, node: str, query: BoundedQuery) -> ResultEnvelope:
        tool = "get_recent_tasks"
        started_ns = monotonic_ns()
        self._require(tool, node, lambda: self._policy.require_tool(tool), started_ns)
        self._require(tool, node, lambda: self._policy.require_node(node), started_ns)
        try:
            result = self._adapter.get_recent_tasks(node, query)
        except ProxmoxAdapterError as exc:
            return self._failure(tool, node, exc, started_ns)
        return self._result(
            tool,
            node,
            AdapterResult(
                {"tasks": result.value, "count": len(result.value)}, result.errors
            ),
            started_ns,
        )

    def get_backup_status(self, *, node: str, query: BoundedQuery) -> ResultEnvelope:
        tool = "get_backup_status"
        started_ns = monotonic_ns()
        self._require(tool, node, lambda: self._policy.require_tool(tool), started_ns)
        self._require(tool, node, lambda: self._policy.require_node(node), started_ns)
        try:
            result = self._adapter.get_backup_status(node, query)
        except ProxmoxAdapterError as exc:
            return self._failure(tool, node, exc, started_ns)
        return self._result(
            tool,
            node,
            AdapterResult(
                {"backups": result.value, "count": len(result.value)}, result.errors
            ),
            started_ns,
        )


def build_simulated_service() -> NodeOpsService:
    from nodeops_mcp.adapters.proxmox import SimulatedProxmoxAdapter

    allowed_guests: set[tuple[str, int]] = set()
    for guest in fixtures.GUESTS:
        guest_type = guest["guest_type"]
        guest_id = guest["guest_id"]
        if not isinstance(guest_type, str) or not isinstance(guest_id, int):
            raise TypeError("invalid deterministic guest fixture")
        allowed_guests.add((guest_type, guest_id))

    policy = AuthorizationPolicy(
        allowed_nodes=frozenset({fixtures.NODE}),
        allowed_guests=frozenset(allowed_guests),
        allowed_storage=frozenset(fixtures.STORAGES),
    )
    return NodeOpsService(
        adapter=SimulatedProxmoxAdapter(),
        policy=policy,
        source=fixtures.SOURCE,
        observed_at=fixtures.OBSERVED_AT,
        deterministic_request_ids=True,
    )


def build_live_service(config: NodeOpsConfig) -> NodeOpsService:
    policy = AuthorizationPolicy(
        allowed_nodes=config.node_allowlist,
        allowed_guests=config.guest_allowlist,
        allowed_storage=config.storage_allowlist,
    )
    return NodeOpsService(
        adapter=HttpxProxmoxAdapter(config),
        policy=policy,
        source="proxmox-api",
        observed_at=lambda: datetime.now(UTC).isoformat().replace("+00:00", "Z"),
        audit_logger=JsonAuditLogger(
            actor_id=os.environ.get("NODEOPS_CLIENT_ID", "configured-private-client"),
            deployment_version=config.deployment_version,
        ),
    )
