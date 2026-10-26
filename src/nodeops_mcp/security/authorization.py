"""Deny-by-default tool and target authorization helpers."""

from dataclasses import dataclass

from nodeops_mcp.catalog import FIXED_TOOL_NAMES
from nodeops_mcp.schemas import GuestTarget


@dataclass(frozen=True, slots=True)
class AuthorizationPolicy:
    allowed_nodes: frozenset[str]
    allowed_guests: frozenset[tuple[str, int]]
    allowed_storage: frozenset[str]

    def require_tool(self, tool: str) -> None:
        if tool not in FIXED_TOOL_NAMES:
            raise PermissionError("tool is not in the fixed catalog")

    def require_node(self, node: str) -> None:
        if node not in self.allowed_nodes:
            raise PermissionError("node is not allowlisted")

    def require_guest(self, target: GuestTarget) -> None:
        key = (target.guest_type.value, target.guest_id)
        if key not in self.allowed_guests:
            raise PermissionError("guest is not allowlisted")

    def require_storage(self, storage: str) -> None:
        if storage not in self.allowed_storage:
            raise PermissionError("storage is not allowlisted")
