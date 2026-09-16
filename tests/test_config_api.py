"""End-to-end tests for the rules/goals/schedules API and the pages it feeds.

Every test drives a real server (app_server_factory) with CONDUCTION_DRY_RUN=1
(ScriptedWorker: free, ~1.5 s/agent) and CONDUCTION_SCHEDULER=0 (the background
ticker must not race the explicit run-now assertions).
"""

from __future__ import annotations

import json
import shutil
import time
from pathlib import Path

import pytest
import requests

DRY_RUN_ENV = {"CONDUCTION_DRY_RUN": "1", "CONDUCTION_SCHEDULER": "0"}

ECO_RULE = {
    "id": "eco-1",
    "text": "Never commit secrets to the repository.",
    "scope": "all",
    "enabled": True,
}
PROJECT_RULE = {
    "id": "proj-1",
    "text": "Write a test for every behaviour you change.",
    "scope": "writers",
    "enabled": True,
}


REPO_ROOT = Path(__file__).resolve().parent.parent
KNOWN_REPOS_PATH = REPO_ROOT / ".agentgraph" / "known_repos.json"


def prune_known_repos() -> None:
    """Drop registrations pointing at directories that no longer exist.

    Registering a repo is permanent by design, so without this every test run
    would leave an entry behind and repos_to_scan() (an rglob per repo, on every
    /api/runs call) would get slower forever.
    """
    if not KNOWN_REPOS_PATH.exists():
        return
    try:
        entries = json.loads(KNOWN_REPOS_PATH.read_text(encoding="utf-8"))
    except (ValueError, OSError):
        return
    live = [entry for entry in entries if isinstance(entry, str) and Path(entry).is_dir()]
    if live != entries:
        KNOWN_REPOS_PATH.write_text(json.dumps(live, indent=2), encoding="utf-8")


@pytest.fixture
def server(app_server_factory):
    # Prune before boot: the fixture only allows /api/runs 2 s to answer, and
    # that scan walks every registered repo, so stale registrations from earlier
    # runs make server startup itself time out.
    prune_known_repos()
    return app_server_factory(DRY_RUN_ENV)


@pytest.fixture
def temp_repo():
    """A repo under Path.home() so validate_target_repo's default root accepts it."""
    prune_known_repos()
    root = Path.home() / ".conduction-test-repos"
    root.mkdir(parents=True, exist_ok=True)
    repo = root / f"cfg-{int(time.time() * 1000)}"
    (repo / ".agentgraph").mkdir(parents=True)
    try:
        yield repo.resolve()
    finally:
        shutil.rmtree(repo, ignore_errors=True)
        prune_known_repos()


def register(server, repo: Path) -> dict:
    response = requests.post(
        f"{server.base_url}/api/projects",
        json={"target_repo": str(repo), "name": "Temp Repo"},
        timeout=30,
    )
    assert response.status_code == 200, response.text
    return response.json()


def test_put_ecosystem_round_trips_a_rule(server):
    put = requests.put(
        f"{server.base_url}/api/ecosystem",
        json={"schema": 1, "rules": [ECO_RULE], "goals": [], "schedules": []},
        timeout=30,
    )
    assert put.status_code == 200, put.text

    got = requests.get(f"{server.base_url}/api/ecosystem", timeout=30).json()
    assert [rule["id"] for rule in got["rules"]] == ["eco-1"]
    assert got["rules"][0]["text"] == ECO_RULE["text"]


def test_invalid_rule_is_rejected_with_400(server):
    bad = requests.put(
        f"{server.base_url}/api/ecosystem",
        json={
            "schema": 1,
            "rules": [{"id": "x", "scope": "nonsense"}],
            "goals": [],
            "schedules": [],
        },
        timeout=30,
    )
    assert bad.status_code == 400, bad.text
    assert bad.json()["errors"]


def test_post_projects_registers_repo_and_writes_project_json(server, temp_repo):
    item = register(server, temp_repo)
    assert item["target_repo"] == str(temp_repo)
    assert item["name"] == "Temp Repo"
    assert (temp_repo / ".agentgraph" / "project.json").exists()

    listed = requests.get(f"{server.base_url}/api/projects", timeout=30).json()
    assert item["repo_key"] in {project["repo_key"] for project in listed}


def test_project_put_reflects_rule_goal_and_schedule(server, temp_repo):
    key = register(server, temp_repo)["repo_key"]
    document = {
        "schema": 1,
        "name": "Temp Repo",
        "rules": [PROJECT_RULE],
        "goals": [
            {
                "id": "g-1",
                "title": "Ship the harness",
                "description": "Wave L",
                "status": "open",
                "linked_runs": [],
                "updated_at": "2026-01-01T00:00:00+00:00",
            }
        ],
        "schedules": [
            {
                "id": "s-1",
                "kind": "factory",
                "factory_path": ".agentgraph/factory.json",
                "every": "1h",
                "cron": None,
                "enabled": True,
                "last_run_at": None,
                "last_factory_run_id": None,
            }
        ],
    }
    put = requests.put(f"{server.base_url}/api/projects/{key}", json=document, timeout=30)
    assert put.status_code == 200, put.text

    got = requests.get(f"{server.base_url}/api/projects/{key}", timeout=30).json()
    assert got["rules"][0]["text"] == PROJECT_RULE["text"]
    assert got["goals"][0]["id"] == "g-1"
    assert got["schedules"][0]["every"] == "1h"
    assert got["target_repo"] == str(temp_repo)


def test_schedules_listing_carries_next_due(server, temp_repo):
    key = register(server, temp_repo)["repo_key"]
    requests.put(
        f"{server.base_url}/api/projects/{key}",
        json={
            "schema": 1,
            "name": "Temp Repo",
            "rules": [],
            "goals": [],
            "schedules": [
                {
                    "id": "s-due",
                    "kind": "factory",
                    "factory_path": ".agentgraph/factory.json",
                    "every": "1h",
                    "cron": None,
                    "enabled": True,
                    "last_run_at": None,
                    "last_factory_run_id": None,
                }
            ],
        },
        timeout=30,
    ).raise_for_status()

    schedules = requests.get(f"{server.base_url}/api/schedules", timeout=30).json()
    mine = [item for item in schedules if item["id"] == "s-due"]
    assert mine, schedules
    assert mine[0]["target_repo"] == str(temp_repo)
    assert mine[0]["source"] == key
    assert "next_due" in mine[0]


def test_effective_rules_merges_ecosystem_and_project(server, temp_repo):
    key = register(server, temp_repo)["repo_key"]
    requests.put(
        f"{server.base_url}/api/ecosystem",
        json={"schema": 1, "rules": [ECO_RULE], "goals": [], "schedules": []},
        timeout=30,
    ).raise_for_status()
    requests.put(
        f"{server.base_url}/api/projects/{key}",
        json={
            "schema": 1,
            "name": "Temp Repo",
            "rules": [PROJECT_RULE],
            "goals": [],
            "schedules": [],
        },
        timeout=30,
    ).raise_for_status()

    effective = requests.get(
        f"{server.base_url}/api/rules/effective",
        params={"target_repo": str(temp_repo)},
        timeout=30,
    ).json()
    texts = [rule["text"] for rule in effective["rules"]]
    assert texts == [ECO_RULE["text"], PROJECT_RULE["text"]]
    assert "RULES (binding)" in effective["writer_block"]
    assert PROJECT_RULE["text"] in effective["writer_block"]
    # A writers-scoped rule must not leak into a reader's block.
    assert PROJECT_RULE["text"] not in effective["reader_block"]


def wait_for_run(server, run_id: str, timeout: float = 90) -> str | None:
    deadline = time.monotonic() + timeout
    status = None
    while time.monotonic() < deadline:
        runs = requests.get(f"{server.base_url}/api/runs", timeout=30).json()
        for run in runs:
            if run["run_id"] == run_id:
                status = run["status"]
        if status in ("completed", "failed", "errored", "stale"):
            return status
        time.sleep(1)
    return status


def test_launched_mission_carries_binding_rules(server, temp_repo):
    key = register(server, temp_repo)["repo_key"]
    requests.put(
        f"{server.base_url}/api/ecosystem",
        json={"schema": 1, "rules": [ECO_RULE], "goals": [], "schedules": []},
        timeout=30,
    ).raise_for_status()
    requests.put(
        f"{server.base_url}/api/projects/{key}",
        json={
            "schema": 1,
            "name": "Temp Repo",
            "rules": [PROJECT_RULE],
            "goals": [],
            "schedules": [],
        },
        timeout=30,
    ).raise_for_status()

    launch = requests.post(
        f"{server.base_url}/api/runs",
        json={
            "slug": "rules-check",
            "target_repo": str(temp_repo),
            "agents": [
                {"name": "writer", "brief": "Change a file.", "tools": ["Read", "Write", "Edit"]}
            ],
        },
        timeout=60,
    )
    assert launch.status_code == 200, launch.text
    run_id = launch.json()["run_id"]
    assert wait_for_run(server, run_id) == "completed"

    manifest = json.loads(
        (temp_repo / ".agentgraph" / "runs" / "rules-check" / "mission.json").read_text(
            encoding="utf-8"
        )
    )
    brief = manifest["agents"][0]["brief"]
    assert brief.startswith("RULES (binding)"), brief[:200]
    assert ECO_RULE["text"] in brief
    assert PROJECT_RULE["text"] in brief

    findings = requests.get(f"{server.base_url}/api/runs/{run_id}/findings", timeout=30).json()
    assert any(finding["topic"] == "rules" for finding in findings), findings


def test_run_now_launches_the_schedules_factory(server, temp_repo):
    key = register(server, temp_repo)["repo_key"]
    factory_spec = {
        "slug": "temp-factory",
        "description": "two dry-run waves",
        "waves": [
            {
                "slug": "wave-one",
                "agents": [{"name": "scout", "brief": "Look around.", "tools": ["Read"]}],
            },
            {
                "slug": "wave-two",
                "agents": [{"name": "scribe", "brief": "Write it down.", "tools": ["Read"]}],
            },
        ],
    }
    (temp_repo / ".agentgraph" / "factory.json").write_text(
        json.dumps(factory_spec), encoding="utf-8"
    )
    requests.put(
        f"{server.base_url}/api/projects/{key}",
        json={
            "schema": 1,
            "name": "Temp Repo",
            "rules": [],
            "goals": [],
            "schedules": [
                {
                    "id": "s-now",
                    "kind": "factory",
                    "factory_path": ".agentgraph/factory.json",
                    "every": "1d",
                    "cron": None,
                    "enabled": True,
                    "last_run_at": None,
                    "last_factory_run_id": None,
                }
            ],
        },
        timeout=30,
    ).raise_for_status()

    started = requests.post(f"{server.base_url}/api/schedules/s-now/run-now", timeout=60)
    assert started.status_code == 200, started.text
    factory_run_id = started.json()["factory_run_id"]

    deadline = time.monotonic() + 180
    state = {}
    while time.monotonic() < deadline:
        response = requests.get(f"{server.base_url}/api/factory/runs/{factory_run_id}", timeout=30)
        if response.status_code == 200:
            state = response.json()
            if state.get("status") in ("completed", "failed", "halted", "errored"):
                break
        time.sleep(2)
    assert state.get("status") == "completed", state

    # run-now must stamp the owning document so the ticker does not re-fire it.
    project = requests.get(f"{server.base_url}/api/projects/{key}", timeout=30).json()
    assert project["schedules"][0]["last_factory_run_id"] == factory_run_id
    assert project["schedules"][0]["last_run_at"]


@pytest.mark.parametrize("path", ["/projects", "/settings"])
def test_config_pages_render(server, path):
    response = requests.get(f"{server.base_url}{path}", timeout=30)
    assert response.status_code == 200
    assert "text/html" in response.headers["content-type"]
    assert "<html" in response.text.lower()


def test_project_detail_page_renders(server, temp_repo):
    key = register(server, temp_repo)["repo_key"]
    response = requests.get(f"{server.base_url}/projects/{key}", timeout=30)
    assert response.status_code == 200
    assert "text/html" in response.headers["content-type"]
    assert "<html" in response.text.lower()
