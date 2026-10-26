from __future__ import annotations

import json
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path

import pytest
from starlette.testclient import TestClient

from nodeops_mcp import fixtures
from nodeops_mcp.config import NodeOpsConfig
from nodeops_mcp.console.app import ConsoleConfig, build_console_app
from nodeops_mcp.console.data import ConsoleSnapshot, LiveConsoleDataSource
from nodeops_mcp.console.models import (
    ConsoleIncident,
    ConsoleIncidentLifecycle,
    ConsoleIncidentRepository,
)
from nodeops_mcp.service import build_simulated_service

NOW = datetime(2026, 10, 25, 12, 0, tzinfo=UTC)
OWNER = "owner@example.com"
ORIGIN = "https://nodeops.example.ts.net"
HEADERS = {"tailscale-user-login": OWNER}


class Source:
    def collect(self) -> ConsoleSnapshot:
        return ConsoleSnapshot(NOW, "measurement_only", ())


def incident() -> ConsoleIncident:
    return ConsoleIncident(
        incident_id="incident-01",
        rule_id="backup_failure_v1",
        target_type="qemu",
        target_id="110",
        severity="critical",
        lifecycle=ConsoleIncidentLifecycle.OPEN,
        observed_at=NOW,
        summary="Two consecutive bounded backup outcomes failed.",
        delivery_state="accepted",
        recommendation_state="deterministic_fallback",
        runbook_ref="runbook:backup-failure",
    )


def app(*, trusted: frozenset[str] = frozenset({"testclient"})) -> tuple[TestClient, list[str]]:
    journal: list[str] = []
    console = build_console_app(
        config=ConsoleConfig(OWNER, ORIGIN, trusted_proxy_hosts=trusted),
        data_source=Source(),
        incidents=ConsoleIncidentRepository((incident(),)),
        journal_write=journal.append,
    )
    return TestClient(console), journal


def test_console_requires_exact_tailscale_identity_and_trusted_proxy() -> None:
    client, _ = app()
    assert client.get("/console").status_code == 403
    assert client.get("/console", headers={"tailscale-user-login": "other@example.com"}).status_code == 403
    untrusted, _ = app(trusted=frozenset({"127.0.0.1"}))
    assert untrusted.get("/console", headers=HEADERS).status_code == 403


def test_console_page_state_assets_and_security_headers() -> None:
    client, _ = app()
    page = client.get("/console", headers=HEADERS)
    assert page.status_code == 200
    assert "NodeOps" in page.text
    assert page.headers["cache-control"] == "no-store"
    assert "frame-ancestors 'none'" in page.headers["content-security-policy"]

    state = client.get("/console/api/state", headers=HEADERS)
    assert state.status_code == 200
    assert state.json()["mode"] == "measurement_only"
    assert state.json()["capability_boundary"] == "incident_state_only_no_infrastructure_mutation"
    script = client.get("/console/app.js", headers=HEADERS)
    assert script.status_code == 200
    assert "textContent" in script.text
    assert "innerHTML" not in script.text


def test_acknowledge_and_snooze_require_same_origin_and_write_safe_journal() -> None:
    client, journal = app()
    missing_origin = client.post(
        "/console/api/incidents/incident-01/acknowledge",
        headers=HEADERS,
        json={},
    )
    assert missing_origin.status_code == 403

    action_headers = {**HEADERS, "origin": ORIGIN}
    acknowledged = client.post(
        "/console/api/incidents/incident-01/acknowledge",
        headers=action_headers,
        json={},
    )
    assert acknowledged.status_code == 200
    assert acknowledged.json()["incident"]["lifecycle"] == "acknowledged"
    snoozed = client.post(
        "/console/api/incidents/incident-01/snooze",
        headers=action_headers,
        json={"seconds": 3600},
    )
    assert snoozed.status_code == 200
    assert snoozed.json()["incident"]["snoozed_until"] is not None
    assert len(journal) == 2
    assert all("summary" not in line and OWNER in line for line in journal)
    assert json.loads(journal[0])["event"] == "nodeops_console_action"


@pytest.mark.parametrize(
    ("path", "body", "expected"),
    [
        ("/console/api/incidents/incident-01/snooze", {"seconds": 1}, 400),
        ("/console/api/incidents/missing/acknowledge", {}, 404),
        ("/console/api/incidents/incident-01/restart", {}, 404),
    ],
)
def test_invalid_or_infrastructure_like_actions_are_rejected(
    path: str, body: dict[str, object], expected: int
) -> None:
    client, journal = app()
    response = client.post(path, headers={**HEADERS, "origin": ORIGIN}, json=body)
    assert response.status_code == expected
    assert not journal


def test_console_incident_rejects_secret_like_or_linked_summary() -> None:
    with pytest.raises(ValueError, match="secret-like"):
        replace(incident(), summary="Open https://attacker.invalid with Bearer secret")


def test_live_console_source_uses_only_fixed_configuration(tmp_path: Path) -> None:
    config = NodeOpsConfig(
        environment="test",
        deployment_version="test",
        proxmox_base_url="https://proxmox.test",
        node_allowlist=frozenset({fixtures.NODE}),
        guest_allowlist=frozenset({("qemu", 110)}),
        storage_allowlist=frozenset({"local"}),
        token_id_file=tmp_path / "id",
        token_secret_file=tmp_path / "secret",
        proxmox_ca_file=tmp_path / "ca",
        connect_timeout_seconds=1,
        read_timeout_seconds=1,
        max_request_bytes=65_536,
        max_response_bytes=1_000_000,
    )
    snapshot = LiveConsoleDataSource(build_simulated_service(), config).collect()
    assert snapshot.mode == "measurement_only"
    assert {(item.category, item.target) for item in snapshot.observations} == {
        ("node", fixtures.NODE),
        ("guests", fixtures.NODE),
        ("storage", "local"),
        ("tasks", fixtures.NODE),
        ("backups", fixtures.NODE),
    }
