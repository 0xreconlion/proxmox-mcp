from datetime import UTC, datetime
from pathlib import Path

import httpx
import pytest
import respx

from nodeops_mcp.notifications import (
    CloudflareEmailWorkerClient,
    DeliveryStatus,
    EmailWorkerConfig,
    NotificationEvent,
    NotificationState,
)

ORIGIN = "https://nodeops-mailer.example.workers.dev"
NOW = datetime(2026, 10, 25, 6, 0, tzinfo=UTC)


def token_file(tmp_path: Path, *, mode: int = 0o600) -> Path:
    path = tmp_path / "mailer-token"
    path.write_text("A" * 48, encoding="utf-8")
    path.chmod(mode)
    return path


def config(tmp_path: Path, **overrides: object) -> EmailWorkerConfig:
    values: dict[str, object] = {
        "worker_origin": ORIGIN,
        "token_file": token_file(tmp_path),
        "connect_timeout_seconds": 1,
        "read_timeout_seconds": 2,
    }
    values.update(overrides)
    return EmailWorkerConfig(**values)  # type: ignore[arg-type]


def event(**overrides: object) -> NotificationEvent:
    values: dict[str, object] = {
        "event_id": "evt-01",
        "state": NotificationState.CRITICAL,
        "rule_id": "backup_failure_v1",
        "target_type": "qemu",
        "target_id": "110",
        "observed_at": NOW,
        "summary": "Two consecutive bounded backup outcomes failed.",
        "runbook_ref": "runbook:backup-failure",
    }
    values.update(overrides)
    return NotificationEvent(**values)  # type: ignore[arg-type]


@respx.mock
def test_fixed_endpoint_and_structured_payload_are_used(tmp_path: Path) -> None:
    route = respx.post(f"{ORIGIN}/deliver").mock(return_value=httpx.Response(204))
    result = CloudflareEmailWorkerClient(config(tmp_path)).send(event())
    assert result.status is DeliveryStatus.ACCEPTED
    request = route.calls[0].request
    assert request.headers["authorization"] == f"Bearer {'A' * 48}"
    payload = __import__("json").loads(request.content)
    assert payload == event().to_payload()
    assert "to" not in payload and "from" not in payload


@pytest.mark.parametrize(
    ("status_code", "expected"),
    [
        (400, DeliveryStatus.REJECTED),
        (401, DeliveryStatus.DENIED),
        (403, DeliveryStatus.DENIED),
        (429, DeliveryStatus.UNAVAILABLE),
        (500, DeliveryStatus.UNAVAILABLE),
    ],
)
@respx.mock
def test_worker_statuses_are_finite_without_response_details(
    tmp_path: Path, status_code: int, expected: DeliveryStatus
) -> None:
    respx.post(f"{ORIGIN}/deliver").mock(
        return_value=httpx.Response(status_code, text="token=secret upstream detail")
    )
    result = CloudflareEmailWorkerClient(config(tmp_path)).send(event())
    assert result.status is expected
    assert "secret" not in result.reason


@respx.mock
def test_redirect_is_not_followed(tmp_path: Path) -> None:
    respx.post(f"{ORIGIN}/deliver").mock(
        return_value=httpx.Response(302, headers={"location": "https://attacker.invalid"})
    )
    result = CloudflareEmailWorkerClient(config(tmp_path)).send(event())
    assert result.status is DeliveryStatus.UNAVAILABLE
    assert len(respx.calls) == 1


def test_missing_or_permissive_token_file_fails_closed(tmp_path: Path) -> None:
    missing = config(tmp_path, token_file=tmp_path / "missing")
    assert CloudflareEmailWorkerClient(missing).send(event()).status is DeliveryStatus.UNAVAILABLE

    permissive_path = token_file(tmp_path, mode=0o644)
    permissive = config(tmp_path, token_file=permissive_path)
    assert CloudflareEmailWorkerClient(permissive).send(event()).status is DeliveryStatus.UNAVAILABLE

    symlink = tmp_path / "mailer-token-link"
    symlink.symlink_to(token_file(tmp_path))
    linked = config(tmp_path, token_file=symlink)
    assert CloudflareEmailWorkerClient(linked).send(event()).status is DeliveryStatus.UNAVAILABLE


@pytest.mark.parametrize(
    "origin",
    [
        "http://mailer.example",
        "https://user@example.com",
        "https://mailer.example/path",
        "https://mailer.example?target=other",
    ],
)
def test_worker_origin_must_be_one_fixed_https_origin(tmp_path: Path, origin: str) -> None:
    with pytest.raises(ValueError, match="origin"):
        CloudflareEmailWorkerClient(config(tmp_path, worker_origin=origin))


@pytest.mark.parametrize(
    "summary",
    [
        "Bearer secret-value",
        "See https://attacker.invalid/details",
        "token=secret-value",
        "bad\x00control",
        "é" * 601,
    ],
)
def test_secret_like_or_control_summary_is_rejected(summary: str) -> None:
    with pytest.raises(ValueError, match="summary"):
        event(summary=summary)
