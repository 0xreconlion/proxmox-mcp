"""One-time setup crawl that proposes allowlists from your own Proxmox.

This module exists so you never have to hand-write node, storage, and guest
allowlists. It performs read-only `GET` requests against your Proxmox API and
prints a configuration block for you to review and trim.

> **It deliberately reads more broadly than the running server is allowed to.**
> That is the point: you cannot build an allowlist from inside an allowlist. This
> is setup tooling, invoked by you from a shell. It is never registered as an MCP
> tool and is never reachable by a model or an MCP client.

Nothing here writes to Proxmox. Only `GET` is used, and the request helper
refuses any other verb.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import httpx

MAX_RESPONSE_BYTES = 4_194_304
CONNECT_TIMEOUT_SECONDS = 5.0
READ_TIMEOUT_SECONDS = 20.0

#: Guests whose purpose is usually not worth monitoring. Only a default for the
#: proposal; you decide what stays.
_NOISY_NAME_HINTS = ("template", "test", "scratch", "tmp")


class DiscoveryError(RuntimeError):
    """Raised when discovery cannot complete safely."""


@dataclass(slots=True)
class Discovered:
    nodes: list[str] = field(default_factory=list)
    storages: list[str] = field(default_factory=list)
    guests: list[dict[str, Any]] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    def suggested_guest_allowlist(self, *, running_only: bool) -> list[str]:
        chosen = []
        for guest in self.guests:
            if running_only and guest.get("status") != "running":
                continue
            name = str(guest.get("name", "")).lower()
            if any(hint in name for hint in _NOISY_NAME_HINTS):
                continue
            chosen.append(f"{guest['kind']}:{guest['guest_id']}")
        return sorted(chosen, key=lambda item: (item.split(":")[0], int(item.split(":")[1])))

    def as_env(self, *, running_only: bool = True) -> str:
        """Render the discovered state as a .env fragment, ready to review."""

        return "\n".join(
            (
                f"NODEOPS_PROXMOX_NODE_ALLOWLIST={','.join(sorted(self.nodes))}",
                f"NODEOPS_PROXMOX_STORAGE_ALLOWLIST={','.join(sorted(self.storages))}",
                "NODEOPS_PROXMOX_GUEST_ALLOWLIST="
                + ",".join(self.suggested_guest_allowlist(running_only=running_only)),
            )
        )

    def as_json(self, *, running_only: bool = True) -> str:
        return json.dumps(
            {
                "nodes": sorted(self.nodes),
                "storages": sorted(self.storages),
                "guests": self.suggested_guest_allowlist(running_only=running_only),
            },
            indent=2,
        )


def _verify_option(ca_file: Path | None, *, insecure: bool) -> Any:
    if insecure:
        return False
    if ca_file is not None:
        if not ca_file.is_file():
            raise DiscoveryError(f"CA file not found: {ca_file}")
        return str(ca_file)
    return True


def _get(client: httpx.Client, path: str) -> list[dict[str, Any]]:
    """Issue one bounded read. Only GET is ever used."""

    try:
        with client.stream("GET", path) as response:
            if response.status_code == 401:
                raise DiscoveryError(
                    "Proxmox rejected the token (401). Check the token id format "
                    "(user@realm!tokenname) and that the secret is correct."
                )
            if response.status_code == 403:
                raise DiscoveryError(
                    f"Proxmox denied access to {path} (403). The token needs at least "
                    "PVEAuditor on / to complete discovery."
                )
            if response.status_code >= 400:
                raise DiscoveryError(f"Proxmox returned {response.status_code} for {path}")
            total = 0
            chunks = []
            for chunk in response.iter_bytes():
                total += len(chunk)
                if total > MAX_RESPONSE_BYTES:
                    raise DiscoveryError(f"response from {path} exceeded the size bound")
                chunks.append(chunk)
    except httpx.HTTPError as error:
        raise DiscoveryError(f"could not reach Proxmox at {path}: {error}") from error

    try:
        payload = json.loads(b"".join(chunks))
    except json.JSONDecodeError as error:
        raise DiscoveryError(f"Proxmox returned non-JSON for {path}") from error
    data = payload.get("data") if isinstance(payload, dict) else None
    if not isinstance(data, list):
        raise DiscoveryError(f"unexpected response shape from {path}")
    return [item for item in data if isinstance(item, dict)]


def discover(
    *,
    base_url: str,
    token_id: str,
    token_secret: str,
    ca_file: Path | None = None,
    insecure: bool = False,
) -> Discovered:
    """Crawl one Proxmox API for nodes, storages, and guests."""

    parsed = urlparse(base_url)
    if parsed.scheme != "https" or not parsed.hostname:
        raise DiscoveryError("base URL must be an HTTPS origin, e.g. https://proxmox.lan:8006")
    if parsed.path not in ("", "/"):
        raise DiscoveryError("base URL must be an origin with no path")

    found = Discovered()
    if insecure:
        found.warnings.append(
            "TLS verification was disabled for discovery. Do not run the server this way; "
            "export your Proxmox CA and set NODEOPS_PROXMOX_CA_FILE instead."
        )

    headers = {"Authorization": f"PVEAPIToken={token_id}={token_secret}"}
    timeout = httpx.Timeout(
        connect=CONNECT_TIMEOUT_SECONDS,
        read=READ_TIMEOUT_SECONDS,
        write=READ_TIMEOUT_SECONDS,
        pool=CONNECT_TIMEOUT_SECONDS,
    )
    with httpx.Client(
        base_url=base_url.rstrip("/") + "/api2/json",
        headers=headers,
        timeout=timeout,
        verify=_verify_option(ca_file, insecure=insecure),
        follow_redirects=False,
    ) as client:
        for node in _get(client, "/nodes"):
            name = node.get("node")
            if isinstance(name, str) and name:
                found.nodes.append(name)
        if not found.nodes:
            raise DiscoveryError("no nodes were returned; the token may lack read access")

        seen_storage: set[str] = set()
        for node_name in found.nodes:
            try:
                for store in _get(client, f"/nodes/{node_name}/storage"):
                    name = store.get("storage")
                    if isinstance(name, str) and name and name not in seen_storage:
                        seen_storage.add(name)
                        found.storages.append(name)
            except DiscoveryError as error:
                found.warnings.append(f"storage enumeration failed on {node_name}: {error}")

        for resource in _get(client, "/cluster/resources?type=vm"):
            kind = resource.get("type")
            vmid = resource.get("vmid")
            if kind not in {"qemu", "lxc"} or not isinstance(vmid, int):
                continue
            if resource.get("template"):
                continue
            found.guests.append(
                {
                    "kind": kind,
                    "guest_id": vmid,
                    "name": str(resource.get("name", "")),
                    "node": str(resource.get("node", "")),
                    "status": str(resource.get("status", "")),
                }
            )

    if not found.storages:
        found.warnings.append(
            "no storages were discovered; at least one is required to register "
            "the get_storage_status tool"
        )
    return found
