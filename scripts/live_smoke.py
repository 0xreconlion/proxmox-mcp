"""Live-read smoke test. Requires a complete, validated protected configuration.

Reads the first node and every allowlisted guest from your configuration, so it
exercises only targets you have already approved. Run `proxmox-mcp check` first.
"""

from __future__ import annotations

import json

from nodeops_mcp.config import load_live_config
from nodeops_mcp.schemas import BoundedQuery, GuestTarget, GuestType, envelope_to_dict
from nodeops_mcp.service import build_live_service


def main() -> None:
    config = load_live_config()
    service = build_live_service(config)
    results: dict[str, object] = {}
    results["list_guests"] = envelope_to_dict(service.list_guests(limit=50))
    results["get_node_health"] = envelope_to_dict(
        service.get_node_health(next(iter(config.node_allowlist)))
    )
    for kind, guest_id in sorted(config.guest_allowlist):
        target = GuestTarget(GuestType(kind), guest_id)
        results[f"get_guest_status:{kind}:{guest_id}"] = envelope_to_dict(
            service.get_guest_status(target)
        )
    node = next(iter(config.node_allowlist))
    for storage in sorted(config.storage_allowlist):
        results[f"get_storage_status:{storage}"] = envelope_to_dict(
            service.get_storage_status(node=node, storage=storage)
        )
    query = BoundedQuery(limit=20, lookback_minutes=10_080)
    results["get_recent_tasks"] = envelope_to_dict(
        service.get_recent_tasks(node=node, query=query)
    )
    results["get_backup_status"] = envelope_to_dict(
        service.get_backup_status(node=node, query=query)
    )
    print(json.dumps(results, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
