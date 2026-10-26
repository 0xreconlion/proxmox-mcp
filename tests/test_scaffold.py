import unittest

from proxmox_mcp.catalog import FIXED_TOOL_CATALOG, FIXED_TOOL_NAMES
from proxmox_mcp.config import ProxmoxConfig
from proxmox_mcp.schemas import BoundedQuery, GuestTarget, GuestType, ResultStatus
from proxmox_mcp.security.authorization import AuthorizationPolicy
from proxmox_mcp.server import build_server
from proxmox_mcp.telemetry.audit import FORBIDDEN_AUDIT_KEYS, AuditEvent


class CatalogTests(unittest.TestCase):
    def test_catalog_is_exact_and_finite(self) -> None:
        self.assertEqual(
            tuple(tool.name for tool in FIXED_TOOL_CATALOG),
            (
                "list_guests",
                "get_guest_status",
                "get_node_health",
                "get_storage_status",
                "get_recent_tasks",
                "get_backup_status",
            ),
        )
        self.assertEqual(len(FIXED_TOOL_NAMES), 6)

    def test_result_status_is_finite(self) -> None:
        self.assertEqual(
            {status.value for status in ResultStatus},
            {"ok", "partial", "unavailable", "denied"},
        )


class InputBoundaryTests(unittest.TestCase):
    def test_guest_id_must_be_positive(self) -> None:
        with self.assertRaises(ValueError):
            GuestTarget(GuestType.QEMU, 0)

    def test_query_bounds(self) -> None:
        BoundedQuery(limit=50, lookback_minutes=43_200)
        for limit in (0, 51):
            with self.assertRaises(ValueError):
                BoundedQuery(limit=limit, lookback_minutes=60)

    def test_config_rejects_non_https_backend(self) -> None:
        from pathlib import Path

        config = ProxmoxConfig(
            environment="test",
            deployment_version="0.0.0-scaffold",
            proxmox_base_url="http://proxmox.example.invalid",
            node_allowlist=frozenset({"example-node"}),
            guest_allowlist=frozenset(),
            storage_allowlist=frozenset(),
            token_id_file=Path("/run/secrets/id"),
            token_secret_file=Path("/run/secrets/secret"),
            proxmox_ca_file=Path("/run/secrets/ca.pem"),
            connect_timeout_seconds=1,
            read_timeout_seconds=1,
            max_request_bytes=1024,
            max_response_bytes=1024,
        )
        with self.assertRaises(ValueError):
            config.validate()

    def test_server_exposes_exact_catalog(self) -> None:
        server = build_server()
        self.assertEqual(server.name, "Proxmox MCP Lab")


class AuthorizationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.policy = AuthorizationPolicy(
            allowed_nodes=frozenset({"hv-a"}),
            allowed_guests=frozenset({("qemu", 110)}),
            allowed_storage=frozenset({"critical"}),
        )

    def test_unknown_tool_is_denied(self) -> None:
        with self.assertRaises(PermissionError):
            self.policy.require_tool("restart_guest")

    def test_unknown_target_is_denied(self) -> None:
        with self.assertRaises(PermissionError):
            self.policy.require_guest(GuestTarget(GuestType.LXC, 999))


class AuditTests(unittest.TestCase):
    def test_safe_event_has_no_forbidden_keys(self) -> None:
        event = AuditEvent(
            timestamp="2026-10-23T00:00:00Z",
            request_id="opaque-test-id",
            actor_id="approved-test-client",
            tool="get_node_health",
            target_id="hv-a",
            policy_result="allow",
            outcome="success",
            latency_ms=1,
            backend_status=200,
            deployment_version="0.0.0-scaffold",
        ).to_safe_mapping()
        self.assertFalse(FORBIDDEN_AUDIT_KEYS.intersection(event))


if __name__ == "__main__":
    unittest.main()
