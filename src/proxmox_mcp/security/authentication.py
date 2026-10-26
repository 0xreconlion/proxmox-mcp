"""Execution-time authentication for the owner-only private HTTP service."""

from __future__ import annotations

import hmac
import os
from dataclasses import dataclass
from pathlib import Path

from mcp.server.auth.provider import AccessToken
from mcp.server.auth.settings import AuthSettings
from pydantic import AnyHttpUrl


@dataclass(frozen=True, slots=True)
class StaticBearerConfig:
    token_file: Path
    issuer_url: str
    resource_server_url: str
    client_id: str
    required_scope: str
    allowed_hosts: tuple[str, ...]
    allowed_origins: tuple[str, ...]

    def validate(self) -> None:
        if not self.token_file.is_absolute() or not self.token_file.is_file():
            raise ValueError("the protected client-token file must exist at an absolute path")
        if not self.issuer_url.startswith("https://"):
            raise ValueError("the authentication issuer URL must use HTTPS")
        if not self.resource_server_url.startswith("https://"):
            raise ValueError("the protected resource URL must use HTTPS")
        if not self.client_id or not self.required_scope:
            raise ValueError("client identity and required scope are mandatory")
        if not self.allowed_hosts or not self.allowed_origins:
            raise ValueError("exact Host and Origin allowlists are mandatory")
        if any("*" in item for item in (*self.allowed_hosts, *self.allowed_origins)):
            raise ValueError("wildcards are forbidden in production Host and Origin allowlists")


def _required(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        raise ValueError(f"required authentication setting is absent: {name}")
    return value


def load_static_bearer_config() -> StaticBearerConfig:
    config = StaticBearerConfig(
        token_file=Path(_required("PROXMOX_MCP_CLIENT_TOKEN_FILE")),
        issuer_url=_required("PROXMOX_MCP_AUTH_ISSUER_URL"),
        resource_server_url=_required("PROXMOX_MCP_RESOURCE_SERVER_URL"),
        client_id=_required("PROXMOX_MCP_CLIENT_ID"),
        required_scope=_required("PROXMOX_MCP_REQUIRED_SCOPE"),
        allowed_hosts=tuple(
            item.strip() for item in _required("PROXMOX_MCP_ALLOWED_HOSTS").split(",")
        ),
        allowed_origins=tuple(
            item.strip() for item in _required("PROXMOX_MCP_ALLOWED_ORIGINS").split(",")
        ),
    )
    config.validate()
    return config


class StaticBearerTokenVerifier:
    """Verify one externally stored owner-client token using constant-time comparison."""

    def __init__(self, config: StaticBearerConfig) -> None:
        config.validate()
        self._config = config

    async def verify_token(self, token: str) -> AccessToken | None:
        expected = self._config.token_file.read_text(encoding="utf-8").strip()
        if not expected or not hmac.compare_digest(token, expected):
            return None
        return AccessToken(
            token="accepted-but-never-logged",
            client_id=self._config.client_id,
            scopes=[self._config.required_scope],
            resource=self._config.resource_server_url,
            subject="owner",
            claims={"iss": self._config.issuer_url},
        )


def build_auth_settings(config: StaticBearerConfig) -> AuthSettings:
    return AuthSettings(
        issuer_url=AnyHttpUrl(config.issuer_url),
        resource_server_url=AnyHttpUrl(config.resource_server_url),
        required_scopes=[config.required_scope],
        validate_token_resource=True,
    )
