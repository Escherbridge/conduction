"""Tests for GET /api/runs/<run_id>/relaunch -- the "Re-run" affordance.

The endpoint reshapes a finished run's manifest into a body POST /api/runs
would accept. The load-bearing part is the slug: POST /api/runs rejects a slug
whose run.jsonl already exists, so a relaunch spec that echoed the original
slug back would produce a guaranteed 409.
"""

from __future__ import annotations

import json
import time

import pytest
import requests

DRY_RUN_ENV = {"CONDUCTION_DRY_RUN": "1", "CONDUCTION_SCHEDULER": "0"}


@pytest.fixture
def repo(tmp_path):
    """A registered-looking target repo the server is allowed to launch into."""
    target = tmp_path / "workspace" / "demo-repo"
    (target / ".git").mkdir(parents=True)
    return target


@pytest.fixture
def server(app_server_factory, repo):
    env = dict(DRY_RUN_ENV)
    env["CONDUCTION_ALLOWED_ROOTS"] = str(repo.parent)
    return app_server_factory(env)


def launch(server, repo, slug, **overrides):
    payload = {
        "slug": slug,
        "target_repo": str(repo),
        "agents": [
            {
                "name": "alpha",
                "brief": "Do the thing.",
                "tools": ["Read", "Grep"],
                "owns": ["src/a.py"],
            }
        ],
        "synthesis": "Summarise what alpha found.",
        "max_turns": 7,
        "max_concurrency": 2,
    }
    payload.update(overrides)
    return requests.post(server.base_url + "/api/runs", json=payload, timeout=30)


def wait_for_manifest(repo, slug, timeout=30):
    """The manifest is written as the run starts; poll rather than sleep."""
    manifest_path = repo / ".agentgraph" / "runs" / slug / "mission.json"
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if manifest_path.exists():
            return json.loads(manifest_path.read_text(encoding="utf-8"))
        time.sleep(0.2)
    raise AssertionError(f"manifest never appeared at {manifest_path}")


def test_relaunch_spec_round_trips_the_manifest_with_a_free_slug(server, repo):
    response = launch(server, repo, "fix-auth")
    assert response.status_code in (200, 201, 202), response.text
    run_id = response.json()["run_id"]
    wait_for_manifest(repo, "fix-auth")

    spec = requests.get(
        server.base_url + "/api/runs/" + requests.utils.quote(run_id, safe="") + "/relaunch",
        timeout=10,
    ).json()

    assert spec["source_run_id"] == run_id
    assert spec["original_slug"] == "fix-auth"
    assert spec["slug"] == "fix-auth-2"  # the original is taken, so it is bumped
    assert spec["target_repo"] == str(repo)
    assert spec["synthesis"] == "Summarise what alpha found."
    assert spec["max_turns"] == 7
    assert spec["max_concurrency"] == 2
    assert [agent["name"] for agent in spec["agents"]] == ["alpha"]
    assert spec["agents"][0]["brief"] == "Do the thing."
    assert spec["agents"][0]["owns"] == ["src/a.py"]


def test_suggested_slug_is_actually_launchable(server, repo):
    """The point of the whole endpoint: POST the suggestion back and it works."""
    first = launch(server, repo, "cleanup")
    run_id = first.json()["run_id"]
    wait_for_manifest(repo, "cleanup")

    spec = requests.get(
        server.base_url + "/api/runs/" + requests.utils.quote(run_id, safe="") + "/relaunch",
        timeout=10,
    ).json()
    again = launch(server, repo, spec["slug"])
    assert again.status_code in (200, 201, 202), again.text
    assert again.json()["run_id"] != run_id


def take(repo, *slugs):
    runs = repo / ".agentgraph" / "runs"
    for slug in slugs:
        (runs / slug).mkdir(parents=True, exist_ok=True)
        (runs / slug / "run.jsonl").write_text("", encoding="utf-8")


def test_slug_series_skips_every_taken_number(server, repo):
    """`name`, `name-2` both taken -> the suggestion must be `name-3`, not `-2`."""
    from app import next_free_slug

    take(repo, "sweep", "sweep-2")
    assert next_free_slug(repo, "sweep") == "sweep-3"
    assert next_free_slug(repo, "sweep-2") == "sweep-3"
    assert next_free_slug(repo, "untouched") == "untouched"


def test_a_trailing_number_is_not_assumed_to_be_a_counter(repo):
    """`sprint-2024` is a name, not the 2024th run of `sprint`. Bumping it to
    `sprint-2025` would silently rename the user's mission."""
    from app import next_free_slug

    take(repo, "sprint-2024")
    assert next_free_slug(repo, "sprint-2024") == "sprint-2024-2"

    # ...but once the bare stem IS a run, the digits really are a counter.
    take(repo, "wave", "wave-2")
    assert next_free_slug(repo, "wave-2") == "wave-3"


def test_a_long_slug_is_trimmed_to_fit_rather_than_giving_up(repo):
    """A 64-char slug still has to be re-runnable: the stem is trimmed so the
    `-2` fits, instead of returning the colliding original."""
    from app import MAX_SLUG_LENGTH, next_free_slug

    long_slug = "x" * MAX_SLUG_LENGTH
    take(repo, long_slug)
    suggestion = next_free_slug(repo, long_slug)
    assert suggestion is not None
    assert suggestion != long_slug
    assert len(suggestion) <= MAX_SLUG_LENGTH
    assert suggestion.endswith("-2")


def test_exhausted_series_reports_none_instead_of_a_taken_slug(repo, monkeypatch):
    """None is the honest answer. Handing back the colliding slug would make the
    launch form prefill a value guaranteed to 409 while claiming success."""
    import app as app_module

    monkeypatch.setattr(app_module, "MAX_SLUG_SERIES", 3)
    take(repo, "full", "full-2", "full-3")
    assert app_module.next_free_slug(repo, "full") is None


def test_relaunch_reports_whether_the_target_repo_still_exists(server, repo):
    response = launch(server, repo, "moved")
    run_id = response.json()["run_id"]
    wait_for_manifest(repo, "moved")

    spec = requests.get(
        server.base_url + "/api/runs/" + requests.utils.quote(run_id, safe="") + "/relaunch",
        timeout=10,
    ).json()
    assert spec["target_repo_exists"] is True
    # `model` is deliberately absent: POST /api/runs ignores it.
    assert "model" not in spec


def test_rerun_launches_from_the_manifest_without_client_agents(server, repo):
    """POST /rerun is the only way a remote client can start work, so it must
    work with an EMPTY body -- everything comes from the stored manifest."""
    first = launch(server, repo, "nightly")
    source_run_id = first.json()["run_id"]
    wait_for_manifest(repo, "nightly")

    response = requests.post(
        server.base_url + "/api/runs/" + requests.utils.quote(source_run_id, safe="") + "/rerun",
        timeout=30,
    )
    assert response.status_code in (200, 201, 202), response.text
    launched = response.json()
    assert launched["slug"] == "nightly-2"
    assert launched["run_id"] != source_run_id

    # The new run carries the original's agents, taken from disk.
    manifest = wait_for_manifest(repo, "nightly-2")
    assert [agent["name"] for agent in manifest["agents"]] == ["alpha"]
    assert manifest["agents"][0]["brief"] == "Do the thing."


def test_rerun_ignores_any_agents_the_caller_tries_to_supply(server, repo):
    """The security property: a caller cannot smuggle briefs, tools or `owns`
    paths through /rerun. If this regresses, remote scope becomes arbitrary
    code execution on the host."""
    first = launch(server, repo, "bounded")
    source_run_id = first.json()["run_id"]
    wait_for_manifest(repo, "bounded")

    requests.post(
        server.base_url + "/api/runs/" + requests.utils.quote(source_run_id, safe="") + "/rerun",
        json={
            "agents": [{"name": "attacker", "brief": "rm -rf", "tools": ["Bash"]}],
            "slug": "attacker-chosen",
            "target_repo": "C:\\",
        },
        timeout=30,
    )
    manifest = wait_for_manifest(repo, "bounded-2")
    assert [agent["name"] for agent in manifest["agents"]] == ["alpha"]
    assert manifest["target_repo"] == str(repo)


def test_relaunch_404s_for_an_unknown_run(server):
    response = requests.get(
        server.base_url + "/api/runs/MISSION-nope@deadbeef/relaunch", timeout=10
    )
    assert response.status_code == 404
    assert "not found" in response.json()["error"].lower()
