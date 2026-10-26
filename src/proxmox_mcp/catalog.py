"""Finite v1 tool catalog. No runtime registration occurs in this scaffold."""

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class ToolSpec:
    name: str
    purpose: str


FIXED_TOOL_CATALOG: tuple[ToolSpec, ...] = (
    ToolSpec("list_guests", "List allowlisted guest identity and runtime state."),
    ToolSpec("get_guest_status", "Return bounded runtime status for one allowlisted guest."),
    ToolSpec("get_node_health", "Return bounded health metrics for the configured node."),
    ToolSpec("get_storage_status", "Return capacity and availability for allowlisted storage."),
    ToolSpec("get_recent_tasks", "Return bounded safe summaries of recent tasks."),
    ToolSpec("get_backup_status", "Return bounded observed backup outcomes and provenance."),
)

FIXED_TOOL_NAMES = frozenset(tool.name for tool in FIXED_TOOL_CATALOG)
