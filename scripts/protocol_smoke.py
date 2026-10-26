"""Sanitized deployed MCP protocol and live-read smoke test."""

from __future__ import annotations

import argparse
import ssl
from pathlib import Path

import anyio
import httpx2
from mcp.client import Client
from mcp.client.streamable_http import streamable_http_client


async def exercise(client: Client, node: str) -> None:
    tools = await client.list_tools()
    result = await client.call_tool("get_node_health", {"node": node})
    assert client.server_info is not None
    assert result.structured_content is not None
    print(
        {
            "protocol": client.protocol_version,
            "server": client.server_info.name,
            "version": client.server_info.version,
            "tools": sorted(tool.name for tool in tools.tools),
            "is_error": result.is_error,
            "status": result.structured_content["status"],
            "source": result.structured_content["source"],
        }
    )


async def smoke(url: str, node: str, token_file: Path | None, ca_file: Path | None) -> None:
    if token_file is None:
        async with Client(url, mode="auto") as client:
            await exercise(client, node)
        return
    if ca_file is None:
        raise ValueError("--ca-file is required with --token-file")
    token = token_file.read_text(encoding="utf-8").strip()
    context = ssl.create_default_context(cafile=str(ca_file))
    async with httpx2.AsyncClient(
        headers={"Authorization": f"Bearer {token}"}, verify=context
    ) as http_client:
        transport = streamable_http_client(url, http_client=http_client)
        async with Client(transport, mode="auto") as client:
            await exercise(client, node)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("url")
    parser.add_argument(
        "--node",
        required=True,
        help="node name to probe; must be in your PROXMOX_MCP_NODE_ALLOWLIST",
    )
    parser.add_argument("--token-file", type=Path)
    parser.add_argument("--ca-file", type=Path)
    args = parser.parse_args()
    anyio.run(smoke, args.url, args.node, args.token_file, args.ca_file)


if __name__ == "__main__":
    main()
