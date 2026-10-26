from __future__ import annotations

import io
import json
from collections.abc import Mapping

import pytest

from nodeops_mcp import fixtures
from nodeops_mcp.adapters.proxmox import (
    AdapterResult,
    ProxmoxAdapterError,
    SimulatedProxmoxAdapter,
)
from nodeops_mcp.schemas import GuestTarget, GuestType, ResultStatus, ToolError
from nodeops_mcp.security.authorization import AuthorizationPolicy
from nodeops_mcp.service import NodeOpsService
from nodeops_mcp.telemetry.audit import FORBIDDEN_AUDIT_KEYS, JsonAuditLogger


class FailingAdapter(SimulatedProxmoxAdapter):
    def get_node_health(self, node: str) -> AdapterResult[Mapping[str, object]]:
        del node
        raise ProxmoxAdapterError(
            "backend_timeout",
            "backend read timed out",
            status=ResultStatus.UNAVAILABLE,
            backend_status=504,
        )


class PartialAdapter(SimulatedProxmoxAdapter):
    def get_node_health(self, node: str) -> AdapterResult[Mapping[str, object]]:
        return AdapterResult(
            {"node": node, "cpu": 0.1},
            (ToolError("missing_field", "backend field was absent", field="memory"),),
        )


def audited_service(stream: io.StringIO) -> NodeOpsService:
    return NodeOpsService(
        adapter=SimulatedProxmoxAdapter(),
        policy=AuthorizationPolicy(
            allowed_nodes=frozenset({fixtures.NODE}),
            allowed_guests=frozenset({("qemu", 110)}),
            allowed_storage=frozenset({"local"}),
        ),
        source="test_fixture",
        observed_at="2026-10-24T00:00:00Z",
        deterministic_request_ids=True,
        audit_logger=JsonAuditLogger(
            actor_id="approved-test-client",
            deployment_version="test-version",
            stream=stream,
        ),
    )


def test_success_audit_is_correlated_and_secret_safe() -> None:
    stream = io.StringIO()
    result = audited_service(stream).get_node_health(fixtures.NODE)
    event = json.loads(stream.getvalue())
    assert event["request_id"] == result.request_id
    assert event["policy_result"] == "allow"
    assert event["outcome"] == "ok"
    assert event["backend_status"] == 200
    assert not FORBIDDEN_AUDIT_KEYS.intersection(event)
    rendered = stream.getvalue().lower()
    assert "authorization" not in rendered
    assert "prompt" not in rendered
    assert "token" not in rendered


def test_policy_denial_is_audited_before_backend_execution() -> None:
    stream = io.StringIO()
    service = audited_service(stream)
    with pytest.raises(PermissionError):
        service.get_guest_status(GuestTarget(GuestType.LXC, 299))
    event = json.loads(stream.getvalue())
    assert event["tool"] == "get_guest_status"
    assert event["target_id"] == "lxc/299"
    assert event["policy_result"] == "deny"
    assert event["outcome"] == "denied"
    assert event["backend_status"] is None


@pytest.mark.parametrize(
    ("adapter", "expected_outcome", "expected_status"),
    [
        (FailingAdapter(), "unavailable", 504),
        (PartialAdapter(), "partial", 200),
    ],
)
def test_backend_failure_and_partial_result_are_audited(
    adapter: SimulatedProxmoxAdapter,
    expected_outcome: str,
    expected_status: int,
) -> None:
    stream = io.StringIO()
    service = NodeOpsService(
        adapter=adapter,
        policy=AuthorizationPolicy(
            allowed_nodes=frozenset({fixtures.NODE}),
            allowed_guests=frozenset({("qemu", 110)}),
            allowed_storage=frozenset({"local"}),
        ),
        source="test_fixture",
        observed_at="2026-10-24T00:00:00Z",
        deterministic_request_ids=True,
        audit_logger=JsonAuditLogger(
            actor_id="approved-test-client",
            deployment_version="test-version",
            stream=stream,
        ),
    )
    result = service.get_node_health(fixtures.NODE)
    event = json.loads(stream.getvalue())
    assert event["request_id"] == result.request_id
    assert event["outcome"] == expected_outcome
    assert event["backend_status"] == expected_status


def test_hostile_target_is_json_encoded_and_bounded_to_one_line() -> None:
    stream = io.StringIO()
    logger = JsonAuditLogger(
        actor_id="approved-test-client",
        deployment_version="test-version",
        stream=stream,
    )
    logger.emit(
        tool="get_node_health",
        target_id="hv-a\n<script>" + "x" * 500,
        policy_result="deny",
        outcome="denied",
        latency_ms=1,
    )
    rendered = stream.getvalue()
    event = json.loads(rendered)
    assert len(event["target_id"]) == 256
    assert rendered.count("\n") == 1


def test_audit_contract_rejects_unbounded_result_values() -> None:
    logger = JsonAuditLogger(
        actor_id="approved-test-client",
        deployment_version="test-version",
        stream=io.StringIO(),
    )
    with pytest.raises(ValueError, match="policy result"):
        logger.emit(
            tool="get_node_health",
            target_id="hv-a",
            policy_result="maybe",
            outcome="ok",
            latency_ms=1,
        )
    with pytest.raises(ValueError, match="outcome"):
        logger.emit(
            tool="get_node_health",
            target_id="hv-a",
            policy_result="allow",
            outcome="secret-upstream-body",
            latency_ms=1,
        )
