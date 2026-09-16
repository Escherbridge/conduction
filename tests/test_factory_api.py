"""Live-server tests for the factory API (contract item 2).

A factory is an ordered list of waves; a factory run executes each wave as one
gated Mission, halts on the first failed gate and resumes from that wave.
Everything here runs with CONDUCTION_DRY_RUN=1, so agents are ScriptedWorkers
(no model calls) and the gates are trivial subprocesses.
"""

from __future__ import annotations

import json
import sys
import time
from pathlib import Path
from tempfile import TemporaryDirectory

import pytest
import requests

TERMINAL_FACTORY_STATUSES = ("completed", "failed", "interrupted")
POLL_TIMEOUT_SECONDS = 180


def gate_spec(exit_code: int) -> dict:
    """A command gate that simply exits with `exit_code`."""
    return {
        "command": {
            "argv": [sys.executable, "-c", f"raise SystemExit({exit_code})"],
        }
    }


def factory_spec(second_gate_exit: int) -> dict:
    return {
        "slug": "testfactory",
        "description": "two waves, dry run",
        "waves": [
            {
                "slug": "wave-one",
                "agents": [{"name": "alpha", "brief": "do the first thing", "owns": ["alpha.txt"]}],
                "gate": gate_spec(0),
            },
            {
                "slug": "wave-two",
                "agents": [{"name": "beta", "brief": "do the second thing", "owns": ["beta.txt"]}],
                "gate": gate_spec(second_gate_exit),
            },
        ],
    }


def write_spec(repo: Path, spec: dict) -> Path:
    spec_path = repo / ".agentgraph" / "factory.json"
    spec_path.parent.mkdir(parents=True, exist_ok=True)
    spec_path.write_text(json.dumps(spec, indent=2), encoding="utf-8")
    return spec_path


@pytest.fixture
def target_repo():
    """A throwaway repo under $HOME so it passes validate_target_repo's root check."""
    with TemporaryDirectory(dir=str(Path.home()), prefix="factory-repo-") as tmp:
        yield Path(tmp).resolve()


@pytest.fixture
def server(app_server_factory):
    return app_server_factory({"CONDUCTION_DRY_RUN": "1"})


def poll_until_terminal(base_url: str, factory_run_id: str) -> dict:
    deadline = time.monotonic() + POLL_TIMEOUT_SECONDS
    state: dict = {}
    while time.monotonic() < deadline:
        response = requests.get(f"{base_url}/api/factory/runs/{factory_run_id}", timeout=10)
        if response.status_code == 200:
            state = response.json()
            if state.get("status") in TERMINAL_FACTORY_STATUSES:
                return state
        time.sleep(1)
    raise AssertionError(f"factory run never reached a terminal status: {state}")


def resolve_app_run_id(base_url: str, wave: dict) -> str:
    """The composite run_id the /runs UI keys on.

    The contract has factory.py record compose_run_id() directly; until it does,
    a bare mission slug is resolved against GET /api/runs by slug.
    """
    run_id = wave.get("run_id")
    assert run_id, f"wave has no run_id: {wave}"
    if "@" in run_id:
        return run_id
    runs = requests.get(f"{base_url}/api/runs", timeout=30).json()
    matches = [run["run_id"] for run in runs if run["slug"] == run_id]
    assert matches, f"wave run_id {run_id!r} does not resolve to any run"
    return matches[0]


def launch(base_url: str, repo: Path) -> dict:
    response = requests.post(
        f"{base_url}/api/factory/runs",
        json={"target_repo": str(repo)},
        timeout=30,
    )
    assert response.status_code == 200, response.text
    return response.json()


def test_factory_run_completes_and_each_wave_has_a_resolvable_run(server, target_repo):
    write_spec(target_repo, factory_spec(second_gate_exit=0))

    launched = launch(server.base_url, target_repo)
    assert launched["waves"] == ["wave-one", "wave-two"]
    assert launched["target_repo"] == str(target_repo)
    assert launched["state_path"].endswith("state.json")

    state = poll_until_terminal(server.base_url, launched["factory_run_id"])
    assert state["status"] == "completed", state
    assert [wave["status"] for wave in state["waves"]] == ["passed", "passed"], state

    for wave in state["waves"]:
        run_id = resolve_app_run_id(server.base_url, wave)
        agents = requests.get(f"{server.base_url}/api/runs/{run_id}/agents", timeout=30)
        assert agents.status_code == 200, agents.text
        assert agents.json()["agents"], f"no agents mirrored for {run_id}"

    listing = requests.get(f"{server.base_url}/api/factory/runs", timeout=30)
    assert listing.status_code == 200
    ids = [item.get("factory_run_id") for item in listing.json()]
    assert launched["factory_run_id"] in ids


def test_failed_second_gate_halts_then_resume_completes(server, target_repo):
    write_spec(target_repo, factory_spec(second_gate_exit=3))

    launched = launch(server.base_url, target_repo)
    factory_run_id = launched["factory_run_id"]

    state = poll_until_terminal(server.base_url, factory_run_id)
    assert state["status"] == "failed", state
    assert [wave["status"] for wave in state["waves"]] == ["passed", "failed"], state

    # Fix the failing gate, then resume from the failed wave under the same id.
    write_spec(target_repo, factory_spec(second_gate_exit=0))

    resumed = requests.post(
        f"{server.base_url}/api/factory/runs/{factory_run_id}/resume",
        json={},
        timeout=30,
    )
    assert resumed.status_code == 200, resumed.text
    assert resumed.json()["start_wave"] == 1

    state = poll_until_terminal(server.base_url, factory_run_id)
    assert state["status"] == "completed", state
    assert [wave["status"] for wave in state["waves"]] == ["skipped", "passed"], state


def test_invalid_spec_is_rejected_with_the_error_list(server, target_repo):
    write_spec(
        target_repo,
        {
            "slug": "bad factory slug!",
            "waves": [
                {"slug": "dup", "agents": [], "gate": {}},
                {"slug": "dup", "agents": [{"brief": "no name"}], "gate": {"command": {}}},
            ],
        },
    )

    response = requests.post(
        f"{server.base_url}/api/factory/runs",
        json={"target_repo": str(target_repo)},
        timeout=30,
    )
    assert response.status_code == 400, response.text
    body = response.json()
    assert body.get("errors"), body
    assert isinstance(body["errors"], list) and len(body["errors"]) >= 1


def test_missing_spec_is_a_400(server, target_repo):
    response = requests.post(
        f"{server.base_url}/api/factory/runs",
        json={"target_repo": str(target_repo)},
        timeout=30,
    )
    assert response.status_code == 400, response.text
    assert "errors" in response.json()


def test_unknown_factory_run_is_404(server):
    response = requests.get(f"{server.base_url}/api/factory/runs/nope-123", timeout=30)
    assert response.status_code == 404


def test_factory_page_is_served(server):
    response = requests.get(f"{server.base_url}/factory", timeout=30)
    assert response.status_code == 200
    assert "text/html" in response.headers.get("content-type", "")
