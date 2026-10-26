"""Setup CLI: discover your Proxmox, check your config, or run a safe simulation.

Three subcommands, in the order you will want them:

    proxmox-mcp simulate   # run with zero credentials and see the six tools work
    proxmox-mcp discover   # crawl your Proxmox and propose allowlists
    proxmox-mcp check      # validate the config you ended up with

There is deliberately no `--token-secret` flag. A secret passed on a command line
lands in your shell history and is visible to every other process via `ps`. Pass
a file path, use the environment, or let the prompt read it.
"""

from __future__ import annotations

import argparse
import os
import sys
from getpass import getpass
from pathlib import Path

from proxmox_mcp.discovery import DiscoveryError, discover


def _read_secret(args: argparse.Namespace) -> str:
    if args.token_secret_file:
        path = Path(args.token_secret_file).expanduser()
        if not path.is_file():
            raise DiscoveryError(f"token secret file not found: {path}")
        return path.read_text(encoding="utf-8").strip()
    from_env = os.environ.get("PROXMOX_TOKEN_SECRET", "").strip()
    if from_env:
        return from_env
    if not sys.stdin.isatty():
        raise DiscoveryError(
            "no token secret available. Pass --token-secret-file, or set "
            "PROXMOX_TOKEN_SECRET, or run interactively."
        )
    secret = getpass("Proxmox API token secret (not echoed): ").strip()
    if not secret:
        raise DiscoveryError("token secret was empty")
    return secret


def _cmd_discover(args: argparse.Namespace) -> int:
    found = discover(
        base_url=args.base_url,
        token_id=args.token_id,
        token_secret=_read_secret(args),
        ca_file=Path(args.ca_file).expanduser() if args.ca_file else None,
        insecure=args.insecure,
    )
    for warning in found.warnings:
        print(f"warning: {warning}", file=sys.stderr)

    print(f"\n# Discovered {len(found.nodes)} node(s), "
          f"{len(found.storages)} storage(s), {len(found.guests)} guest(s).\n")
    for guest in sorted(found.guests, key=lambda g: g["guest_id"]):
        print(f"#   {guest['kind']}:{guest['guest_id']:<7} "
              f"{guest['status']:<9} {guest['name']} (node {guest['node']})")

    print("\n# ---- review and trim this, then put it in your .env ----")
    print("# Every entry below is something the server will be permitted to read.")
    print("# Narrower is better: delete anything you do not want exposed.\n")
    if args.format == "json":
        print(found.as_json(running_only=not args.include_stopped))
    else:
        print(found.as_env(running_only=not args.include_stopped))
    print()
    if args.include_stopped:
        print("# (stopped guests included because --include-stopped was passed)")
    else:
        print("# (stopped guests and template/test-named guests omitted; "
              "pass --include-stopped to include them)")
    return 0


def _cmd_check(args: argparse.Namespace) -> int:
    del args
    try:
        from proxmox_mcp.config import load_live_config
    except ImportError as error:  # pragma: no cover - defensive
        print(f"could not import configuration loader: {error}", file=sys.stderr)
        return 2
    try:
        config = load_live_config()
    except ValueError as error:
        print(f"FAIL  configuration is not usable:\n      {error}", file=sys.stderr)
        print("\nRun `proxmox-mcp discover` to generate the allowlists, then set the "
              "PROXMOX_MCP_* variables shown in .env.example.", file=sys.stderr)
        return 1
    print("PASS  configuration loaded and validated")
    print(f"      base URL   : {config.proxmox_base_url}")
    print(f"      nodes      : {', '.join(sorted(config.node_allowlist))}")
    print(f"      storages   : {', '.join(sorted(config.storage_allowlist)) or '(none)'}")
    print(f"      guests     : {len(config.guest_allowlist)} allowlisted")
    print(f"      bind       : {os.environ.get('PROXMOX_MCP_HTTP_HOST', '127.0.0.1')}:"
          f"{os.environ.get('PROXMOX_MCP_HTTP_PORT', '8000')}")
    if not config.storage_allowlist:
        print("\nWARN  no storage allowlisted; get_storage_status cannot be registered")
        return 1
    return 0


def _cmd_simulate(args: argparse.Namespace) -> int:
    from mcp.server import MCPServer

    from proxmox_mcp.service import build_simulated_service
    from proxmox_mcp.tools import register_tools

    service = build_simulated_service()
    server: MCPServer[object] = MCPServer(name="proxmox-mcp-simulated", version="0.0.0")
    register_tools(server, service)
    print("Simulated server built with no credentials and no network access.")
    print(f"  nodes    : {', '.join(service.allowed_nodes)}")
    print(f"  storages : {', '.join(service.allowed_storage)}")
    print("\nRunning on stdio. Point an MCP client at this process, or Ctrl-C to exit.")
    if args.dry_run:
        print("(--dry-run: exiting without serving)")
        return 0
    server.run(transport="stdio")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="proxmox-mcp",
        description="Setup helper for the read-only Proxmox MCP server.",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    d = sub.add_parser("discover", help="crawl your Proxmox and propose allowlists")
    d.add_argument("--base-url", required=True,
                   help="Proxmox API origin, e.g. https://proxmox.lan:8006")
    d.add_argument("--token-id", required=True, help="API token id: user@realm!tokenname")
    d.add_argument("--token-secret-file", help="file containing the token secret")
    d.add_argument("--ca-file", help="CA bundle for your Proxmox certificate")
    d.add_argument("--insecure", action="store_true",
                   help="skip TLS verification for discovery only (not for serving)")
    d.add_argument("--include-stopped", action="store_true",
                   help="also propose stopped guests")
    d.add_argument("--format", choices=("env", "json"), default="env")
    d.set_defaults(handler=_cmd_discover)

    c = sub.add_parser("check", help="validate the current configuration")
    c.set_defaults(handler=_cmd_check)

    s = sub.add_parser("simulate", help="run the credential-free simulated server")
    s.add_argument("--dry-run", action="store_true",
                   help="build and report, but do not serve")
    s.set_defaults(handler=_cmd_simulate)

    args = parser.parse_args(argv)
    try:
        return int(args.handler(args))
    except DiscoveryError as error:
        print(f"error: {error}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:  # pragma: no cover
        return 130


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
