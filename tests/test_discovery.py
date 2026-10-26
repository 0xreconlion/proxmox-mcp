"""Discovery crawl behavior, exercised without any real credential."""

from __future__ import annotations

import httpx
import pytest
import respx

from proxmox_mcp.discovery import Discovered, DiscoveryError, discover

ORIGIN = "https://proxmox.example.invalid:8006"
API = f"{ORIGIN}/api2/json"
TOKEN_ID = "mcp@pve!reader"
SECRET = "x" * 40


def _ok(payload: object) -> httpx.Response:
    return httpx.Response(200, json={"data": payload})


def _mock_cluster() -> None:
    respx.get(f"{API}/nodes").mock(return_value=_ok([{"node": "hv-a"}, {"node": "hv-b"}]))
    respx.get(f"{API}/nodes/hv-a/storage").mock(
        return_value=_ok([{"storage": "local"}, {"storage": "ssd-pool"}])
    )
    respx.get(f"{API}/nodes/hv-b/storage").mock(
        return_value=_ok([{"storage": "local"}, {"storage": "nas-backups"}])
    )
    respx.get(f"{API}/cluster/resources", params={"type": "vm"}).mock(
        return_value=_ok(
            [
                {"type": "qemu", "vmid": 110, "name": "app", "node": "hv-a", "status": "running"},
                {"type": "lxc", "vmid": 200, "name": "proxy", "node": "hv-a", "status": "running"},
                {"type": "lxc", "vmid": 201, "name": "old", "node": "hv-b", "status": "stopped"},
                {"type": "qemu", "vmid": 900, "name": "debian-template",
                 "node": "hv-b", "status": "stopped", "template": 1},
                {"type": "lxc", "vmid": 300, "name": "test-scratch",
                 "node": "hv-a", "status": "running"},
            ]
        )
    )


def _run() -> Discovered:
    return discover(base_url=ORIGIN, token_id=TOKEN_ID, token_secret=SECRET)


@respx.mock
def test_discovers_nodes_storages_and_guests() -> None:
    _mock_cluster()
    found = _run()
    assert sorted(found.nodes) == ["hv-a", "hv-b"]
    assert sorted(found.storages) == ["local", "nas-backups", "ssd-pool"]
    assert len(found.guests) == 4  # the template is excluded


@respx.mock
def test_templates_are_never_proposed() -> None:
    _mock_cluster()
    assert all(guest["guest_id"] != 900 for guest in _run().guests)


@respx.mock
def test_default_proposal_omits_stopped_and_test_named_guests() -> None:
    _mock_cluster()
    proposed = _run().suggested_guest_allowlist(running_only=True)
    assert proposed == ["lxc:200", "qemu:110"]


@respx.mock
def test_stopped_guests_included_on_request() -> None:
    _mock_cluster()
    proposed = _run().suggested_guest_allowlist(running_only=False)
    assert "lxc:201" in proposed


@respx.mock
def test_env_rendering_is_reviewable() -> None:
    _mock_cluster()
    rendered = _run().as_env()
    assert "PROXMOX_MCP_NODE_ALLOWLIST=hv-a,hv-b" in rendered
    assert "PROXMOX_MCP_STORAGE_ALLOWLIST=local,nas-backups,ssd-pool" in rendered
    assert "PROXMOX_MCP_GUEST_ALLOWLIST=lxc:200,qemu:110" in rendered


@respx.mock
def test_sends_token_header_and_only_uses_get() -> None:
    _mock_cluster()
    _run()
    for call in respx.calls:
        assert call.request.method == "GET"
        assert call.request.headers["authorization"] == f"PVEAPIToken={TOKEN_ID}={SECRET}"


@pytest.mark.parametrize(
    "bad_url",
    ["http://proxmox.example.invalid", "https://proxmox.example.invalid/api2", "not-a-url"],
)
def test_unsafe_base_url_is_rejected(bad_url: str) -> None:
    with pytest.raises(DiscoveryError):
        discover(base_url=bad_url, token_id=TOKEN_ID, token_secret=SECRET)


@respx.mock
def test_401_explains_the_token_format() -> None:
    respx.get(f"{API}/nodes").mock(return_value=httpx.Response(401))
    with pytest.raises(DiscoveryError, match="user@realm!tokenname"):
        _run()


@respx.mock
def test_403_names_the_required_privilege() -> None:
    respx.get(f"{API}/nodes").mock(return_value=httpx.Response(403))
    with pytest.raises(DiscoveryError, match="PVEAuditor"):
        _run()


@respx.mock
def test_empty_node_list_fails_closed() -> None:
    respx.get(f"{API}/nodes").mock(return_value=_ok([]))
    with pytest.raises(DiscoveryError, match="no nodes"):
        _run()


@respx.mock
def test_storage_failure_on_one_node_warns_without_aborting() -> None:
    respx.get(f"{API}/nodes").mock(return_value=_ok([{"node": "hv-a"}, {"node": "hv-b"}]))
    respx.get(f"{API}/nodes/hv-a/storage").mock(return_value=_ok([{"storage": "local"}]))
    respx.get(f"{API}/nodes/hv-b/storage").mock(return_value=httpx.Response(403))
    respx.get(f"{API}/cluster/resources", params={"type": "vm"}).mock(return_value=_ok([]))
    found = _run()
    assert found.storages == ["local"]
    assert any("hv-b" in warning for warning in found.warnings)


@respx.mock
def test_insecure_discovery_warns_loudly() -> None:
    _mock_cluster()
    found = discover(
        base_url=ORIGIN, token_id=TOKEN_ID, token_secret=SECRET, insecure=True
    )
    assert any("TLS verification was disabled" in warning for warning in found.warnings)
