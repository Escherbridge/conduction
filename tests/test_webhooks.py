"""Tests for webhooks.py -- outbound signed notifications.

Fast lane: these drive the module directly against a throwaway HTTP receiver in
a thread, with no app.py subprocess. See conductor/code_styleguides/testing.md.
"""

from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

import webhooks


class Receiver:
    """A real HTTP endpoint that records what it was sent.

    Real, not a mock: the thing under test is an actual signed POST over the
    wire, including headers and retry behaviour. A mocked `urlopen` would assert
    that the code calls itself correctly and prove nothing about delivery.
    """

    def __init__(self, status=200, fail_times=0):
        self.received = []
        self._status = status
        self._fail_times = fail_times
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                length = int(self.headers.get("Content-Length", 0))
                body = self.rfile.read(length)
                outer.received.append(
                    {
                        "body": body,
                        "json": json.loads(body),
                        "signature": self.headers.get(webhooks.SIGNATURE_HEADER),
                        "event": self.headers.get(webhooks.EVENT_HEADER),
                    }
                )
                if outer._fail_times > 0:
                    outer._fail_times -= 1
                    self.send_response(500)
                else:
                    self.send_response(outer._status)
                self.end_headers()

            def log_message(self, *args):
                pass

        self.server = HTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.server.server_port}/hook"
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)

    def __enter__(self):
        self.thread.start()
        return self

    def __exit__(self, *exc):
        self.server.shutdown()
        self.server.server_close()


@pytest.fixture(autouse=True)
def no_retry_sleep(monkeypatch):
    """Retries are real; their backoff is not worth 5 s of suite time."""
    monkeypatch.setattr(webhooks, "RETRY_BACKOFF_SECONDS", (0, 0))


# --- signing ---------------------------------------------------------------


def test_signature_round_trips_and_rejects_tampering():
    body = b'{"event":"run.failed"}'
    signature = webhooks.sign("s3cret", body)
    assert signature.startswith("sha256=")
    assert webhooks.verify("s3cret", body, signature)
    assert not webhooks.verify("wrong-secret", body, signature)
    assert not webhooks.verify("s3cret", b'{"event":"run.completed"}', signature)
    assert not webhooks.verify("s3cret", body, "")


# --- validation ------------------------------------------------------------


def test_validate_accepts_a_well_formed_subscription():
    assert (
        webhooks.validate_subscription(
            {"url": "https://example.test/hook", "events": ["run.failed"], "secret": "x"}
        )
        == []
    )


def test_validate_rejects_the_ways_this_goes_wrong():
    problems = webhooks.validate_subscription({"url": "ftp://nope", "events": []})
    assert any("http" in p for p in problems)
    assert any("events" in p for p in problems)

    unknown = webhooks.validate_subscription({"url": "https://x.test", "events": ["run.exploded"]})
    assert any("unknown events" in p for p in unknown)

    assert webhooks.validate_subscription("not an object") == ["subscription must be an object"]


def test_declared_events_are_only_the_ones_that_actually_fire():
    """An event name a user can subscribe to but which never fires is a lie in
    the config UI. Keep this list honest as the lifecycle grows."""
    assert set(webhooks.EVENTS) == {
        "run.launched",
        "run.completed",
        "run.failed",
        "gate.failed",
    }


# --- matching --------------------------------------------------------------


def test_matching_filters_by_event_enabled_flag_and_repo(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    other = tmp_path / "other"
    other.mkdir()

    everywhere = {"url": "u", "events": ["run.failed"]}
    assert webhooks.subscription_matches(everywhere, "run.failed", str(repo))
    assert not webhooks.subscription_matches(everywhere, "run.completed", str(repo))

    disabled = {"url": "u", "events": ["run.failed"], "enabled": False}
    assert not webhooks.subscription_matches(disabled, "run.failed", str(repo))

    scoped = {"url": "u", "events": ["run.failed"], "target_repo": str(repo)}
    assert webhooks.subscription_matches(scoped, "run.failed", str(repo))
    assert not webhooks.subscription_matches(scoped, "run.failed", str(other))


# --- delivery --------------------------------------------------------------


def test_delivery_posts_signed_json_with_the_event_header():
    with Receiver() as receiver:
        entry = {"url": receiver.url, "events": ["run.failed"], "secret": "s3cret"}
        payload = webhooks.build_payload("run.failed", {"run_id": "MISSION-x@abc"})
        result = webhooks.deliver(entry, "run.failed", payload)

    assert result["ok"] is True
    assert len(receiver.received) == 1
    delivered = receiver.received[0]
    assert delivered["event"] == "run.failed"
    assert delivered["json"]["run"]["run_id"] == "MISSION-x@abc"
    # The signature must verify against the exact bytes that were sent.
    assert webhooks.verify("s3cret", delivered["body"], delivered["signature"])


def test_delivery_omits_the_signature_when_no_secret_is_set():
    with Receiver() as receiver:
        webhooks.deliver(
            {"url": receiver.url, "events": ["run.launched"]},
            "run.launched",
            webhooks.build_payload("run.launched", {}),
        )
    assert receiver.received[0]["signature"] is None


def test_delivery_retries_a_server_error_then_succeeds():
    with Receiver(fail_times=2) as receiver:
        result = webhooks.deliver(
            {"url": receiver.url, "events": ["run.failed"]},
            "run.failed",
            webhooks.build_payload("run.failed", {}),
        )
    assert result["ok"] is True
    assert result["attempts"] == 3
    assert len(receiver.received) == 3


def test_delivery_does_not_retry_a_4xx():
    """A 4xx is the receiver rejecting the request itself -- repeating an
    unauthorised or malformed POST just gets the same answer three times."""
    with Receiver(status=404) as receiver:
        result = webhooks.deliver(
            {"url": receiver.url, "events": ["run.failed"]},
            "run.failed",
            webhooks.build_payload("run.failed", {}),
        )
    assert result["ok"] is False
    assert result["attempts"] == 1
    assert len(receiver.received) == 1


def test_an_unreachable_receiver_never_raises():
    """The load-bearing property: a webhook endpoint being down must not touch
    the mission that triggered it."""
    result = webhooks.deliver(
        {"url": "http://127.0.0.1:9/nothing-listens-here", "events": ["run.failed"]},
        "run.failed",
        webhooks.build_payload("run.failed", {}),
    )
    assert result["ok"] is False
    assert result["attempts"] == webhooks.MAX_ATTEMPTS


# --- dispatch --------------------------------------------------------------


def test_dispatch_delivers_to_ecosystem_and_project_subscriptions(monkeypatch, tmp_path):
    monkeypatch.setenv("CONDUCTION_TRUST_PROJECT_WEBHOOKS", "1")
    from agentgraph import policy

    app_root = tmp_path / "app"
    repo = tmp_path / "repo"
    app_root.mkdir()
    repo.mkdir()

    with Receiver() as eco, Receiver() as proj:
        policy.save_ecosystem(
            app_root,
            dict(
                policy.empty_ecosystem(),
                webhooks=[{"url": eco.url, "events": ["run.failed"]}],
            ),
        )
        policy.save_project(
            repo,
            dict(
                policy.empty_project("repo"),
                webhooks=[{"url": proj.url, "events": ["run.failed"]}],
            ),
        )
        results = webhooks.dispatch(
            app_root, "run.failed", {"run_id": "MISSION-x@abc", "target_repo": str(repo)}
        )

    by_url = {result["url"]: result for result in results}
    assert by_url[eco.url]["ok"] is True
    assert len(eco.received) == 1

    # The project entry is opted in and well-formed, but it points at loopback
    # and arrived from a repository, so it is refused rather than delivered.
    assert by_url[proj.url]["ok"] is False
    assert "private address" in by_url[proj.url]["error"]
    assert proj.received == []


def test_webhooks_survive_the_vendored_policy_round_trip(tmp_path):
    """`webhooks` is deliberately NOT in agentgraph/policy.py's schema -- that
    package is a vendored copy and editing it would diverge from upstream. This
    pins the property that makes that possible: unknown keys survive save/load
    and do not fail validation."""
    from agentgraph import policy

    app_root = tmp_path / "app"
    app_root.mkdir()
    doc = dict(
        policy.empty_ecosystem(),
        webhooks=[{"url": "https://x.test", "events": ["run.failed"]}],
    )
    policy.save_ecosystem(app_root, doc)

    reloaded = policy.load_ecosystem(app_root)
    assert reloaded["webhooks"] == doc["webhooks"]
    assert policy.validate_ecosystem(reloaded) == []


def test_dispatch_ignores_an_unknown_event(tmp_path):
    assert webhooks.dispatch(tmp_path, "run.exploded", {"target_repo": None}) == []


# --- reading the run log ---------------------------------------------------


def test_final_mission_status_reads_the_wrapped_event_shape(tmp_path):
    """Log records wrap the event: {"event": {"type", "payload"}, "run_id", "seq"}.

    Reading `type`/`data` off the top level instead returned {} for every run,
    which made a run that COMPLETED fire run.failed. Pinning the shape here
    because it belongs to the vendored engine and can move underneath us.
    """
    import json as stdlib_json

    from app import final_mission_status

    log = tmp_path / "run.jsonl"
    log.write_text(
        "\n".join(
            stdlib_json.dumps(record)
            for record in (
                {"event": {"type": "agent.started", "payload": {}}, "run_id": "r", "seq": 1},
                {
                    "event": {
                        "type": "mission.completed",
                        "payload": {
                            "agents_total": 2,
                            "agents_failed": 0,
                            "gate_passed": True,
                            "status": "completed",
                        },
                    },
                    "run_id": "r",
                    "seq": 2,
                },
            )
        ),
        encoding="utf-8",
    )
    outcome = final_mission_status(log)
    assert outcome["status"] == "completed"
    assert outcome["gate_passed"] is True
    assert outcome["agents_total"] == 2


def test_final_mission_status_is_empty_for_a_run_that_never_completed(tmp_path):
    from app import final_mission_status

    log = tmp_path / "run.jsonl"
    log.write_text('{"event": {"type": "agent.started", "payload": {}}}\n', encoding="utf-8")
    assert final_mission_status(log) == {}
    assert final_mission_status(tmp_path / "missing.jsonl") == {}


# --- end to end ------------------------------------------------------------


def test_a_real_mission_fires_launched_and_completed(app_server_factory, tmp_path):
    """Smoke lane: proves the lifecycle is actually wired, not just that the
    dispatcher works in isolation. A unit test of webhooks.py cannot catch the
    hook being registered against the wrong root or never called at all -- which
    is exactly the bug this found (APP_ROOT vs ECOSYSTEM_ROOT)."""
    import time

    import requests

    from agentgraph import policy

    eco_root = tmp_path / "eco"
    repo = tmp_path / "workspace" / "demo"
    (repo / ".git").mkdir(parents=True)
    eco_root.mkdir()

    with Receiver() as receiver:
        policy.save_ecosystem(
            eco_root,
            dict(
                policy.empty_ecosystem(),
                webhooks=[
                    {
                        "url": receiver.url,
                        # Every event: subscribing to only the happy path hides a
                        # run that completed but dispatched run.failed.
                        "events": list(webhooks.EVENTS),
                        "secret": "hook-secret",
                    }
                ],
            ),
        )
        server = app_server_factory(
            {
                "CONDUCTION_DRY_RUN": "1",
                "CONDUCTION_SCHEDULER": "0",
                "CONDUCTION_ECOSYSTEM_ROOT": str(eco_root),
                "CONDUCTION_ALLOWED_ROOTS": str(repo.parent),
            }
        )
        response = requests.post(
            server.base_url + "/api/runs",
            json={
                "slug": "hooked",
                "target_repo": str(repo),
                "agents": [{"name": "alpha", "brief": "Do it.", "tools": ["Read"]}],
            },
            timeout=30,
        )
        assert response.status_code in (200, 201, 202), response.text

        deadline = time.monotonic() + 60
        while time.monotonic() < deadline:
            events = [item["event"] for item in receiver.received]
            if "run.launched" in events and any(e.startswith("run.") for e in events[1:]):
                break
            time.sleep(0.3)

    events = [item["event"] for item in receiver.received]
    assert "run.launched" in events, f"never fired run.launched; got {events}"
    assert "run.completed" in events, (
        f"a dry-run mission that completed must fire run.completed; got {events}"
    )
    assert "run.failed" not in events

    completed = next(item for item in receiver.received if item["event"] == "run.completed")
    assert completed["json"]["run"]["slug"] == "hooked"
    assert completed["json"]["run"]["target_repo"] == str(repo)
    assert webhooks.verify("hook-secret", completed["body"], completed["signature"])


# --- repo-supplied subscriptions are not trusted ---------------------------


def test_a_cloned_repo_cannot_register_webhooks_by_default(monkeypatch, tmp_path):
    """`.agentgraph/project.json` is a VERSIONED file (see manifest.py's
    gitignore), so cloning any repository would otherwise hand it an outbound
    request from this machine on every run -- leaking run metadata and absolute
    host paths, and probing whatever this host can reach."""
    from agentgraph import policy

    monkeypatch.delenv("CONDUCTION_TRUST_PROJECT_WEBHOOKS", raising=False)
    app_root = tmp_path / "app"
    repo = tmp_path / "cloned"
    app_root.mkdir()
    repo.mkdir()
    policy.save_project(
        repo,
        dict(
            policy.empty_project("cloned"),
            webhooks=[{"url": "https://evil.tld/collect", "events": ["run.launched"]}],
        ),
    )
    assert webhooks.collect_subscriptions(app_root, str(repo)) == []


def test_a_malformed_subscription_is_dropped_at_load(tmp_path):
    """Validation runs on READ, not only on write: these documents are edited by
    hand and shipped inside repositories, so the write API is not the only way
    an entry arrives."""
    from agentgraph import policy

    app_root = tmp_path / "app"
    app_root.mkdir()
    policy.save_ecosystem(
        app_root,
        dict(
            policy.empty_ecosystem(),
            webhooks=[
                {"url": "file:///etc/passwd", "events": ["run.failed"]},
                {"url": "https://ok.test/hook", "events": ["run.failed"]},
            ],
        ),
    )
    urls = [entry["url"] for entry in webhooks.collect_subscriptions(app_root, None)]
    assert urls == ["https://ok.test/hook"]


def test_private_destinations_are_refused_for_repo_supplied_entries():
    for url in ("http://127.0.0.1:8000/api/runs", "http://169.254.169.254/latest/meta-data"):
        assert not webhooks.destination_allowed(url), url
    assert not webhooks.destination_allowed("not-a-url")


def test_redirects_are_not_followed():
    """A 302 from a public URL to 127.0.0.1 would walk the destination check
    straight back into private address space."""
    assert webhooks.NoRedirects().redirect_request(None, None, 302, "", {}, "http://x") is None
