"""Fail-closed runtime configuration for the fixed Proxmox integration."""

import os
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlparse

from proxmox_mcp.security.limits import MAX_REQUEST_BODY_BYTES


@dataclass(frozen=True, slots=True)
class ProxmoxConfig:
    environment: str
    deployment_version: str
    proxmox_base_url: str
    node_allowlist: frozenset[str]
    guest_allowlist: frozenset[tuple[str, int]]
    storage_allowlist: frozenset[str]
    token_id_file: Path
    token_secret_file: Path
    proxmox_ca_file: Path
    connect_timeout_seconds: float
    read_timeout_seconds: float
    max_request_bytes: int
    max_response_bytes: int

    def validate(self) -> None:
        parsed = urlparse(self.proxmox_base_url)
        if parsed.scheme != "https" or not parsed.hostname:
            raise ValueError("Proxmox base URL must be one fixed HTTPS origin")
        if parsed.username or parsed.password or parsed.query or parsed.fragment:
            raise ValueError("Proxmox base URL cannot embed credentials, query, or fragment")
        if parsed.path not in ("", "/"):
            raise ValueError("Proxmox base URL must be an origin without an API path")
        if self.environment not in {"development", "test", "production"}:
            raise ValueError("environment must be development, test, or production")
        if not self.deployment_version:
            raise ValueError("deployment version is required")
        if not self.node_allowlist or any("*" in node for node in self.node_allowlist):
            raise ValueError("an exact non-wildcard node allowlist is required")
        if any(kind not in {"qemu", "lxc"} or guest_id <= 0 for kind, guest_id in self.guest_allowlist):
            raise ValueError("guest allowlist entries must be exact qemu/lxc identifiers")
        if any(not storage or "*" in storage for storage in self.storage_allowlist):
            raise ValueError("storage allowlist entries must be exact identifiers")
        if self.connect_timeout_seconds <= 0 or self.read_timeout_seconds <= 0:
            raise ValueError("positive timeout budgets are required")
        if self.max_request_bytes != MAX_REQUEST_BODY_BYTES:
            raise ValueError(
                f"request limit must be the reviewed {MAX_REQUEST_BODY_BYTES}-byte value"
            )
        if self.max_response_bytes <= 0:
            raise ValueError("an explicit positive response limit is required")
        for path in (self.token_id_file, self.token_secret_file, self.proxmox_ca_file):
            if not path.is_absolute():
                raise ValueError("credential and CA paths must be absolute")


def _required(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        raise ValueError(f"required setting is absent: {name}")
    return value


def _required_protected_file(name: str) -> Path:
    path = Path(_required(name))
    if not path.is_absolute():
        raise ValueError(f"{name} must be an absolute path")
    if not path.is_file():
        raise ValueError(f"required protected file is absent: {path}")
    return path


def load_live_config() -> ProxmoxConfig:
    """Load live configuration without accepting per-call target or URL overrides."""

    guests: set[tuple[str, int]] = set()
    for item in _required("PROXMOX_MCP_GUEST_ALLOWLIST").split(","):
        kind, separator, raw_id = item.strip().partition(":")
        if not separator:
            raise ValueError("guest allowlist entries must use kind:id")
        guests.add((kind, int(raw_id)))

    config = ProxmoxConfig(
        environment=_required("PROXMOX_MCP_ENV"),
        deployment_version=_required("PROXMOX_MCP_DEPLOYMENT_VERSION"),
        proxmox_base_url=_required("PROXMOX_MCP_BASE_URL").rstrip("/"),
        node_allowlist=frozenset(
            item.strip() for item in _required("PROXMOX_MCP_NODE_ALLOWLIST").split(",")
        ),
        guest_allowlist=frozenset(guests),
        storage_allowlist=frozenset(
            item.strip() for item in _required("PROXMOX_MCP_STORAGE_ALLOWLIST").split(",")
        ),
        token_id_file=Path(_required("PROXMOX_MCP_TOKEN_ID_FILE")),
        token_secret_file=Path(_required("PROXMOX_MCP_TOKEN_SECRET_FILE")),
        proxmox_ca_file=Path(_required("PROXMOX_MCP_CA_FILE")),
        connect_timeout_seconds=float(_required("PROXMOX_MCP_CONNECT_TIMEOUT_SECONDS")),
        read_timeout_seconds=float(_required("PROXMOX_MCP_READ_TIMEOUT_SECONDS")),
        max_request_bytes=int(_required("PROXMOX_MCP_MAX_REQUEST_BYTES")),
        max_response_bytes=int(_required("PROXMOX_MCP_MAX_RESPONSE_BYTES")),
    )
    config.validate()
    for path in (config.token_id_file, config.token_secret_file, config.proxmox_ca_file):
        if not path.is_file():
            raise ValueError(f"required protected file is absent: {path}")
    return config
