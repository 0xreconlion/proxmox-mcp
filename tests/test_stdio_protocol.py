from __future__ import annotations

import sys

import pytest
from mcp.client import Client
from mcp.client.stdio import StdioServerParameters

from nodeops_mcp import __version__, fixtures
from nodeops_mcp.catalog import FIXED_TOOL_NAMES


@pytest.mark.anyio
async def test_stdio_auto_mode_negotiates_current_protocol() -> None:
    parameters = StdioServerParameters(
        command=sys.executable,
        args=["-m", "nodeops_mcp.server", "--transport", "stdio"],
    )
    async with Client(parameters, mode="auto") as client:
        tools = await client.list_tools()
        result = await client.call_tool("get_node_health", {"node": fixtures.NODE})

        assert client.protocol_version == "2026-07-28"
        assert client.server_info is not None
        assert client.server_info.name == "NodeOps Lab"
        assert client.server_info.version == __version__
        assert {tool.name for tool in tools.tools} == FIXED_TOOL_NAMES
        assert result.is_error is False
        assert result.structured_content["source"] == "test_fixture"
