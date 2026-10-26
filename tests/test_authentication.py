from pathlib import Path

import pytest

from proxmox_mcp.security.authentication import (
    StaticBearerConfig,
    StaticBearerTokenVerifier,
    build_auth_settings,
)


def make_config(tmp_path: Path) -> StaticBearerConfig:
    token_file = tmp_path / "client-token"
    token_file.write_text("owner-client-fixture-token", encoding="utf-8")
    return StaticBearerConfig(
        token_file=token_file,
        issuer_url="https://proxmox-mcp.test:8443",
        resource_server_url="https://proxmox-mcp.test:8443/mcp",
        client_id="approved-owner-client",
        required_scope="proxmox-mcp:read",
        allowed_hosts=("proxmox-mcp.test:8443",),
        allowed_origins=("https://proxmox-mcp.test:8443",),
    )


@pytest.mark.anyio
async def test_static_token_verifier_returns_bounded_identity(tmp_path: Path) -> None:
    config = make_config(tmp_path)
    access = await StaticBearerTokenVerifier(config).verify_token(
        "owner-client-fixture-token"
    )
    assert access is not None
    assert access.client_id == "approved-owner-client"
    assert access.scopes == ["proxmox-mcp:read"]
    assert access.resource == "https://proxmox-mcp.test:8443/mcp"
    assert access.token == "accepted-but-never-logged"


@pytest.mark.anyio
async def test_static_token_verifier_rejects_invalid_token(tmp_path: Path) -> None:
    access = await StaticBearerTokenVerifier(make_config(tmp_path)).verify_token("wrong")
    assert access is None


def test_auth_settings_require_resource_binding(tmp_path: Path) -> None:
    settings = build_auth_settings(make_config(tmp_path))
    assert settings.validate_token_resource is True
    assert settings.required_scopes == ["proxmox-mcp:read"]


def test_wildcard_host_is_forbidden(tmp_path: Path) -> None:
    config = make_config(tmp_path)
    invalid = StaticBearerConfig(
        token_file=config.token_file,
        issuer_url=config.issuer_url,
        resource_server_url=config.resource_server_url,
        client_id=config.client_id,
        required_scope=config.required_scope,
        allowed_hosts=("proxmox-mcp.test:*",),
        allowed_origins=config.allowed_origins,
    )
    with pytest.raises(ValueError, match="wildcards"):
        invalid.validate()
