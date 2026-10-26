"""Fail-closed loading of the fixed mailer configuration seam.

These cover the configuration boundary only. Loading the seam must never
dispatch mail, so there is no network fixture here on purpose.
"""

from pathlib import Path

import pytest

from nodeops_mcp.config import load_email_worker_config

ORIGIN = "https://nodeops-mailer.example.workers.dev"

REQUIRED = (
    "NODEOPS_MAILER_ORIGIN_FILE",
    "NODEOPS_MAILER_TOKEN_FILE",
    "NODEOPS_MAILER_CONNECT_TIMEOUT_SECONDS",
    "NODEOPS_MAILER_READ_TIMEOUT_SECONDS",
    "NODEOPS_MAILER_MAX_RESPONSE_BYTES",
)


def staged(tmp_path: Path, *, origin: str = ORIGIN) -> dict[str, str]:
    origin_file = tmp_path / "mailer-origin"
    origin_file.write_text(f"{origin}\n", encoding="utf-8")
    origin_file.chmod(0o400)
    token = tmp_path / "mailer-token"
    token.write_text("A" * 48, encoding="utf-8")
    token.chmod(0o400)
    return {
        "NODEOPS_MAILER_ORIGIN_FILE": str(origin_file),
        "NODEOPS_MAILER_TOKEN_FILE": str(token),
        "NODEOPS_MAILER_CONNECT_TIMEOUT_SECONDS": "5",
        "NODEOPS_MAILER_READ_TIMEOUT_SECONDS": "10",
        "NODEOPS_MAILER_MAX_RESPONSE_BYTES": "4096",
    }


def apply(monkeypatch: pytest.MonkeyPatch, values: dict[str, str]) -> None:
    for name in REQUIRED:
        monkeypatch.delenv(name, raising=False)
    for name, value in values.items():
        monkeypatch.setenv(name, value)


def test_staged_boundary_loads(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Positive control: a fully staged boundary must actually load."""

    values = staged(tmp_path)
    apply(monkeypatch, values)
    config = load_email_worker_config()
    assert config.worker_origin == ORIGIN
    assert config.token_file == Path(values["NODEOPS_MAILER_TOKEN_FILE"])
    assert config.connect_timeout_seconds == 5
    assert config.read_timeout_seconds == 10
    assert config.max_response_bytes == 4096


@pytest.mark.parametrize("absent", REQUIRED)
def test_each_absent_setting_fails_closed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, absent: str
) -> None:
    values = staged(tmp_path)
    del values[absent]
    apply(monkeypatch, values)
    with pytest.raises(ValueError):
        load_email_worker_config()


def test_relative_path_is_rejected(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    values = staged(tmp_path)
    values["NODEOPS_MAILER_TOKEN_FILE"] = "mailer-token"
    apply(monkeypatch, values)
    with pytest.raises(ValueError, match="absolute"):
        load_email_worker_config()


def test_absent_token_file_is_rejected(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    values = staged(tmp_path)
    values["NODEOPS_MAILER_TOKEN_FILE"] = str(tmp_path / "missing-token")
    apply(monkeypatch, values)
    with pytest.raises(ValueError, match="absent"):
        load_email_worker_config()


def test_empty_origin_file_is_rejected(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    values = staged(tmp_path)
    origin_file = Path(values["NODEOPS_MAILER_ORIGIN_FILE"])
    origin_file.chmod(0o600)
    origin_file.write_text("   \n", encoding="utf-8")
    origin_file.chmod(0o400)
    apply(monkeypatch, values)
    with pytest.raises(ValueError, match="empty"):
        load_email_worker_config()


@pytest.mark.parametrize(
    "origin",
    [
        "http://nodeops-mailer.example.workers.dev",
        "https://nodeops-mailer.example.workers.dev/deliver",
        "https://user:pass@nodeops-mailer.example.workers.dev",
        "https://nodeops-mailer.example.workers.dev?x=1",
        "not-a-url",
    ],
)
def test_unsafe_origin_is_rejected(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, origin: str
) -> None:
    apply(monkeypatch, staged(tmp_path, origin=origin))
    with pytest.raises(ValueError):
        load_email_worker_config()


@pytest.mark.parametrize(
    ("name", "value"),
    [
        ("NODEOPS_MAILER_CONNECT_TIMEOUT_SECONDS", "0"),
        ("NODEOPS_MAILER_CONNECT_TIMEOUT_SECONDS", "31"),
        ("NODEOPS_MAILER_READ_TIMEOUT_SECONDS", "0"),
        ("NODEOPS_MAILER_READ_TIMEOUT_SECONDS", "31"),
        ("NODEOPS_MAILER_MAX_RESPONSE_BYTES", "255"),
        ("NODEOPS_MAILER_MAX_RESPONSE_BYTES", "16385"),
    ],
)
def test_out_of_band_limits_are_rejected(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, name: str, value: str
) -> None:
    values = staged(tmp_path)
    values[name] = value
    apply(monkeypatch, values)
    with pytest.raises(ValueError):
        load_email_worker_config()
