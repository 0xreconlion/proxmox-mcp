"""Typed, read-only Proxmox adapter with fixed routes and bounded responses."""

from __future__ import annotations

import json
import ssl
import time
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Generic, Protocol, TypeVar, cast

import httpx

from proxmox_mcp import fixtures
from proxmox_mcp.config import ProxmoxConfig
from proxmox_mcp.schemas import BoundedQuery, GuestTarget, ResultStatus, ToolError

T = TypeVar("T")
QueryScalar = str | int | float | bool | None


@dataclass(frozen=True, slots=True)
class AdapterResult(Generic[T]):
    value: T
    errors: tuple[ToolError, ...] = ()


class ProxmoxAdapterError(RuntimeError):
    """Safe finite adapter failure; never contains an upstream body or credential."""

    def __init__(
        self,
        code: str,
        message: str,
        *,
        status: ResultStatus = ResultStatus.UNAVAILABLE,
        backend_status: int | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.safe_message = message
        self.status = status
        self.backend_status = backend_status


class ProxmoxReadAdapter(Protocol):
    def list_guests(self, *, limit: int) -> AdapterResult[list[Mapping[str, object]]]: ...

    def get_guest_status(self, target: GuestTarget) -> AdapterResult[Mapping[str, object]]: ...

    def get_node_health(self, node: str) -> AdapterResult[Mapping[str, object]]: ...

    def get_storage_status(
        self, node: str, storage: str
    ) -> AdapterResult[Mapping[str, object]]: ...

    def get_recent_tasks(
        self, node: str, query: BoundedQuery
    ) -> AdapterResult[list[Mapping[str, object]]]: ...

    def get_backup_status(
        self, node: str, query: BoundedQuery
    ) -> AdapterResult[list[Mapping[str, object]]]: ...


class UnconfiguredProxmoxAdapter:
    """Fail closed when no approved adapter is selected."""

    def __getattr__(self, name: str) -> object:
        raise RuntimeError(f"Proxmox adapter is not configured: {name}")


class SimulatedProxmoxAdapter:
    """Credential-free deterministic adapter for protocol and policy tests."""

    def list_guests(self, *, limit: int) -> AdapterResult[list[Mapping[str, object]]]:
        return AdapterResult([dict(guest) for guest in fixtures.GUESTS[:limit]])

    def get_guest_status(self, target: GuestTarget) -> AdapterResult[Mapping[str, object]]:
        for guest in fixtures.GUESTS:
            if (
                guest["guest_type"] == target.guest_type.value
                and guest["guest_id"] == target.guest_id
            ):
                return AdapterResult(dict(guest))
        raise LookupError("allowlisted guest is absent from the fixture")

    def get_node_health(self, node: str) -> AdapterResult[Mapping[str, object]]:
        if node != fixtures.NODE_HEALTH["node"]:
            raise LookupError("allowlisted node is absent from the fixture")
        return AdapterResult(dict(fixtures.NODE_HEALTH))

    def get_storage_status(
        self, node: str, storage: str
    ) -> AdapterResult[Mapping[str, object]]:
        if node != fixtures.NODE_HEALTH["node"]:
            raise LookupError("allowlisted node is absent from the fixture")
        return AdapterResult(dict(fixtures.STORAGES[storage]))

    def get_recent_tasks(
        self, node: str, query: BoundedQuery
    ) -> AdapterResult[list[Mapping[str, object]]]:
        del node
        _ = query.lookback_minutes
        return AdapterResult([dict(task) for task in fixtures.RECENT_TASKS[: query.limit]])

    def get_backup_status(
        self, node: str, query: BoundedQuery
    ) -> AdapterResult[list[Mapping[str, object]]]:
        del node
        _ = query.lookback_minutes
        return AdapterResult([dict(backup) for backup in fixtures.BACKUPS[: query.limit]])


class HttpxProxmoxAdapter:
    """Fixed-origin Proxmox API client with no generic route or method surface."""

    def __init__(self, config: ProxmoxConfig) -> None:
        config.validate()
        self._config = config
        token_id = config.token_id_file.read_text(encoding="utf-8").strip()
        token_secret = config.token_secret_file.read_text(encoding="utf-8").strip()
        if not token_id or not token_secret:
            raise ValueError("protected Proxmox credential files must be non-empty")
        self._headers = {"Authorization": f"PVEAPIToken={token_id}={token_secret}"}
        self._ssl_context = ssl.create_default_context(cafile=str(config.proxmox_ca_file))
        self._timeout = httpx.Timeout(
            connect=config.connect_timeout_seconds,
            read=config.read_timeout_seconds,
            write=config.connect_timeout_seconds,
            pool=config.connect_timeout_seconds,
        )

    def _request(
        self, path: str, *, params: Mapping[str, QueryScalar] | None = None
    ) -> object:
        url = f"{self._config.proxmox_base_url}/api2/json{path}"
        try:
            with httpx.Client(
                headers=self._headers,
                verify=self._ssl_context,
                timeout=self._timeout,
                follow_redirects=False,
            ) as client, client.stream("GET", url, params=params) as response:
                if response.is_redirect:
                    raise ProxmoxAdapterError(
                        "redirect_denied", "backend redirect was rejected"
                    )
                if response.status_code in {401, 403}:
                    raise ProxmoxAdapterError(
                        "backend_denied",
                        "backend authorization denied the read",
                        status=ResultStatus.DENIED,
                        backend_status=response.status_code,
                    )
                if response.status_code == 404:
                    raise ProxmoxAdapterError(
                        "target_not_found",
                        "allowlisted backend target was not found",
                        backend_status=404,
                    )
                if response.status_code >= 400:
                    raise ProxmoxAdapterError(
                        "backend_error",
                        "backend read failed",
                        backend_status=response.status_code,
                    )
                chunks: list[bytes] = []
                size = 0
                for chunk in response.iter_bytes():
                    size += len(chunk)
                    if size > self._config.max_response_bytes:
                        raise ProxmoxAdapterError(
                            "response_too_large",
                            "backend response exceeded the configured cap",
                        )
                    chunks.append(chunk)
        except ProxmoxAdapterError:
            raise
        except httpx.TimeoutException as exc:
            raise ProxmoxAdapterError("backend_timeout", "backend read timed out") from exc
        except httpx.ConnectError as exc:
            code = "tls_failure" if "certificate" in str(exc).lower() else "backend_unavailable"
            message = (
                "backend TLS verification failed"
                if code == "tls_failure"
                else "backend transport failed"
            )
            raise ProxmoxAdapterError(code, message) from exc
        except httpx.HTTPError as exc:
            raise ProxmoxAdapterError("backend_unavailable", "backend transport failed") from exc

        try:
            payload = json.loads(b"".join(chunks))
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            raise ProxmoxAdapterError(
                "invalid_backend_json", "backend returned invalid JSON"
            ) from exc
        if not isinstance(payload, dict) or "data" not in payload:
            raise ProxmoxAdapterError(
                "invalid_backend_shape", "backend response shape was invalid"
            )
        return cast(dict[str, object], payload)["data"]

    def _require_node(self, node: str) -> None:
        if node not in self._config.node_allowlist:
            raise PermissionError("node is not allowlisted")

    def _require_guest(self, target: GuestTarget) -> None:
        if (target.guest_type.value, target.guest_id) not in self._config.guest_allowlist:
            raise PermissionError("guest is not allowlisted")

    def _require_storage(self, storage: str) -> None:
        if storage not in self._config.storage_allowlist:
            raise PermissionError("storage is not allowlisted")

    @staticmethod
    def _mapping(data: object) -> Mapping[str, object]:
        if not isinstance(data, dict):
            raise ProxmoxAdapterError(
                "invalid_backend_shape", "backend object was not a mapping"
            )
        return cast(dict[str, object], data)

    @staticmethod
    def _list(data: object) -> list[Mapping[str, object]]:
        if not isinstance(data, list) or any(not isinstance(item, dict) for item in data):
            raise ProxmoxAdapterError(
                "invalid_backend_shape", "backend collection was invalid"
            )
        return [cast(dict[str, object], item) for item in data]

    @staticmethod
    def _pick(
        source: Mapping[str, object], fields: tuple[str, ...]
    ) -> tuple[dict[str, object], tuple[ToolError, ...]]:
        result = {field: source[field] for field in fields if field in source}
        missing = tuple(field for field in fields if field not in source)
        errors = tuple(
            ToolError("missing_field", "backend field was absent", field=field)
            for field in missing
        )
        return result, errors

    def list_guests(self, *, limit: int) -> AdapterResult[list[Mapping[str, object]]]:
        raw = self._list(self._request("/cluster/resources", params={"type": "vm"}))
        allowed: list[Mapping[str, object]] = []
        for item in raw:
            kind = item.get("type")
            vmid = item.get("vmid")
            node = item.get("node")
            if not isinstance(kind, str) or not isinstance(vmid, int) or not isinstance(node, str):
                continue
            if (
                (kind, vmid) not in self._config.guest_allowlist
                or node not in self._config.node_allowlist
            ):
                continue
            normalized, _ = self._pick(
                item,
                (
                    "name",
                    "node",
                    "status",
                    "maxcpu",
                    "maxmem",
                    "maxdisk",
                ),
            )
            normalized["guest_id"] = vmid
            normalized["guest_type"] = kind
            normalized["template"] = bool(item.get("template", 0))
            allowed.append(normalized)
        allowed.sort(
            key=lambda item: (
                str(item.get("guest_type")),
                int(cast(int, item["guest_id"])),
            )
        )
        return AdapterResult(allowed[:limit])

    def get_guest_status(self, target: GuestTarget) -> AdapterResult[Mapping[str, object]]:
        self._require_guest(target)
        node = next(iter(self._config.node_allowlist))
        raw = self._mapping(
            self._request(
                f"/nodes/{node}/{target.guest_type.value}/{target.guest_id}/status/current"
            )
        )
        normalized, errors = self._pick(
            raw,
            (
                "name",
                "status",
                "cpu",
                "cpus",
                "mem",
                "maxmem",
                "disk",
                "maxdisk",
                "uptime",
            ),
        )
        normalized.update(
            {
                "node": node,
                "guest_id": target.guest_id,
                "guest_type": target.guest_type.value,
                "template": bool(raw.get("template", 0)),
            }
        )
        return AdapterResult(normalized, errors)

    def get_node_health(self, node: str) -> AdapterResult[Mapping[str, object]]:
        self._require_node(node)
        raw = self._mapping(self._request(f"/nodes/{node}/status"))
        normalized, errors = self._pick(
            raw, ("cpu", "uptime", "loadavg", "memory", "swap", "rootfs")
        )
        normalized["node"] = node
        return AdapterResult(normalized, errors)

    def get_storage_status(
        self, node: str, storage: str
    ) -> AdapterResult[Mapping[str, object]]:
        self._require_node(node)
        self._require_storage(storage)
        raw = self._mapping(self._request(f"/nodes/{node}/storage/{storage}/status"))
        normalized, errors = self._pick(
            raw, ("active", "enabled", "total", "used", "avail", "type", "content")
        )
        normalized.update({"node": node, "storage": storage})
        return AdapterResult(normalized, errors)

    @staticmethod
    def _normalize_tasks(
        raw: list[Mapping[str, object]], limit: int
    ) -> list[Mapping[str, object]]:
        safe_fields = ("type", "status", "starttime", "endtime", "id", "node")
        normalized: list[Mapping[str, object]] = []
        for item in raw[:limit]:
            task = {field: item[field] for field in safe_fields if field in item and field != "status"}
            raw_status = item.get("status")
            if raw_status == "OK":
                task["status"] = "ok"
            elif raw_status is None:
                task["status"] = "running" if "endtime" not in item else "unknown"
            else:
                task["status"] = "failed"
            normalized.append(task)
        return normalized

    def get_recent_tasks(
        self, node: str, query: BoundedQuery
    ) -> AdapterResult[list[Mapping[str, object]]]:
        self._require_node(node)
        since = int(time.time()) - query.lookback_minutes * 60
        raw = self._list(
            self._request(
                f"/nodes/{node}/tasks",
                params={"start": 0, "limit": query.limit, "since": since},
            )
        )
        return AdapterResult(self._normalize_tasks(raw, query.limit))

    def get_backup_status(
        self, node: str, query: BoundedQuery
    ) -> AdapterResult[list[Mapping[str, object]]]:
        self._require_node(node)
        since = int(time.time()) - query.lookback_minutes * 60
        raw = self._list(
            self._request(
                f"/nodes/{node}/tasks",
                params={
                    "start": 0,
                    "limit": query.limit,
                    "since": since,
                    "typefilter": "vzdump",
                },
            )
        )
        return AdapterResult(self._normalize_tasks(raw, query.limit))
