# Documentation

Start with the [top-level README](../README.md) — it covers install,
configuration, and the tool catalog.

| Topic | Where |
| --- | --- |
| Install, configure, run | [`../README.md`](../README.md) |
| Every configuration key, annotated | [`../.env.example`](../.env.example) |
| systemd units and Proxmox token teardown | [`../deployment/README.md`](../deployment/README.md) |
| Safety invariants, and what not to change | [`../AGENTS.md`](../AGENTS.md) |
| Release history | [`../CHANGELOG.md`](../CHANGELOG.md) |

## Design notes

**Why allowlists are types.** Node and storage names are compiled into the
advertised MCP tool schema at startup, not merely checked at request time. A
model is therefore constrained by the schema it is shown *and* by the
server-side authorization layer. The schema narrows what gets asked for; the
authorization layer is what actually enforces it. Neither alone is the control.

**Why there is no write path.** A read-only server next to a model is a
reviewable risk. A write-capable one is a different product with a different
threat model, and bolting mutation onto this codebase would invalidate the
reasoning behind every other control here. See [`../AGENTS.md`](../AGENTS.md).

**Why setup discovery reads more than the server can.** You cannot build an
allowlist from inside an allowlist. `discovery.py` is `GET`-only, human-invoked,
and never registered as an MCP tool.
