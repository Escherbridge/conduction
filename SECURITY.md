# Security

Read this before exposing Conduction to anything.

## What Conduction does

Conduction launches AI agents that execute against repositories on the machine
running it. Agents are granted tools including `Read`, `Write`, `Edit` and
`Bash`, and gate checks run arbitrary subprocesses. **Anyone who can make
Conduction start a mission can run code on the host.**

That is the intended behaviour, not a flaw. It is also why the access model
below matters more than it would for an ordinary web app.

## The trust model

Conduction has **no authentication**. There are no accounts, no passwords, no
sessions, no API keys. Access is decided by *where the request came from*:

| Scope | Who | May do |
|---|---|---|
| `local` | Loopback (`127.0.0.0/8`, `::1`) | Everything |
| `remote` | A peer inside `CONDUCTION_TRUSTED_CIDRS` | Observe, steer, re-run — an explicit allowlist |
| `denied` | Everyone else | Nothing |

The default bind is `127.0.0.1`, and **remote scope is off unless you turn it
on**. A fresh install is reachable only from the machine it runs on.

Scope is computed from the real socket peer (`request.ip`), never from
`X-Forwarded-For` or any other caller-supplied header.

## Enabling remote access

Intended for a private network you control — Tailscale, WireGuard, or similar.

```bash
export CONDUCTION_REMOTE_ACCESS=1
export CONDUCTION_TRUSTED_CIDRS="100.x.y.0/24"   # your tailnet prefix
export CONDUCTION_ALLOWED_HOSTS="your-host.ts.net"
export CONDUCTION_HOST=0.0.0.0
```

**There is no default trusted range, deliberately.** `100.64.0.0/10` is the
whole carrier-grade NAT block, not "your tailnet" — a mobile hotspot neighbour
or a container network can sit inside it. Name your own prefix.

Do not put Conduction on the public internet. It has no authentication to
survive that, and a tunnel with an identity proxy in front is the minimum if
you must reach it from outside a private network.

## What remote scope may do

An **allowlist**, so an endpoint added later is remote-denied until someone
lists it deliberately.

Allowed: read runs, events, findings, manifests, costs and fleet status;
`interrupt` a running mission; `resume`-free `rerun` of an existing run.

Denied, and why:

- **`POST /api/runs`** — accepts arbitrary agent briefs, tools and `owns`
  paths. Allowing it would let a remote client author new work. The bounded
  alternative is `POST /api/runs/<id>/rerun`, which rebuilds the mission from
  the manifest already on disk and takes nothing from the caller.
- **`POST /api/runs/<id>/resume`** — builds its agents from the request body,
  so it has the same power as launching. Local only.
- **`/api/fs/*`** — enumerates the host filesystem. Local only regardless of
  network posture.
- **`GET /api/projects/<key>`** — returns a project document verbatim,
  including webhook secrets and absolute host paths.

## Browser-borne attacks

Scope identifies the *machine*, not the user. A browser on the host is
`127.0.0.1`, so every page it visits would otherwise speak with local
authority. Two checks prevent that:

- **Host header allowlist** — blocks DNS rebinding. A page at `evil.com`
  re-resolved to `127.0.0.1` still sends `Host: evil.com`.
- **Origin / `Sec-Fetch-Site` check on writes** — blocks cross-site form POSTs.

## Webhooks

Outbound notifications are signed with HMAC-SHA256 over the exact body
(`X-Conduction-Signature: sha256=…`). Verify it before trusting a delivery.

**A target repository's `.agentgraph/project.json` is a versioned file**, so a
repository you clone can ship webhook subscriptions. Conduction therefore
ignores repo-supplied webhooks unless you set
`CONDUCTION_TRUST_PROJECT_WEBHOOKS=1`, and even then refuses destinations that
resolve to loopback, private, link-local or reserved addresses, and does not
follow redirects. Your own subscriptions in `ecosystem.json` are trusted and
may point anywhere, including services on your own machine.

Note that a cloned repository's `project.json` also supplies **policy rules**,
which are prepended to every agent brief. Treat a repo you did not write as
untrusted input to your agents.

## Reporting a vulnerability

Open a private security advisory on the GitHub repository rather than a public
issue.

## Known limitations

- No authentication, by design. Scope is network position only.
- No audit log of who did what; there is no "who".
- `.agentgraph/` inside a target repository is trusted for policy and
  manifests.
