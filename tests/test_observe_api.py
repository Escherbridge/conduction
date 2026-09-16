"""Fleet observability API (/api/observe/*).

Every test runs the server with CONDUCTION_DRY_RUN=1 (agents are scripted, ~1.5 s
each, no model call) and CONDUCTION_SCHEDULER=0 (no background schedule launches).
"""

from __future__ import annotations

import http.client
import json
import queue
import tempfile
import threading
import time
import uuid
from pathlib import Path
from urllib.parse import urlparse

import pytest
import requests

SERVER_ENV = {"CONDUCTION_DRY_RUN": "1", "CONDUCTION_SCHEDULER": "0"}
ACTIVITY_KEYS = {"run_id", "slug", "target_repo", "seq", "type", "worker", "summary", "ts"}


@pytest.fixture
def server(app_server_factory):
    return app_server_factory(dict(SERVER_ENV))


@pytest.fixture
def target_repo():
    """A throwaway repo under Path.home(), which is the default allowed root."""
    # ignore_cleanup_errors: on Windows the mission thread may still hold
    # run.jsonl open when the fixture unwinds; a leftover temp dir is not a failure.
    with tempfile.TemporaryDirectory(dir=str(Path.home()), ignore_cleanup_errors=True) as directory:
        yield str(Path(directory).resolve())


def launch_three_agent_run(server, target_repo: str) -> tuple[str, str]:
    """Launch a 3-agent serial dry run; returns (run_id, slug)."""
    slug = f"observe-{uuid.uuid4().hex[:8]}"
    body = {
        "slug": slug,
        "target_repo": target_repo,
        "agents": [
            {"name": f"worker-{index}", "brief": "note something", "tools": ["Read"]}
            for index in range(1, 4)
        ],
        "max_turns": 3,
        "max_concurrency": 1,
    }
    response = requests.post(f"{server.base_url}/api/runs", json=body, timeout=30)
    assert response.status_code == 200, response.text
    return response.json()["run_id"], slug


def test_summary_has_every_contract_key_and_counts_all_runs(server):
    payload = requests.get(f"{server.base_url}/api/observe/summary", timeout=30).json()

    assert set(payload) == {
        "active_runs",
        "live_agents",
        "cost_today_usd",
        "cost_total_usd",
        "gate_pass_rate_last_20",
        "violations_24h",
        "runs_by_status",
        "projects",
    }
    assert isinstance(payload["runs_by_status"], dict)
    assert payload["gate_pass_rate_last_20"] is None or (
        0.0 <= payload["gate_pass_rate_last_20"] <= 1.0
    )
    assert payload["cost_total_usd"] >= payload["cost_today_usd"] >= 0
    assert payload["projects"] >= 1

    known_runs = requests.get(f"{server.base_url}/api/runs", timeout=30).json()
    assert len(known_runs) >= 8, f"expected the historical runs, saw {len(known_runs)}"
    assert sum(payload["runs_by_status"].values()) == len(known_runs)


def test_agents_shows_a_freshly_launched_run_as_running(server, target_repo):
    run_id, slug = launch_three_agent_run(server, target_repo)

    deadline = time.monotonic() + 2.0
    mine = []
    while time.monotonic() < deadline and not mine:
        agents = requests.get(f"{server.base_url}/api/observe/agents", timeout=10).json()
        mine = [agent for agent in agents if agent["run_id"] == run_id]
        if not mine:
            time.sleep(0.1)

    assert mine, f"run {run_id} never appeared in /api/observe/agents within 2 s"
    for agent in mine:
        assert agent["status"] == "running"
        assert agent["slug"] == slug
        assert agent["target_repo"] == target_repo
        assert set(agent) == {
            "run_id",
            "slug",
            "target_repo",
            "agent",
            "model",
            "status",
            "turns",
            "cost_usd",
            "last_finding",
            "last_event_type",
            "last_event_ts",
        }


def test_timeline_rows_have_the_contract_shape(server, target_repo):
    launch_three_agent_run(server, target_repo)

    rows = []
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline and not rows:
        rows = requests.get(f"{server.base_url}/api/observe/timeline?limit=200", timeout=30).json()
        if not rows:
            time.sleep(0.5)

    assert rows, "timeline stayed empty"
    assert len(rows) <= 200
    for row in rows:
        assert set(row) == ACTIVITY_KEYS


def _read_activity_events(base_url: str, results: queue.Queue, stop: threading.Event) -> None:
    """Read the SSE feed with http.client and push each `activity` payload onto results."""
    parsed = urlparse(base_url)
    connection = http.client.HTTPConnection(parsed.hostname, parsed.port, timeout=15)
    try:
        connection.request("GET", "/api/observe/feed", headers={"Accept": "text/event-stream"})
        response = connection.getresponse()
        assert response.status == 200
        assert "text/event-stream" in response.getheader("Content-Type", "")
        event_name = None
        while not stop.is_set():
            raw = response.readline()
            if not raw:
                return
            line = raw.decode("utf-8").rstrip("\r\n")
            if line.startswith("event:"):
                event_name = line.split(":", 1)[1].strip()
            elif line.startswith("data:") and event_name == "activity":
                results.put(json.loads(line.split(":", 1)[1].strip()))
    except Exception as error:  # surfaced by the assertion in the test body
        results.put({"__error__": repr(error)})
    finally:
        connection.close()


def test_feed_streams_an_activity_event_for_a_new_finding(server, target_repo):
    run_id, _slug = launch_three_agent_run(server, target_repo)

    results: queue.Queue = queue.Queue()
    stop = threading.Event()
    reader = threading.Thread(
        target=_read_activity_events, args=(server.base_url, results, stop), daemon=True
    )
    reader.start()

    deadline = time.monotonic() + 10
    matched = None
    while time.monotonic() < deadline and matched is None:
        try:
            event = results.get(timeout=0.5)
        except queue.Empty:
            continue
        assert "__error__" not in event, event["__error__"]
        assert set(event) == ACTIVITY_KEYS
        if event["run_id"] == run_id and event["type"] == "finding.recorded":
            matched = event

    stop.set()
    reader.join(timeout=5)

    assert matched is not None, f"no activity frame for {run_id}'s finding within 10 s"
    assert matched["summary"]
    assert matched["seq"] > 0
