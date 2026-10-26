# Agent instructions

Read this before changing code. It describes invariants that are load-bearing
for safety, not style preferences.

## What this server is

A **read-only** MCP server over the Proxmox API. It exposes a small, fixed
catalog of tools that answer questions about a homelab or cluster. It has no
capability to change anything, and that is the entire security argument for
running it next to a model.

## Invariants — do not break these

- **No write path.** There is no `POST`, `PUT`, `PATCH`, or `DELETE` anywhere
  in `src/`. Adding one is not a feature increment; it makes every other
  control in this repo meaningless. A write-capable service belongs in a
  separate project with its own identity and its own threat model.
- **No open-ended inputs.** Never accept arbitrary URLs, HTTP routes, shell
  commands, filesystem paths, SQL, templates, or executable code as tool
  input. Targets are constrained to configured allowlists.
- **The tool catalog is exact and finite.** Tools are registered from
  configuration at startup; they are not discovered or synthesized at request
  time.
- **Fail closed.** Absent, empty, zero, or unparseable configuration is an
  error, never a default and never "unlimited". Allowlists reject the empty
  case rather than treating it as "allow all".
- **TLS verification is mandatory.** It is not configurable off. The only
  escape hatch is an explicit `--insecure` flag on the interactive setup
  command, never on the server.
- **Secrets come from files, not the environment.** Credentials are read from
  mode-600 files so they never appear in a process listing, `systemctl show`,
  or `/proc`.
- **Never log or return** secrets, tokens, cookies, private keys, raw model
  prompts, unrestricted upstream response bodies, or hardware serials.
- **Preserve the negative paths.** A large share of the suite exists to prove
  things are *refused*. Deleting a denial test to make a change pass is a
  regression, whatever the suite says afterwards.

## A note on `discovery.py`

The setup crawl reads more broadly than the running server is permitted to —
you cannot build an allowlist from inside an allowlist. It is `GET`-only,
invoked by a human from a shell, and is **never registered as an MCP tool**.
Keep it that way: if a model can reach discovery, the allowlist stops being a
boundary.

## Validation

```bash
uv sync --locked
uv run ruff check .
uv run mypy src tests
uv run pytest
```

All four must pass before a change lands. Keep coverage deterministic — no
network, no credentials, and no wall-clock dependence in the suite.
