# Changelog

All notable changes to this project are documented here. This project adheres
to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## Unreleased — 0.1.0

First public release. This repository is a generalization of a private,
single-cluster deployment; node, storage, and guest targets that were
previously compile-time constants are now runtime configuration, which is what
makes the server usable by anyone other than its original author.

### Added

- **Six read-only MCP tools** over the Proxmox API: `list_guests`,
  `get_guest_status`, `get_node_health`, `get_storage_status`,
  `get_recent_tasks`, and `get_backup_status`. The catalog is fixed and finite.
- **Configuration-driven allowlists.** Nodes, storages, and guests are supplied
  via `PROXMOX_MCP_*_ALLOWLIST` and compiled into the advertised tool schema
  at startup, so a model is constrained by the schema it is shown as well as by
  the server-side authorization layer. Empty allowlists are rejected rather than
  interpreted as "allow everything".
- **`proxmox-mcp discover`**, a `GET`-only setup crawl that proposes allowlists
  from your own cluster so you never hand-write them. It is interactive tooling,
  never registered as an MCP tool.
- **`proxmox-mcp check`**, a preflight that validates configuration and prints
  exactly what the server will be permitted to read. Exits non-zero on bad
  config, so it works in a deploy script.
- **`proxmox-mcp simulate`**, a credential-free simulated server for trying the
  tool surface with no cluster and no network access.
- **Fixed-origin HTTPS Proxmox adapter** with mandatory CA verification, bounded
  responses, normalized task outcomes, and finite safe errors that do not leak
  upstream status bodies.
- **Authenticated transport.** Constant-time bearer authentication through the
  MCP SDK auth boundary, tokens bound to the configured resource and scope, and
  exact Host/Origin validation with no wildcards.
- **Explicit limits:** request body cap, response read cap, connect/read timeout
  budgets, concurrency ceiling, and a per-process rate limit. All are required to
  be positive; zero is a configuration error.
- **Secret-safe JSON audit events** for allowed, denied, partial, and
  backend-failure outcomes, with bounded correlation fields and no secret
  material.
- **Deterministic health rules** with healthy/warning/critical/unknown/stale
  states, confirmation windows, freshness budgets, and hysteresis, so a single
  threshold spike does not alert.
- **Incident lifecycle** with stable dedupe keys, typed states, reminder
  cooldowns, maintenance suppression, and protection against clearing unknown or
  stale incidents.
- **A capability-free recommendation scaffold** with typed evidence, labeled
  inference, explicit confidence/uncertainty, and fail-closed output validation
  that rejects invented evidence and instruction-like text.
- **A no-store `/health` liveness route** exposing only service name, version,
  and process status, performing no backend query.
- **Hardened systemd units** and a `.env.example` annotated with
  `[EDIT-REQUIRED]` / `[EDIT-OPTIONAL]` markers, greppable so you can confirm
  your configuration is complete.
- **MIT license.**
- 125 tests, a large share of which assert that unsafe operations are *refused*.

### Deliberately absent

- **Any write capability.** There is no `POST`, `PUT`, `PATCH`, or `DELETE` in
  `src/`. This server cannot change your cluster, by construction.
- **Any bundled model provider, endpoint, or credential.** The recommendation
  and evaluation scaffolds are disconnected; nothing calls out to a model.
- **An operations console.** The original deployment had a web console, but it
  was built around one specific single-operator setup — a single hardcoded
  identity, a particular tunnel topology, and assumptions about the host it ran
  on. Generalizing it would have meant inventing an auth model rather than
  shipping one that had been tested, so it was cut instead. If you want a UI,
  point an existing MCP client at this server.
- **Notification delivery.** The original deployment mailed incident state
  through a provider-specific worker that nobody else would have. Rather than
  ship a seam wired to infrastructure you don't run, the delivery code was
  removed and the **decision logic kept**: `monitoring/` still computes whether
  a notification is due — `DeliveryAction.SEND_STATE_CHANGE`, `SEND_REMINDER`,
  `SUPPRESSED_MAINTENANCE`, `NONE` — with dedupe keys, reminder cooldowns, and
  monotonic delivery timestamps via `mark_delivery`. That is the hard part and
  it is provider-independent. Wire it to whatever you actually use: read the
  decision, send the message, call `mark_delivery`.
