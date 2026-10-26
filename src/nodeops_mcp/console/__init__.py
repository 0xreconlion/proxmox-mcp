"""Owner-only loopback console contracts for Tailscale Serve."""

from nodeops_mcp.console.app import ConsoleConfig, build_console_app
from nodeops_mcp.console.data import ConsoleObservation, ConsoleSnapshot
from nodeops_mcp.console.models import ConsoleIncident, ConsoleIncidentRepository

__all__ = [
    "ConsoleConfig",
    "ConsoleIncident",
    "ConsoleIncidentRepository",
    "ConsoleObservation",
    "ConsoleSnapshot",
    "build_console_app",
]
