# Deployment

Three systemd units ship here. Pick one; they are not meant to be combined.

| Unit | Purpose | Binds |
| --- | --- | --- |
| `systemd/nodeops-mcp-lab.service` | Credential-free trial run. No Proxmox token, no network egress. | `127.0.0.1:8766/mcp` |
| `systemd/nodeops-mcp.service.template` | Production transport. Requires credentials and TLS inputs. | your choice |
| `systemd/nodeops-console.service.template` | Optional read-only operations console. | `127.0.0.1:8421` |

Both `.template` files contain `[EDIT-REQUIRED]` markers. Copy, fill them in,
then install:

```bash
grep -n 'EDIT-REQUIRED' systemd/nodeops-mcp.service.template
```

## Rules that are not tuning knobs

- **Never add a reverse-proxy route, public listener, or Proxmox credential to
  the lab unit.** Its whole value is that it cannot reach anything.
- **Never bind the console to a LAN address or put it behind a public CDN.** It
  trusts identity headers only from loopback; exposing it directly makes those
  headers forgeable. Front it with an authenticated tunnel or don't run it.
- The HTTP limits (64 KiB request bodies, eight concurrent requests, 120
  accepted requests per rolling 60-second window) are enforced in the
  application *in addition to* systemd and firewall controls. Changing them
  needs a new test cycle, not an edit.

## Proxmox token teardown order

Temporary and replacement Proxmox API tokens must be retired in this order:

1. Remove every ACL binding that names the token with `pveum acl delete`.
2. Confirm `pveum acl list` completes without an invalid-token warning.
3. Delete the token with `pveum user token delete`.
4. Confirm the retired credential returns 401 and the active identity still
   completes an approved read.

Deleting a token object before its ACL bindings leaves an invalid principal in
`/etc/pve/user.cfg`. On Proxmox VE 9.2.2, that state can make ACL enumeration
stall after warning, and the supported ACL command then refuses to remove the
orphan because the token no longer exists. Do not use the reverse order.

If an older orphan is discovered: stop automation, back up `user.cfg`, remove
only the exact orphaned principal, verify the remaining ACL line, and rerun both
the parser and active-identity checks before continuing.
