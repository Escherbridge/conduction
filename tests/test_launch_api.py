"""Launch/resume API: SDK routing, per-agent owns, and host-side gates.

Every test runs the server with CONDUCTION_DRY_RUN=1, so no model is called:
agents finish in ~1.5 s and the gate is the only thing doing real work.
"""

from __future__ import annotations

import json
import sys
import tempfile
import time
import uuid
from pathlib import Path

import pytest
import requests

MISSION_TIMEOUT_SECONDS = 90


@pytest.fixture
def target_repo():
    """A throwaway repo under Path.home(), which is the default allowed root."""
    with tempfile.TemporaryDirectory(dir=str(Path.home())) as directory:
        yield str(Path(directory).resolve())


@pytest.fixture
def server(app_server_factory):
    return app_server_factory({"CONDUCTION_DRY_RUN": "1"})


def unique_slug(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4().hex[:8]}"


def launch_body(target_repo: str, slug: str, **extra) -> dict:
    body = {
        "slug": slug,
        "target_repo": target_repo,
        "agents": [{"name": "solo", "brief": "do nothing", "tools": ["Read"]}],
        "max_turns": 3,
        "max_concurrency": 1,
    }
    body.update(extra)
    return body


def read_events(log_path: Path) -> list[dict]:
    events = []
    for line in log_path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            envelope = json.loads(line)
        except ValueError:
            continue
        events.append(envelope.get("event", envelope))  # JSONL wraps each event
    return events


def find_event(events: list[dict], event_type: str) -> dict | None:
    for event in events:
        if event.get("type") == event_type:
            return event
    return None


def wait_for_completion(log_path: Path) -> dict:
    """Block until mission.completed lands in the run log; return its payload."""
    deadline = time.monotonic() + MISSION_TIMEOUT_SECONDS
    while time.monotonic() < deadline:
        if log_path.exists():
            completed = find_event(read_events(log_path), "mission.completed")
            if completed is not None:
                return completed.get("payload", {})
        time.sleep(0.5)
    raise AssertionError(f"mission.completed never appeared in {log_path}")


def command_gate(exit_code: int) -> dict:
    return {
        "command": {
            "argv": [sys.executable, "-c", f"raise SystemExit({exit_code})"],
        }
    }


def test_api_sdks_reports_registry_and_dry_run(server):
    response = requests.get(f"{server.base_url}/api/sdks", timeout=10)
    assert response.status_code == 200
    body = response.json()
    assert "claude" in body["sdks"]
    assert body["dry_run"] is True


def test_launch_page_renders(server):
    response = requests.get(f"{server.base_url}/launch", timeout=10)
    assert response.status_code == 200
    assert "text/html" in response.headers["content-type"]


def test_gate_passing_command_completes_run(server, target_repo):
    slug = unique_slug("gate-pass")
    response = requests.post(
        f"{server.base_url}/api/runs",
        json=launch_body(target_repo, slug, gate=command_gate(0)),
        timeout=30,
    )
    assert response.status_code == 200, response.text
    launched = response.json()
    assert launched["gate"] is True

    payload = wait_for_completion(Path(launched["log_path"]))
    assert payload["status"] == "completed"
    assert payload["gate_passed"] is True

    events = read_events(Path(launched["log_path"]))
    gate_findings = [
        event
        for event in events
        if event.get("payload", {}).get("topic") == "gate"
        and event.get("payload", {}).get("summary") == "passed"
    ]
    assert gate_findings, "no gate finding with summary 'passed' in the run log"


def test_gate_failing_command_fails_run(server, target_repo):
    slug = unique_slug("gate-fail")
    response = requests.post(
        f"{server.base_url}/api/runs",
        json=launch_body(target_repo, slug, gate=command_gate(3)),
        timeout=30,
    )
    assert response.status_code == 200, response.text
    launched = response.json()

    payload = wait_for_completion(Path(launched["log_path"]))
    assert payload["status"] == "failed"
    assert payload["gate_passed"] is False

    runs = requests.get(f"{server.base_url}/api/runs", timeout=30).json()
    row = next(run for run in runs if run["run_id"] == launched["run_id"])
    assert row["status"] == "failed"


def test_invalid_gate_spec_is_rejected(server, target_repo):
    response = requests.post(
        f"{server.base_url}/api/runs",
        json=launch_body(target_repo, unique_slug("gate-bogus"), gate={"bogus": 1}),
        timeout=30,
    )
    assert response.status_code == 400, response.text
    assert "bogus" in response.json()["error"]


def test_unknown_sdk_is_rejected_with_registry_keys(server, target_repo):
    body = launch_body(target_repo, unique_slug("sdk-nope"))
    body["agents"][0]["sdk"] = "nope"
    response = requests.post(f"{server.base_url}/api/runs", json=body, timeout=30)
    assert response.status_code == 400, response.text
    assert "claude" in response.json()["error"]


def test_agent_owns_rides_into_the_request_meta(server, target_repo):
    body = launch_body(target_repo, unique_slug("owns"))
    body["agents"][0]["owns"] = ["src/"]
    response = requests.post(f"{server.base_url}/api/runs", json=body, timeout=30)
    assert response.status_code == 200, response.text
    launched = response.json()

    payload = wait_for_completion(Path(launched["log_path"]))
    assert payload["status"] == "completed"

    events = read_events(Path(launched["log_path"]))
    requested = find_event(events, "agent.requested")
    assert requested is not None, "no agent.requested event in the run log"
    assert list(requested["payload"]["meta"]["owns"]) == ["src/"]
