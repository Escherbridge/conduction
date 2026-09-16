"""Wave K item 2: manifest, story, $0 replay and delete over HTTP.

Every test runs the server with CONDUCTION_DRY_RUN=1 (ScriptedWorker, ~1.5 s
per agent, no model calls), so the whole file is free to run.
"""

from __future__ import annotations

import json
import tempfile
import time
import uuid
from pathlib import Path

import pytest
import requests

MISSION_TIMEOUT_SECONDS = 120


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


def two_agent_body(target_repo: str, slug: str) -> dict:
    return {
        "slug": slug,
        "target_repo": target_repo,
        "agents": [
            {"name": "alpha", "brief": "look around", "tools": ["Read"]},
            {"name": "beta", "brief": "look again", "tools": ["Read"]},
        ],
        "max_turns": 3,
        "max_concurrency": 2,
    }


def read_events(log_path: Path) -> list[dict]:
    """Unwrap the JSONL envelope: every line is {"seq", "run_id", "event": {...}}."""
    events = []
    for line in log_path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            envelope = json.loads(line)
        except ValueError:
            continue
        events.append(envelope.get("event", envelope))
    return events


def wait_for_completion(log_path: Path, timeout: float = MISSION_TIMEOUT_SECONDS) -> list[dict]:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if log_path.exists():
            events = read_events(log_path)
            if any(event.get("type") == "mission.completed" for event in events):
                return events
        time.sleep(0.5)
    raise AssertionError(f"mission.completed never appeared in {log_path}")


def launch_two_agents(server, target_repo: str) -> tuple[str, str, Path]:
    slug = unique_slug("replay")
    response = requests.post(
        f"{server.base_url}/api/runs", json=two_agent_body(target_repo, slug), timeout=30
    )
    assert response.status_code == 200, response.text
    payload = response.json()
    log_path = Path(target_repo) / ".agentgraph" / "runs" / slug / "run.jsonl"
    return payload["run_id"], slug, log_path


def test_manifest_written_and_served(server, target_repo):
    run_id, slug, log_path = launch_two_agents(server, target_repo)

    manifest_path = log_path.parent / "mission.json"
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline and not manifest_path.exists():
        time.sleep(0.2)
    assert manifest_path.exists(), "launch must write mission.json into the run dir"

    on_disk = json.loads(manifest_path.read_text(encoding="utf-8"))
    assert on_disk["kind"] == "mission"
    assert on_disk["slug"] == slug
    assert [agent["name"] for agent in on_disk["agents"]] == ["alpha", "beta"]

    response = requests.get(f"{server.base_url}/api/runs/{run_id}/manifest", timeout=30)
    assert response.status_code == 200
    assert response.json() == on_disk

    wait_for_completion(log_path)


def test_story_is_markdown_naming_both_agents(server, target_repo):
    run_id, _slug, log_path = launch_two_agents(server, target_repo)
    wait_for_completion(log_path)

    response = requests.get(f"{server.base_url}/api/runs/{run_id}/story", timeout=60)
    assert response.status_code == 200
    assert "text/markdown" in response.headers["content-type"]
    assert "alpha" in response.text and "beta" in response.text

    missing = requests.get(f"{server.base_url}/api/runs/MISSION-nope@0000/story", timeout=30)
    assert missing.status_code == 404


def test_replay_reproduces_the_run_for_zero_dollars(server, target_repo):
    run_id, _slug, log_path = launch_two_agents(server, target_repo)
    original_events = wait_for_completion(log_path)

    response = requests.post(f"{server.base_url}/api/runs/{run_id}/replay", timeout=60)
    assert response.status_code == 200, response.text
    payload = response.json()
    assert "-replay-" in payload["slug"]
    assert payload["parent_run_id"] == run_id
    assert payload["run_id"] != run_id

    replay_log = Path(target_repo) / ".agentgraph" / "runs" / payload["slug"] / "run.jsonl"
    replay_events = wait_for_completion(replay_log)

    assert [event["type"] for event in replay_events] == [
        event["type"] for event in original_events
    ]
    responded = [event for event in replay_events if event["type"] == "agent.responded"]
    assert responded, "replay must re-serve the recorded agent responses"
    # cost_usd is serialized as a Decimal string; a $0 replay must total zero.
    assert all(float(event["payload"].get("cost_usd", 0)) == 0 for event in responded)

    runs = requests.get(f"{server.base_url}/api/runs", timeout=30).json()
    entry = next(run for run in runs if run["run_id"] == payload["run_id"])
    assert entry["kind"] == "replay"
    assert entry["parent_run_id"] == run_id


def test_delete_removes_run_dir_and_rows(server, target_repo):
    run_id, _slug, log_path = launch_two_agents(server, target_repo)
    wait_for_completion(log_path)

    replay = requests.post(f"{server.base_url}/api/runs/{run_id}/replay", timeout=60).json()
    replay_dir = Path(target_repo) / ".agentgraph" / "runs" / replay["slug"]
    wait_for_completion(replay_dir / "run.jsonl")

    deleted = requests.delete(f"{server.base_url}/api/runs/{replay['run_id']}", timeout=60)
    assert deleted.status_code == 204
    assert not replay_dir.exists()

    agents = requests.get(f"{server.base_url}/api/runs/{replay['run_id']}/agents", timeout=30)
    assert agents.status_code == 404


def test_delete_refuses_a_running_mission(server, target_repo):
    slug = unique_slug("busy")
    body = {
        "slug": slug,
        "target_repo": target_repo,
        "agents": [
            {"name": f"agent{index}", "brief": "wait", "tools": ["Read"]} for index in range(4)
        ],
        "max_turns": 3,
        "max_concurrency": 1,
    }
    launched = requests.post(f"{server.base_url}/api/runs", json=body, timeout=30)
    assert launched.status_code == 200, launched.text
    run_id = launched.json()["run_id"]

    log_path = Path(target_repo) / ".agentgraph" / "runs" / slug / "run.jsonl"
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline and not log_path.exists():
        time.sleep(0.1)

    refused = requests.delete(f"{server.base_url}/api/runs/{run_id}", timeout=30)
    assert refused.status_code == 409, refused.text

    wait_for_completion(log_path)
