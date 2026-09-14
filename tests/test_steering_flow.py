"""Comprehensive steering flow tests (launch → interrupt → resume) in dry-run mode.

Tests the complete steering lifecycle using CONDUCTION_DRY_RUN=1 with a
ScriptedWorker to avoid API costs. Every assertion uses real HTTP calls against
a live server instance spawned per test.
"""

import json
import time
from pathlib import Path
from tempfile import TemporaryDirectory

import pytest
import requests


def read_jsonl(path: Path) -> list:
    """Read JSONL log and return list of event dicts."""
    if not path.exists():
        return []
    events = []
    with path.open("r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                envelope = json.loads(line)
                events.append(envelope.get("event", envelope))  # JSONL wraps each event
    return events


def poll_until_terminal(base_url: str, run_id: str, timeout_seconds: float = 30) -> str:
    """Poll GET /api/runs until the run leaves 'running' status.

    Returns the final status or raises TimeoutError.
    """
    deadline = time.time() + timeout_seconds
    while time.time() < deadline:
        resp = requests.get(f"{base_url}/api/runs")
        assert resp.status_code == 200, f"GET /api/runs failed: {resp.status_code}"
        runs = resp.json()
        for run in runs:
            if run["run_id"] == run_id:
                status = run["status"]
                if status != "running":
                    return status
        time.sleep(0.5)
    raise TimeoutError(f"Run {run_id} still running after {timeout_seconds}s")


def test_launch_and_complete(app_server_factory):
    """a. Launch a 3-agent mission and verify it completes successfully."""
    with TemporaryDirectory(dir=Path.home()) as tmpdir:
        server = app_server_factory(env_overrides={"CONDUCTION_DRY_RUN": "1"})
        base_url = server.base_url
        target_repo = Path(tmpdir)

        # Launch mission with slug "dryrun-a" and 3 agents
        payload = {
            "slug": "dryrun-a",
            "target_repo": str(target_repo),
            "agents": [
                {"name": "agent1", "brief": "Task 1", "tools": ["Read"]},
                {"name": "agent2", "brief": "Task 2", "tools": ["Read"]},
                {"name": "agent3", "brief": "Task 3", "tools": ["Read"]},
            ],
            "max_turns": 20,
            "max_concurrency": 1,  # serial dispatch so an interrupt can stop later agents
        }

        resp = requests.post(f"{base_url}/api/runs", json=payload)
        assert resp.status_code in (200, 202), f"Launch failed: {resp.status_code} {resp.text}"
        data = resp.json()
        run_id = data["run_id"]

        # run_id format: MISSION-dryrun-a@<8 hex>
        assert run_id.startswith("MISSION-dryrun-a@"), f"Unexpected run_id format: {run_id}"
        assert len(run_id.split("@")[1]) == 8, "run_id hash should be 8 hex chars"

        # Verify .agentgraph directory structure
        agentgraph_dir = target_repo / ".agentgraph"
        assert agentgraph_dir.exists(), ".agentgraph directory should exist"
        gitignore = agentgraph_dir / ".gitignore"
        assert gitignore.exists(), ".agentgraph/.gitignore should exist"
        assert gitignore.read_text().strip() == "*", ".gitignore should contain '*'"

        run_dir = target_repo / ".agentgraph" / "runs" / "dryrun-a"
        assert run_dir.exists(), "run directory should exist"
        log_path = run_dir / "run.jsonl"
        assert log_path.exists(), "run.jsonl should exist"

        # Poll until completion
        final_status = poll_until_terminal(base_url, run_id, timeout_seconds=30)
        assert final_status == "completed", f"Expected 'completed', got '{final_status}'"

        # Verify JSONL contents
        events = read_jsonl(log_path)
        assert len(events) > 0, "Log should have events"

        # Last event should be mission.completed with status=completed
        last_event = events[-1]
        assert last_event["type"] == "mission.completed", f"Last event type: {last_event['type']}"
        assert last_event["payload"]["status"] == "completed", f"Mission status: {last_event['payload']}"

        server.stop()


def test_interrupt_mid_run(app_server_factory):
    """c. Launch a 4-agent mission, interrupt it within ~1s, verify it stops early."""
    with TemporaryDirectory(dir=Path.home()) as tmpdir:
        server = app_server_factory(env_overrides={"CONDUCTION_DRY_RUN": "1"})
        base_url = server.base_url
        target_repo = Path(tmpdir)

        # Launch mission with slug "dryrun-b" and 4 agents
        payload = {
            "slug": "dryrun-b",
            "target_repo": str(target_repo),
            "agents": [
                {"name": "agent1", "brief": "Task 1", "tools": ["Read"]},
                {"name": "agent2", "brief": "Task 2", "tools": ["Read"]},
                {"name": "agent3", "brief": "Task 3", "tools": ["Read"]},
                {"name": "agent4", "brief": "Task 4", "tools": ["Read"]},
            ],
            "max_turns": 20,
            "max_concurrency": 1,  # serial dispatch so an interrupt can stop later agents
        }

        resp = requests.post(f"{base_url}/api/runs", json=payload)
        assert resp.status_code in (200, 202), f"Launch failed: {resp.status_code} {resp.text}"
        data = resp.json()
        run_id = data["run_id"]

        # Wait briefly for mission to start, then interrupt
        time.sleep(1.0)
        resp = requests.post(f"{base_url}/api/runs/{run_id}/interrupt")
        assert resp.status_code == 202, f"Interrupt failed: {resp.status_code} {resp.text}"

        # Wait for mission to finish
        final_status = poll_until_terminal(base_url, run_id, timeout_seconds=30)
        assert final_status in ("completed", "failed", "stale"), f"Unexpected status: {final_status}"

        # Verify interrupt.signal exists
        run_dir = target_repo / ".agentgraph" / "runs" / "dryrun-b"
        interrupt_signal = run_dir / "interrupt.signal"
        assert interrupt_signal.exists(), "interrupt.signal should exist after interrupt"

        # Count agent.responded events - should be fewer than 4
        log_path = run_dir / "run.jsonl"
        events = read_jsonl(log_path)
        agent_responded = [e for e in events if e["type"] == "agent.responded"
                          and e["payload"].get("worker") in ["agent1", "agent2", "agent3", "agent4"]]
        assert len(agent_responded) < 4, f"Expected <4 agent responses, got {len(agent_responded)}"

        server.stop()


def test_resume_with_cache(app_server_factory):
    """d. Resume an interrupted mission, verify cache hits for completed agents."""
    with TemporaryDirectory(dir=Path.home()) as tmpdir:
        server = app_server_factory(env_overrides={"CONDUCTION_DRY_RUN": "1"})
        base_url = server.base_url
        target_repo = Path(tmpdir)

        # Launch mission
        payload = {
            "slug": "dryrun-resume",
            "target_repo": str(target_repo),
            "agents": [
                {"name": "agent1", "brief": "Task 1", "tools": ["Read"]},
                {"name": "agent2", "brief": "Task 2", "tools": ["Read"]},
                {"name": "agent3", "brief": "Task 3", "tools": ["Read"]},
                {"name": "agent4", "brief": "Task 4", "tools": ["Read"]},
            ],
            "max_turns": 20,
            "max_concurrency": 1,  # serial dispatch so an interrupt can stop later agents
        }

        resp = requests.post(f"{base_url}/api/runs", json=payload)
        assert resp.status_code in (200, 202)
        original_run_id = resp.json()["run_id"]

        # Interrupt after agent1 and agent2 have landed (1.5 s each, serial) but before all four
        time.sleep(3.2)
        resp = requests.post(f"{base_url}/api/runs/{original_run_id}/interrupt")
        assert resp.status_code == 202

        poll_until_terminal(base_url, original_run_id, timeout_seconds=30)

        # Resume with edited brief for one agent
        resume_payload = {
            "original_agents": payload["agents"],
            "agent_edits": [
                {"name": "agent1", "brief": "Task 1 (revised)"}
            ],
            "max_turns": 20,
            "max_concurrency": 1,  # serial dispatch so an interrupt can stop later agents
        }

        resp = requests.post(f"{base_url}/api/runs/{original_run_id}/resume", json=resume_payload)
        assert resp.status_code in (200, 202), f"Resume failed: {resp.status_code} {resp.text}"
        data = resp.json()
        resume_run_id = data["run_id"]
        assert resume_run_id != original_run_id, "Resume should create a new run_id"

        # Wait for resumed run to complete
        final_status = poll_until_terminal(base_url, resume_run_id, timeout_seconds=30)
        assert final_status == "completed", f"Expected 'completed', got '{final_status}'"

        # Verify resumed log has all 4 agent.responded events
        resume_slug = data["slug"]
        resume_dir = target_repo / ".agentgraph" / "runs" / resume_slug
        resume_log = resume_dir / "run.jsonl"
        events = read_jsonl(resume_log)

        agent_responded = [e for e in events if e["type"] == "agent.responded"
                          and e["payload"].get("worker") in ["agent1", "agent2", "agent3", "agent4"]]
        assert len(agent_responded) == 4, f"Expected 4 agent responses, got {len(agent_responded)}"

        # Cache hits never reach the worker, so the dry-run invocation file is the
        # honest record of who actually executed in the resumed run.
        original_dir = target_repo / ".agentgraph" / "runs" / "dryrun-resume"
        original_invocations = (original_dir / "dry-run-invocations.txt").read_text().split()
        assert 2 <= len(original_invocations) < 4, f"Interrupt should leave 2-3 executed agents, got {original_invocations}"

        resumed_invocations = (resume_dir / "dry-run-invocations.txt").read_text().split()
        assert "agent1" in resumed_invocations, "Edited agent must re-run"
        assert "agent2" not in resumed_invocations, "Unchanged, already-finished agent must be served from cache"
        assert len(resumed_invocations) < 4, f"Expected cache hits to skip agents, got {resumed_invocations}"

        server.stop()


def test_duplicate_slug_conflict(app_server_factory):
    """e. Attempt to launch a mission with a slug that already exists → 409."""
    with TemporaryDirectory(dir=Path.home()) as tmpdir:
        server = app_server_factory(env_overrides={"CONDUCTION_DRY_RUN": "1"})
        base_url = server.base_url
        target_repo = Path(tmpdir)

        payload = {
            "slug": "dryrun-conflict",
            "target_repo": str(target_repo),
            "agents": [
                {"name": "agent1", "brief": "Task 1", "tools": ["Read"]},
            ],
            "max_turns": 20,
        }

        # First launch
        resp = requests.post(f"{base_url}/api/runs", json=payload)
        assert resp.status_code in (200, 202), f"First launch failed: {resp.status_code}"
        run_id = resp.json()["run_id"]

        # Wait for completion
        poll_until_terminal(base_url, run_id, timeout_seconds=30)

        # Second launch with same slug → 409 Conflict
        resp = requests.post(f"{base_url}/api/runs", json=payload)
        assert resp.status_code == 409, f"Expected 409, got {resp.status_code}"
        assert "run_id" in resp.json(), "409 response should include existing run_id"

        server.stop()


def test_full_flow_integration(app_server_factory):
    """b. Integration test: launch → poll → verify JSONL structure."""
    with TemporaryDirectory(dir=Path.home()) as tmpdir:
        server = app_server_factory(env_overrides={"CONDUCTION_DRY_RUN": "1"})
        base_url = server.base_url
        target_repo = Path(tmpdir)

        payload = {
            "slug": "dryrun-full",
            "target_repo": str(target_repo),
            "agents": [
                {"name": "explorer", "brief": "Explore the codebase", "tools": ["Read", "Grep"]},
                {"name": "analyzer", "brief": "Analyze findings", "tools": ["Read"]},
            ],
            "synthesis": "Synthesize all findings",
            "max_turns": 20,
            "max_concurrency": 2,
        }

        resp = requests.post(f"{base_url}/api/runs", json=payload)
        assert resp.status_code in (200, 202)
        run_id = resp.json()["run_id"]

        # Poll until not running
        final_status = poll_until_terminal(base_url, run_id, timeout_seconds=30)
        assert final_status == "completed"

        # Read and verify JSONL structure
        run_dir = target_repo / ".agentgraph" / "runs" / "dryrun-full"
        log_path = run_dir / "run.jsonl"
        events = read_jsonl(log_path)

        # Should have: mission.started, agent.requested x2, agent.responded x2,
        # synthesis.requested, synthesis.responded, mission.completed
        event_types = [e["type"] for e in events]
        assert "mission.started" in event_types, "Should have mission.started"
        assert "mission.completed" in event_types, "Should have mission.completed"

        # Last event is mission.completed with status=completed
        last = events[-1]
        assert last["type"] == "mission.completed"
        assert last["payload"]["status"] == "completed"

        server.stop()
