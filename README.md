# proxmox-mcp — a read-only MCP server for Proxmox VE

Give a local LLM safe, bounded, **read-only** visibility into your Proxmox host, so it can help
you understand what is going on without being able to change anything.


Fork or clone it, run a crawl against your own cluster, review what it proposes, and you have a
working MCP server scoped to exactly the nodes, storages, and guests you chose.

```
proxmox-mcp simulate --dry-run   # 1. try it with zero credentials
proxmox-mcp discover ...         # 2. crawl your Proxmox, get proposed allowlists
proxmox-mcp check                # 3. validate what you configured
nodeops-mcp --transport stdio    # 4. serve it
```

Steps 1–5 below are that list, in order.

## What it can and cannot do

Six tools, and that is the entire surface:

| Tool | Returns |
|---|---|
| `list_guests` | allowlisted guests, type, purpose, runtime state |
| `get_guest_status` | bounded runtime state for one allowlisted guest |
| `get_node_health` | CPU, memory, load, uptime, status |
| `get_storage_status` | capacity and active state for one allowlisted storage |
| `get_recent_tasks` | capped task summaries |
| `get_backup_status` | capped backup outcomes and consistency state |

**It cannot mutate anything.** This is a design property, not a policy you can toggle:

- the HTTP adapter contains no `POST`, `PUT`, or `DELETE` — there is no write path in the code
- the Proxmox token you create needs read privileges only, so mutation is refused server-side too
- every node, storage, and guest must be explicitly allowlisted; unlisted targets are denied
  before any backend call
- results are size- and count-bounded, so a tool cannot dump your whole task log into a context
  window
- guest-supplied text (names, descriptions) is treated as untrusted input, because it reaches the
  model through tool output

If you want a *remediation* tool that can restart units or prune backups, this is not that, and
you should not extend it into that casually. See "Why read-only" below.

## Requirements

- **Proxmox VE 8 or 9** with API access reachable from wherever you run this
- **Python 3.11+**
- **[uv](https://docs.astral.sh/uv/)** (recommended) or pip
- **git**
- **A local model runner** if you want fully local operation — [Ollama](https://ollama.com),
  LM Studio, llama.cpp, or anything that speaks MCP. The server itself has no model dependency;
  it is just an MCP server, so any MCP-capable client works.

## 1. Try it with no credentials

Before you create any API token, run the simulated server. It uses invented fixtures, performs no
network calls, and lets you confirm the six tools behave the way you expect.

```sh
git clone https://github.com/0xreconlion/proxmox-mcp.git
cd proxmox-mcp
uv sync
uv run proxmox-mcp simulate --dry-run
```

To actually drive it from a client, drop `--dry-run` and it serves over stdio.

## 2. Create a read-only Proxmox token

In the Proxmox UI, or on the host:

```sh
pveum user add mcp@pve
pveum acl modify / --users mcp@pve --roles PVEAuditor
pveum user token add mcp@pve reader --privsep 1
```

Copy the printed secret into a protected file. **Do not paste it into a shell command** — it would
land in your history and be visible to other processes:

```sh
install -m 600 /dev/null ~/.config/proxmox-mcp/token-secret
$EDITOR ~/.config/proxmox-mcp/token-secret
```

`PVEAuditor` on `/` is what the discovery crawl needs. You can narrow the ACL afterwards to only
the paths you ended up allowlisting.

## 3. Crawl your own Proxmox

```sh
uv run proxmox-mcp discover \
  --base-url https://proxmox.lan:8006 \
  --token-id 'mcp@pve!reader' \
  --token-secret-file ~/.config/proxmox-mcp/token-secret \
  --ca-file ~/.config/proxmox-mcp/proxmox-ca.pem
```

It issues read-only `GET` requests and prints what it found plus a proposed configuration:

```
#   lxc:200     running   proxy (node hv-a)
#   qemu:110    running   app (node hv-a)
#   lxc:201     stopped   old (node hv-b)

NODEOPS_PROXMOX_NODE_ALLOWLIST=hv-a,hv-b
NODEOPS_PROXMOX_STORAGE_ALLOWLIST=local,nas-backups,ssd-pool
NODEOPS_PROXMOX_GUEST_ALLOWLIST=lxc:200,qemu:110
```

**Review and trim this.** Everything you leave in is something the model will be able to read.
Narrower is better. Stopped guests and anything named like a template or test are omitted by
default; pass `--include-stopped` if you want them.

Discovery deliberately reads more broadly than the running server is permitted to — you cannot
build an allowlist from inside an allowlist. It is a one-time setup command you run from a shell,
is never registered as an MCP tool, and is never reachable by a model.

If your Proxmox uses a self-signed certificate, export its CA and pass `--ca-file`. `--insecure`
exists for discovery only and warns loudly; do not serve that way.

## 4. Configure and check

Copy `.env.example`, paste in the block discovery gave you, then:

```sh
uv run proxmox-mcp check
```

It either prints the configuration it validated, or tells you exactly which setting is missing.
There are no silent defaults for anything infrastructure-specific — if a setting is absent the
server refuses to start rather than guessing.

## 5. Serve it

```sh
# stdio, for a local MCP client on the same machine
uv run nodeops-mcp --transport stdio

# or bounded loopback HTTP
uv run nodeops-mcp --transport streamable-http --host 127.0.0.1 --port 8000
```

`deployment/systemd/` has hardened unit templates if you want it supervised. They ship with
`IPAddressDeny=any` and no host address filled in, so you must set your own — that fails loudly
instead of silently inheriting someone else's network.

## The advertised schema follows your configuration

The tool schema a client sees is generated from your allowlists, so a model is only ever invited
to ask for targets you permitted:

```json
"get_storage_status": {
  "storage": {"enum": ["nas-backups", "ssd-pool"]},
  "node":    {"enum": ["hv-a", "hv-b"]}
}
```

Runtime authorization is still the enforcing check. The enum narrows what gets asked for; the
deny-by-default policy behind it is what actually stops anything.

## Parameters

Precedence is config file, then `NODEOPS_*` environment variables, then CLI flags. See
`.env.example` for the full list. The ones you will actually touch:

| Setting | Meaning |
|---|---|
| `NODEOPS_PROXMOX_BASE_URL` | your Proxmox origin, HTTPS, no path |
| `NODEOPS_PROXMOX_NODE_ALLOWLIST` | comma-separated node names |
| `NODEOPS_PROXMOX_STORAGE_ALLOWLIST` | comma-separated storage names |
| `NODEOPS_PROXMOX_GUEST_ALLOWLIST` | comma-separated `kind:id`, e.g. `qemu:110,lxc:200` |
| `NODEOPS_PROXMOX_TOKEN_ID_FILE` | file holding `user@realm!tokenname` |
| `NODEOPS_PROXMOX_TOKEN_SECRET_FILE` | file holding the secret, mode `600` |
| `NODEOPS_PROXMOX_CA_FILE` | CA bundle for your Proxmox certificate |
| `NODEOPS_HTTP_HOST` / `_PORT` / `_PATH` | HTTP transport binding |

Transport bounds (request size, concurrency, rate) are deliberately not runtime knobs. Raising
them widens what a client can push at the server before authentication, so they live in code
where a change is reviewable.

## Why read-only

The interesting failure mode of an LLM with infrastructure access is not that it is wrong; it is
that it is confidently wrong while holding a credential that can act. This server is built so the
worst outcome of a bad model response is a misleading sentence, not a stopped VM.

That boundary is enforced in several independent places on purpose, so removing it is not a
one-line change. If you do want remediation, the pattern that preserves this property is: a fixed
catalog of named, parameter-validated actions; the model may only select a catalog entry, never
emit a command; a human approves each action; and a **separate**, narrowly-scoped write credential
that is revocable on its own. Do not widen the read token.

## Development

```sh
uv sync
uv run pytest        # 176 tests
uv run ruff check .
uv run mypy src tests
```

## License

[MIT](LICENSE).
