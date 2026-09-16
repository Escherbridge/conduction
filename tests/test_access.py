"""Tests for access.py -- the scope layer.

Conduction had no authentication of any kind: the security model was "bound to
loopback". Joining a private network (Tailscale) ends that, so scope decides
what a non-local caller may do. These tests pin the boundary, because the cost
of getting it wrong is filesystem read plus agent execution on the host.
"""

from __future__ import annotations

import pytest

import access


class FakeRequest:
    """Minimal stand-in. `ip` is the real socket peer; `remote_addr` reflects
    X-Forwarded-For and is caller-controlled, which is the point of several
    tests below."""

    def __init__(self, ip, remote_addr=None, method="GET", path="/"):
        self.ip = ip
        self.remote_addr = remote_addr if remote_addr is not None else ip
        self.method = method
        self.path = path


@pytest.fixture
def remote_enabled(monkeypatch):
    monkeypatch.setenv("CONDUCTION_REMOTE_ACCESS", "1")
    # There is no default trusted range: naming one is part of enabling.
    monkeypatch.setenv("CONDUCTION_TRUSTED_CIDRS", "100.64.0.0/10")


@pytest.fixture
def remote_disabled(monkeypatch):
    monkeypatch.delenv("CONDUCTION_REMOTE_ACCESS", raising=False)


# --- scope classification --------------------------------------------------


def test_loopback_is_local(remote_disabled):
    assert access.scope_for(FakeRequest("127.0.0.1")) == access.SCOPE_LOCAL
    assert access.scope_for(FakeRequest("127.0.0.53")) == access.SCOPE_LOCAL
    assert access.scope_for(FakeRequest("::1")) == access.SCOPE_LOCAL


def test_remote_access_is_off_by_default(remote_disabled):
    """Installing Conduction must not open anything. A tailnet peer is denied
    until CONDUCTION_REMOTE_ACCESS=1 is set deliberately."""
    assert access.scope_for(FakeRequest("100.101.102.103")) == access.SCOPE_DENIED


def test_tailnet_peer_is_remote_once_enabled(remote_enabled):
    assert access.scope_for(FakeRequest("100.101.102.103")) == access.SCOPE_REMOTE


def test_off_tailnet_address_is_denied_even_when_enabled(remote_enabled):
    """Enabling remote access permits a named network, not the whole internet."""
    for address in ("192.168.1.50", "10.0.0.5", "8.8.8.8", "172.16.0.9"):
        assert access.scope_for(FakeRequest(address)) == access.SCOPE_DENIED, address


def test_trusted_cidrs_are_configurable(monkeypatch):
    monkeypatch.setenv("CONDUCTION_REMOTE_ACCESS", "1")
    monkeypatch.setenv("CONDUCTION_TRUSTED_CIDRS", "192.168.1.0/24")
    assert access.scope_for(FakeRequest("192.168.1.50")) == access.SCOPE_REMOTE
    assert access.scope_for(FakeRequest("100.101.102.103")) == access.SCOPE_DENIED


def test_there_is_no_default_trusted_range(monkeypatch):
    """100.64.0.0/10 is the whole carrier-grade NAT range, not "your tailnet".
    A hotspot neighbour or container network can sit inside it, so enabling
    remote access without naming a prefix must grant nothing."""
    monkeypatch.setenv("CONDUCTION_REMOTE_ACCESS", "1")
    monkeypatch.delenv("CONDUCTION_TRUSTED_CIDRS", raising=False)
    assert access.trusted_networks() == []
    assert access.scope_for(FakeRequest("100.101.102.103")) == access.SCOPE_DENIED


def test_ipv4_mapped_loopback_is_local(monkeypatch):
    """On a dual-stack bind a genuine loopback peer arrives as
    ::ffff:127.0.0.1. Without unwrapping it, the app denies its own browser."""
    monkeypatch.delenv("CONDUCTION_REMOTE_ACCESS", raising=False)
    assert access.scope_for(FakeRequest("::ffff:127.0.0.1")) == access.SCOPE_LOCAL


def test_ipv4_mapped_tailnet_peer_is_remote(remote_enabled):
    assert access.scope_for(FakeRequest("::ffff:100.101.102.103")) == access.SCOPE_REMOTE


def test_a_peerless_request_fails_closed(monkeypatch):
    """`request.ip` is empty for a UNIX-socket peer and on ASGI servers that
    report no client. Defaulting that to local would hand full authority --
    /api/fs/* and mission launch -- to any such deployment."""
    monkeypatch.delenv("CONDUCTION_ASSUME_LOCAL_PEER", raising=False)
    assert access.scope_for(FakeRequest("")) == access.SCOPE_DENIED

    monkeypatch.setenv("CONDUCTION_ASSUME_LOCAL_PEER", "1")
    assert access.scope_for(FakeRequest("")) == access.SCOPE_LOCAL


def test_forwarded_header_cannot_claim_loopback(remote_enabled):
    """The decisive check: a caller sets X-Forwarded-For freely, so trust is
    decided on `request.ip` -- the address the kernel actually saw. If this
    regresses, anyone who can reach the port becomes local."""
    spoofed = FakeRequest("203.0.113.9", remote_addr="127.0.0.1")
    assert access.scope_for(spoofed) == access.SCOPE_DENIED


def test_garbage_peer_is_denied(remote_enabled):
    assert access.scope_for(FakeRequest("not-an-ip")) == access.SCOPE_DENIED


# --- the remote allowlist --------------------------------------------------


def test_remote_may_observe():
    for path in (
        "/api/runs",
        "/api/runs/MISSION-x@abc123/events",
        "/api/runs/MISSION-x@abc123/manifest",
        "/api/observe/summary",
        "/api/query/costs",
        "/api/projects",
        "/runs",
        "/static/js/app.js",
    ):
        assert access.remote_may("GET", path), path


def test_remote_may_steer_and_rerun():
    run = "/api/runs/MISSION-x@abc123"
    for path in (f"{run}/interrupt", f"{run}/rerun"):
        assert access.remote_may("POST", path), path


def test_remote_may_not_resume():
    """resume_mission rebuilds its agents from `original_agents` in the REQUEST
    BODY, so allowing it remotely would let a caller supply any brief and any
    tool list -- including Bash. That is exactly what /rerun exists to prevent,
    and it made the /rerun boundary decorative while resume was allowed."""
    assert not access.remote_may("POST", "/api/runs/MISSION-x@abc123/resume")


def test_remote_may_not_read_a_project_document():
    """/api/projects/<key> returns the document verbatim -- webhook secrets and
    absolute host paths included."""
    assert access.remote_may("GET", "/api/projects")
    assert not access.remote_may("GET", "/api/projects/abc123")


def test_remote_may_not_author_new_work():
    """POST /api/runs accepts arbitrary agent briefs, tools and `owns` paths.
    Allowing it would let a phone start work nobody authored on this machine --
    the whole reason /rerun exists as a separate, manifest-only endpoint."""
    assert not access.remote_may("POST", "/api/runs")


def test_remote_may_not_browse_the_filesystem():
    """The filesystem API enumerates the host's disk. It is local-only whatever
    the network posture."""
    for path in ("/api/fs/roots", "/api/fs/list", "/api/fs/validate"):
        assert not access.remote_may("GET", path), path
    assert not access.remote_may("POST", "/api/fs/pick")


def test_remote_may_not_delete_or_reconfigure():
    assert not access.remote_may("DELETE", "/api/runs/MISSION-x@abc123")
    assert not access.remote_may("PUT", "/api/ecosystem")
    assert not access.remote_may("POST", "/api/ecosystem")


def test_unknown_endpoints_are_denied_by_default():
    """The allowlist is the point: an endpoint added tomorrow is remote-denied
    until someone lists it and thinks about why."""
    assert not access.remote_may("GET", "/api/something-invented-later")
    assert not access.remote_may("POST", "/api/runs/x/some-new-action")


# --- enforcement -----------------------------------------------------------


def test_denial_lets_local_do_anything(remote_disabled):
    assert access.denial(access.SCOPE_LOCAL, "POST", "/api/runs") is None
    assert access.denial(access.SCOPE_LOCAL, "GET", "/api/fs/list") is None


def test_denial_explains_the_remote_boundary():
    refused = access.denial(access.SCOPE_REMOTE, "POST", "/api/runs")
    assert refused is not None and refused.status == 403
    assert access.denial(access.SCOPE_REMOTE, "POST", "/api/runs/x/rerun") is None


def test_denied_scope_is_refused_outright():
    refused = access.denial(access.SCOPE_DENIED, "GET", "/api/ping")
    assert refused is not None and refused.status == 403


# --- browser-borne attacks -------------------------------------------------


class HeaderRequest(FakeRequest):
    def __init__(self, ip="127.0.0.1", method="POST", path="/api/runs", **headers):
        super().__init__(ip, method=method, path=path)
        self.headers = {key.replace("_", "-"): value for key, value in headers.items()}


def test_dns_rebinding_is_refused_by_the_host_check(monkeypatch):
    """`evil.com` re-resolved to 127.0.0.1 is still sent with Host: evil.com,
    so the page never becomes same-origin with the app."""
    monkeypatch.delenv("CONDUCTION_HOST", raising=False)
    monkeypatch.delenv("CONDUCTION_ALLOWED_HOSTS", raising=False)
    assert not access.host_allowed(HeaderRequest(host="evil.com"))
    assert access.host_allowed(HeaderRequest(host="127.0.0.1:8000"))
    assert access.host_allowed(HeaderRequest(host="localhost:8000"))


def test_extra_hosts_can_be_allowed(monkeypatch):
    monkeypatch.setenv("CONDUCTION_ALLOWED_HOSTS", "conduction.tailnet.ts.net")
    assert access.host_allowed(HeaderRequest(host="conduction.tailnet.ts.net:8000"))


def test_cross_site_writes_are_refused(monkeypatch):
    """Scope is decided by peer address, which identifies the MACHINE. A browser
    on the host is 127.0.0.1, so without this every page the user visits can
    POST with full local authority."""
    monkeypatch.delenv("CONDUCTION_ALLOWED_HOSTS", raising=False)
    assert not access.same_origin_write(HeaderRequest(sec_fetch_site="cross-site"))
    assert not access.same_origin_write(HeaderRequest(origin="https://evil.tld"))

    assert access.same_origin_write(HeaderRequest(sec_fetch_site="same-origin"))
    assert access.same_origin_write(HeaderRequest(origin="http://127.0.0.1:8000"))


def test_reads_and_non_browser_clients_still_work(monkeypatch):
    """A request with neither header is not something a web page can produce --
    curl and the test suite must keep working."""
    monkeypatch.delenv("CONDUCTION_ALLOWED_HOSTS", raising=False)
    assert access.same_origin_write(HeaderRequest(method="GET", sec_fetch_site="cross-site"))
    assert access.same_origin_write(HeaderRequest())
