"""Fixed MCP tool registration with typed and bounded arguments.

The six-tool catalog is fixed. What is *not* fixed is which node and storage
names are legal: those are enumerated from this deployment's allowlists at
registration time, so the advertised MCP schema shows a client exactly the
targets this deployment permits and nothing else.

Runtime authorization in `AuthorizationPolicy` remains the enforcing check. The
generated enum narrows what a client is invited to ask for; it never replaces
the deny-by-default check behind it.
"""

from collections.abc import Callable
from typing import Annotated, Any, Literal

from mcp.server import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from pydantic import Field

from proxmox_mcp.schemas import (
    BoundedQuery,
    GuestTarget,
    GuestType,
    envelope_to_dict,
)
from proxmox_mcp.service import ProxmoxService

Limit = Annotated[int, Field(ge=1, le=50)]
LookbackMinutes = Annotated[int, Field(ge=1, le=43_200)]
GuestId = Annotated[int, Field(gt=0, le=999_999)]


def register_tools(server: MCPServer[object], service: ProxmoxService) -> None:
    """Register the fixed six-tool catalog against this deployment's allowlists."""

    nodes = service.allowed_nodes
    storages = service.allowed_storage
    if not nodes:
        raise ValueError(
            "at least one node must be allowlisted; set PROXMOX_MCP_NODE_ALLOWLIST "
            "or run `proxmox-mcp discover` to generate it"
        )
    if not storages:
        raise ValueError(
            "at least one storage must be allowlisted; set "
            "PROXMOX_MCP_STORAGE_ALLOWLIST or run `proxmox-mcp discover`"
        )

    # Dynamic Literal generation: Literal[tuple] produces a real Literal type, and
    # assigning it into __annotations__ before registration makes the generated MCP
    # input schema carry an exact `enum` of permitted values.
    node_type = Literal[nodes]  # type: ignore[valid-type]
    storage_type = Literal[storages]  # type: ignore[valid-type]
    default_node = nodes[0]

    def register(function: Callable[..., Any], annotations: dict[str, object]) -> None:
        function.__annotations__.update(annotations)
        server.tool(structured_output=True)(function)

    def list_guests(limit: Limit = 50) -> dict[str, object]:
        """List allowlisted guests with observed time, type, purpose, and runtime state."""
        return envelope_to_dict(service.list_guests(limit=limit))

    register(list_guests, {})

    def get_guest_status(
        guest_type: Literal["qemu", "lxc"], guest_id: GuestId
    ) -> dict[str, object]:
        """Return bounded runtime state for one exact allowlisted guest."""
        target = GuestTarget(GuestType(guest_type), guest_id)
        try:
            return envelope_to_dict(service.get_guest_status(target))
        except PermissionError as exc:
            raise ToolError("request denied by policy") from exc

    register(get_guest_status, {})

    def get_node_health(node: str = default_node) -> dict[str, object]:
        """Return CPU, memory, load, uptime, and status for an allowlisted node."""
        try:
            return envelope_to_dict(service.get_node_health(node))
        except PermissionError as exc:
            raise ToolError("request denied by policy") from exc

    register(get_node_health, {"node": node_type})

    def get_storage_status(storage: str, node: str = default_node) -> dict[str, object]:
        """Return capacity and active state for one allowlisted storage target."""
        try:
            return envelope_to_dict(service.get_storage_status(node=node, storage=storage))
        except PermissionError as exc:
            raise ToolError("request denied by policy") from exc

    register(get_storage_status, {"storage": storage_type, "node": node_type})

    def get_recent_tasks(
        limit: Limit = 20,
        lookback_minutes: LookbackMinutes = 1440,
        node: str = default_node,
    ) -> dict[str, object]:
        """Return capped safe task summaries; unrestricted task logs are unavailable."""
        query = BoundedQuery(limit=limit, lookback_minutes=lookback_minutes)
        try:
            return envelope_to_dict(service.get_recent_tasks(node=node, query=query))
        except PermissionError as exc:
            raise ToolError("request denied by policy") from exc

    register(get_recent_tasks, {"node": node_type})

    def get_backup_status(
        limit: Limit = 20,
        lookback_minutes: LookbackMinutes = 10_080,
        node: str = default_node,
    ) -> dict[str, object]:
        """Return capped backup outcomes, provenance, and observed consistency state."""
        query = BoundedQuery(limit=limit, lookback_minutes=lookback_minutes)
        try:
            return envelope_to_dict(service.get_backup_status(node=node, query=query))
        except PermissionError as exc:
            raise ToolError("request denied by policy") from exc

    register(get_backup_status, {"node": node_type})
