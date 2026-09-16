"""Request scope: who is asking, and what that scope is allowed to do.

The first access-control layer in the app. Rationale, the threat model and the
reasoning behind default-deny live in AGENTS.md -- see "access scopes".
"""

from __future__ import annotations

import ipaddress
import os
import re
from urllib.parse import urlsplit

from sanic.response import json as sanic_json

SCOPE_LOCAL = "local"
SCOPE_REMOTE = "remote"
SCOPE_DENIED = "denied"


def remote_access_enabled() -> bool:
    """Remote scope is opt-in. Installing Conduction opens nothing."""
    return os.environ.get("CONDUCTION_REMOTE_ACCESS", "0").strip() == "1"


def assume_local_peer() -> bool:
    """Treat a peerless request as local. Test hook only.

    `request.ip` is empty for a UNIX-socket peer and whenever Sanic has no
    conn_info (ASGI servers that report no client). Defaulting that to `local`
    would hand full authority -- including /api/fs/* and mission launch -- to
    any such deployment, so production fails closed and the suite opts in.
    """
    return os.environ.get("CONDUCTION_ASSUME_LOCAL_PEER", "0").strip() == "1"


def trusted_networks() -> list[ipaddress.IPv4Network | ipaddress.IPv6Network]:
    """Networks whose peers may hold `remote` scope.

    No default. `100.64.0.0/10` is the whole carrier-grade NAT range, not "your
    tailnet" -- a hotspot neighbour or a container network can sit inside it.
    Enabling remote access without naming your own prefix grants nothing.
    """
    configured = os.environ.get("CONDUCTION_TRUSTED_CIDRS", "").strip()
    networks = []
    for entry in configured.split(","):
        entry = entry.strip()
        if not entry:
            continue
        try:
            networks.append(ipaddress.ip_network(entry, strict=False))
        except ValueError:
            continue
    return networks


def peer_address(request) -> str:
    """The actual socket peer.

    Deliberately NOT `request.remote_addr`: that reflects X-Forwarded-For, which
    a caller controls. Trust decisions are made on the address the kernel saw.
    """
    return (getattr(request, "ip", "") or "").strip()


def parse_peer(raw: str):
    """An ip_address, or None. Unwraps IPv4-mapped IPv6.

    On a dual-stack bind, a genuine loopback or tailnet IPv4 peer arrives as
    `::ffff:127.0.0.1`, which is not inside any IPv4 network -- without this it
    fails closed and the app looks broken to its own browser.
    """
    try:
        address = ipaddress.ip_address(raw)
    except ValueError:
        return None
    mapped = getattr(address, "ipv4_mapped", None)
    return mapped or address


def scope_for(request) -> str:
    """Classify a request into local / remote / denied."""
    raw = peer_address(request)
    if not raw:
        return SCOPE_LOCAL if assume_local_peer() else SCOPE_DENIED
    address = parse_peer(raw)
    if address is None:
        return SCOPE_DENIED
    if address.is_loopback:
        return SCOPE_LOCAL
    if not remote_access_enabled():
        return SCOPE_DENIED
    if any(address in network for network in trusted_networks()):
        return SCOPE_REMOTE
    return SCOPE_DENIED


# --- browser-borne attacks -------------------------------------------------
#
# Scope is decided by peer address, which identifies the MACHINE, not the user.
# A browser on the host is 127.0.0.1, so any page it visits speaks with local
# authority unless these two checks hold.


def allowed_hosts() -> set[str]:
    """Host header values this server answers to.

    Blocks DNS rebinding: `evil.com` re-resolved to 127.0.0.1 still sends
    `Host: evil.com`, so the page never becomes same-origin with the app.
    """
    hosts = {"127.0.0.1", "localhost", "::1", "[::1]"}
    for key in ("CONDUCTION_HOST", "CONDUCTION_ALLOWED_HOSTS"):
        for entry in os.environ.get(key, "").split(","):
            entry = entry.strip().lower()
            if entry and entry != "0.0.0.0":
                hosts.add(entry)
    return hosts


def host_allowed(request) -> bool:
    header = (request.headers.get("host") or "").strip().lower()
    if not header:
        # HTTP/1.0 clients and some tools omit it; there is no browser origin to
        # confuse in that case.
        return True
    hostname = header.rsplit(":", 1)[0] if "]" not in header else header.split("]")[0] + "]"
    return hostname in allowed_hosts() or header in allowed_hosts()


def same_origin_write(request) -> bool:
    """Is this state-changing request free of cross-site provenance?

    A cross-site form POST carries `Origin: https://evil.tld`, and every modern
    browser sends `Sec-Fetch-Site`, which script cannot forge. A request with
    neither header is not something a web page can produce (curl, the test
    suite), so it passes.
    """
    if request.method in ("GET", "HEAD", "OPTIONS"):
        return True

    fetch_site = (request.headers.get("sec-fetch-site") or "").strip().lower()
    if fetch_site:
        return fetch_site in ("same-origin", "none")

    origin = (request.headers.get("origin") or "").strip()
    if origin:
        host = urlsplit(origin).hostname or ""
        return host.lower() in allowed_hosts()
    return True


# --- what `remote` may do --------------------------------------------------
#
# An ALLOWLIST, not a blocklist. A new endpoint is remote-denied until someone
# adds it here and thinks about why. Getting that backwards means every future
# endpoint is remotely reachable by default.

REMOTE_GET_PATTERNS = (
    r"^/$",
    r"^/runs(/.*)?$",
    r"^/projects$",
    r"^/query$",
    r"^/factory$",
    r"^/static/.*$",
    r"^/api/ping$",
    r"^/api/sdks$",
    r"^/api/runs$",
    r"^/api/runs/[^/]+/(agents|events|findings|manifest|story|relaunch)$",
    r"^/api/observe/.*$",
    r"^/api/query/.*$",
    # /api/projects only, never /api/projects/<key>: the per-project document is
    # returned verbatim and carries webhook secrets and absolute host paths.
    r"^/api/projects$",
    r"^/api/factory/runs(/[^/]+)?$",
)

# Steer what is already running, and re-run what was already authored here.
#
# NOT /resume: resume_mission rebuilds its agents from `original_agents` in the
# REQUEST BODY, so allowing it would let a remote caller supply any brief and
# any tool list -- including Bash -- which is exactly what /rerun exists to
# prevent. Re-allow it only once it reads agents from the stored manifest and
# accepts edits at local scope alone.
#
# NOT POST /api/runs, for the same reason.
REMOTE_POST_PATTERNS = (r"^/api/runs/[^/]+/(interrupt|rerun)$",)

_REMOTE_GET = tuple(re.compile(p) for p in REMOTE_GET_PATTERNS)
_REMOTE_POST = tuple(re.compile(p) for p in REMOTE_POST_PATTERNS)


def remote_may(method: str, path: str) -> bool:
    """Is `method path` on the remote allowlist?"""
    if method in ("GET", "HEAD"):
        return any(pattern.match(path) for pattern in _REMOTE_GET)
    if method == "POST":
        return any(pattern.match(path) for pattern in _REMOTE_POST)
    return False


def denial(scope: str, method: str, path: str):
    """The 403 body for a request this scope may not make, or None if it may."""
    if scope == SCOPE_LOCAL:
        return None
    if scope == SCOPE_REMOTE and remote_may(method, path):
        return None
    if scope == SCOPE_REMOTE:
        return sanic_json(
            {
                "error": "not permitted from a remote client",
                "scope": scope,
                "detail": "Remote clients may observe, steer and re-run. "
                "Authoring new work is local-only.",
            },
            status=403,
        )
    return sanic_json(
        {
            "error": "this server only answers local clients",
            "scope": scope,
            "detail": "Set CONDUCTION_REMOTE_ACCESS=1 and CONDUCTION_TRUSTED_CIDRS "
            "to allow a private network.",
        },
        status=403,
    )


def refuse(request):
    """The refusal for this request, or None when it may proceed."""
    if not host_allowed(request):
        return sanic_json({"error": "unrecognised Host header"}, status=403)
    if not same_origin_write(request):
        return sanic_json({"error": "cross-site request refused"}, status=403)
    return denial(scope_for(request), request.method, request.path)


def install(app) -> None:
    """Attach scope classification and enforcement to every request."""
    from sanic.exceptions import NotFound

    @app.on_request
    async def enforce_scope(request):
        request.ctx.scope = scope_for(request)
        refusal = refuse(request)
        if refusal is not None:
            return refusal

    @app.exception(NotFound)
    async def scoped_not_found(request, exception):
        """Request middleware never runs for an unmatched route, so without this
        a remote client can tell "forbidden" from "does not exist" and map the
        whole surface, /api/fs/* included."""
        refusal = refuse(request)
        if refusal is not None:
            return refusal
        return sanic_json({"error": "not found", "path": request.path}, status=404)
