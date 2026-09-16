"""Outbound webhooks: signed JSON POSTs when something notable happens to a run.

Subscriptions live beside rules, goals and schedules in ecosystem.json and each
project's project.json. Rationale, the event contract and the signature scheme
live in AGENTS.md -- see "webhooks".
"""

from __future__ import annotations

import hashlib
import hmac
import http.client
import ipaddress
import json as stdlib_json
import os
import socket
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

#: Event names a subscription may filter on. A subscription naming none of
#: these never fires, which is a configuration error, not a silent default.
EVENTS = (
    "run.launched",
    "run.completed",
    "run.failed",
    "gate.failed",
)

DELIVERY_TIMEOUT_SECONDS = 10
MAX_ATTEMPTS = 3
RETRY_BACKOFF_SECONDS = (1, 4)
#: A malicious project.json could otherwise list dozens of blackholed URLs and
#: hold the dispatching thread for minutes.
MAX_SUBSCRIPTIONS_PER_DISPATCH = 10

#: Where a subscription came from. Ecosystem entries are authored by the user
#: through a local-only API; project entries ride inside a cloned repository and
#: are therefore attacker-shaped.
SOURCE_KEY = "_source"
SOURCE_ECOSYSTEM = "ecosystem"
SOURCE_PROJECT = "project"
SIGNATURE_HEADER = "X-Conduction-Signature"
EVENT_HEADER = "X-Conduction-Event"


def sign(secret: str, payload: bytes) -> str:
    """`sha256=<hex>` over the exact body, so a receiver can verify it."""
    digest = hmac.new(secret.encode("utf-8"), payload, hashlib.sha256).hexdigest()
    return f"sha256={digest}"


def verify(secret: str, payload: bytes, signature: str) -> bool:
    """Constant-time check of a signature this module produced."""
    return hmac.compare_digest(sign(secret, payload), signature or "")


def validate_subscription(entry: Any) -> list[str]:
    """Problems with one subscription, or an empty list."""
    errors: list[str] = []
    if not isinstance(entry, dict):
        return ["subscription must be an object"]

    url = entry.get("url")
    if not isinstance(url, str) or not url:
        errors.append("url is required")
    elif not url.startswith(("http://", "https://")):
        errors.append("url must be http:// or https://")

    events = entry.get("events")
    if not isinstance(events, list) or not events:
        errors.append("events must be a non-empty list")
    else:
        unknown = [event for event in events if event not in EVENTS]
        if unknown:
            errors.append(f"unknown events: {', '.join(map(str, unknown))}")

    secret = entry.get("secret")
    if secret is not None and not isinstance(secret, str):
        errors.append("secret must be a string when present")

    target = entry.get("target_repo")
    if target is not None and not isinstance(target, str):
        errors.append("target_repo must be a string or null")
    return errors


def project_webhooks_trusted() -> bool:
    """May a TARGET REPO's own project.json register webhooks?

    Off by default. `.agentgraph/project.json` is a versioned file, so cloning
    any repository would otherwise hand it an outbound HTTP request from this
    machine on every run -- exfiltration of run metadata and absolute host
    paths, plus a probe into whatever this host can reach.
    """
    return os.environ.get("CONDUCTION_TRUST_PROJECT_WEBHOOKS", "0").strip() == "1"


def address_is_private(raw: str) -> bool:
    """Is this a destination the host can only reach privately?"""
    try:
        address = ipaddress.ip_address(raw)
    except ValueError:
        return True
    address = getattr(address, "ipv4_mapped", None) or address
    return bool(
        address.is_loopback
        or address.is_private
        or address.is_link_local
        or address.is_reserved
        or address.is_multicast
        or address.is_unspecified
    )


def resolve_public(url: str) -> str | None:
    """The one public IP this URL may be delivered to, or None to refuse.

    Applied to REPO-SUPPLIED subscriptions only. A subscription pointed at
    loopback, the LAN or the tailnet turns the notifier into a proxy for
    networks its author cannot otherwise touch -- including this app's own
    local-scope API. You may legitimately point your OWN ecosystem webhooks at
    a service on this machine; a URL that arrived inside a cloned repository is
    not trusted that far.

    Returns the ADDRESS, not a yes/no, because checking and then connecting by
    hostname resolves twice: a DNS server the attacker controls can answer
    public for the check and 127.0.0.1 for the delivery. The caller connects to
    exactly what was verified.
    """
    hostname = urlsplit(url).hostname
    if not hostname:
        return None
    try:
        infos = socket.getaddrinfo(hostname, None)
    except OSError:
        return None
    if not infos:
        return None
    chosen = None
    for info in infos:
        raw = info[4][0]
        if address_is_private(raw):
            return None
        if chosen is None:
            chosen = raw
    return chosen


def destination_allowed(url: str) -> bool:
    """Whether a repo-supplied subscription may be delivered at all."""
    return resolve_public(url) is not None


class PinnedHTTPSConnection(http.client.HTTPSConnection):
    """HTTPS to a pre-verified IP, presenting the original hostname.

    TLS verification and the Host header still use the real hostname, so the
    certificate check is unchanged; only the address dialled is pinned.
    """

    pinned_ip = None

    def connect(self):
        host, self.host = self.host, self.pinned_ip or self.host
        try:
            super().connect()
        finally:
            self.host = host


class NoRedirects(urllib.request.HTTPRedirectHandler):
    """Refuse redirects: a 302 from a public URL to 127.0.0.1 would walk the
    destination check straight back into the private address space."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


_opener = urllib.request.build_opener(NoRedirects)


def _pinned_opener(ip: str):
    """An opener that dials `ip` and nothing else, redirects still refused."""

    class _Connection(PinnedHTTPSConnection):
        pinned_ip = ip

    class _HTTPSHandler(urllib.request.HTTPSHandler):
        def https_open(self, req):
            return self.do_open(_Connection, req)

    class _HTTPHandler(urllib.request.HTTPHandler):
        def http_open(self, req):
            def build(host, **kwargs):
                kwargs.pop("context", None)
                connection = http.client.HTTPConnection(ip, **kwargs)
                connection._real_host = host
                return connection

            return self.do_open(build, req)

    return urllib.request.build_opener(NoRedirects, _HTTPSHandler, _HTTPHandler)


def subscription_matches(entry: dict, event: str, target_repo: str | None) -> bool:
    """Does this subscription want this event, from this repo?

    `target_repo: null` means every project. A subscription stored in a
    project's own project.json is already scoped to it by where it lives.
    """
    if event not in (entry.get("events") or []):
        return False
    if entry.get("enabled") is False:
        return False
    wanted = entry.get("target_repo")
    if wanted in (None, ""):
        return True
    if target_repo is None:
        return False
    try:
        return Path(wanted).resolve() == Path(target_repo).resolve()
    except OSError:
        return str(wanted).lower() == str(target_repo).lower()


def collect_subscriptions(app_root, target_repo: str | None) -> list[dict]:
    """Ecosystem subscriptions plus the target repo's own, de-duplicated by url."""
    from agentgraph import policy

    found: list[dict] = []
    seen: set[str] = set()

    def add(entries, source):
        for entry in entries or []:
            if not isinstance(entry, dict):
                continue
            url = entry.get("url")
            if not isinstance(url, str) or url in seen:
                continue
            # Validate on READ, not only on write: these documents are edited by
            # hand and shipped inside cloned repositories, so the write API is
            # not the only way an entry arrives.
            if validate_subscription(entry):
                continue
            seen.add(url)
            found.append({**entry, SOURCE_KEY: source})

    try:
        add(policy.load_ecosystem(app_root).get("webhooks"), SOURCE_ECOSYSTEM)
    except (OSError, ValueError):
        pass
    if target_repo and project_webhooks_trusted():
        try:
            add(policy.load_project(Path(target_repo)).get("webhooks"), SOURCE_PROJECT)
        except (OSError, ValueError):
            pass
    return found


def build_payload(event: str, run: dict) -> dict:
    """The wire body. Flat, stable, and safe to log."""
    return {
        "event": event,
        "sent_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "run": run,
    }


def deliver(entry: dict, event: str, payload: dict) -> dict:
    """POST one subscription. Returns a result record; never raises.

    A webhook receiver being down must never affect the mission that triggered
    it, so every failure is captured and reported rather than propagated.
    """
    body = stdlib_json.dumps(payload, sort_keys=True).encode("utf-8")
    headers = {"Content-Type": "application/json", EVENT_HEADER: event}
    secret = entry.get("secret")
    if secret:
        headers[SIGNATURE_HEADER] = sign(secret, body)

    url = entry.get("url", "")
    untrusted = entry.get(SOURCE_KEY) == SOURCE_PROJECT
    if untrusted and resolve_public(url) is None:
        return {
            "url": url,
            "ok": False,
            "error": "repo-supplied webhook may not target a private address",
            "attempts": 0,
        }
    last_error = ""
    for attempt in range(MAX_ATTEMPTS):
        opener = _opener
        if untrusted:
            # Re-resolve per attempt: a lookup that changes between retries must
            # fail closed rather than inherit the first attempt's verdict.
            pinned = resolve_public(url)
            if pinned is None:
                return {
                    "url": url,
                    "ok": False,
                    "error": "destination stopped resolving to a public address",
                    "attempts": attempt + 1,
                }
            opener = _pinned_opener(pinned)
        request = urllib.request.Request(url, data=body, headers=headers, method="POST")
        try:
            with opener.open(request, timeout=DELIVERY_TIMEOUT_SECONDS) as response:
                return {"url": url, "ok": True, "status": response.status, "attempts": attempt + 1}
        except urllib.error.HTTPError as error:
            last_error = f"HTTP {error.code}"
            # 4xx is the receiver rejecting the request itself; retrying an
            # unauthorised or malformed POST just repeats the same answer.
            if 400 <= error.code < 500:
                return {"url": url, "ok": False, "status": error.code, "attempts": attempt + 1}
        except (urllib.error.URLError, TimeoutError, OSError) as error:
            last_error = str(error)
        if attempt < len(RETRY_BACKOFF_SECONDS):
            time.sleep(RETRY_BACKOFF_SECONDS[attempt])
    return {"url": url, "ok": False, "error": last_error, "attempts": MAX_ATTEMPTS}


def dispatch(app_root, event: str, run: dict) -> list[dict]:
    """Deliver `event` to every matching subscription. Blocking; call in a thread."""
    if event not in EVENTS:
        return []
    target_repo = run.get("target_repo")
    payload = build_payload(event, run)
    results = []
    matching = [
        entry
        for entry in collect_subscriptions(app_root, target_repo)
        if subscription_matches(entry, event, target_repo)
    ]
    for entry in matching[:MAX_SUBSCRIPTIONS_PER_DISPATCH]:
        results.append(deliver(entry, event, payload))
    return results
