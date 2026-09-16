"""Manifests: round-trip, identity-preserving rebuild, and $0 replay.

Offline throughout — ScriptedWorker under Mission's FrozenClock, same style as
tests/test_mission.py and tests/test_mission_hardening.py.
"""

from __future__ import annotations

import json
from pathlib import Path

from agentgraph import AgentSpec, Mission, ScriptedWorker
from agentgraph.factory import FactoryRunner, FactorySpec, FactoryWave
from agentgraph.manifest import (
    MANIFEST_FILENAME,
    manifest_from_request,
    mission_from_manifest,
    read_mission_manifest,
    specs_from_manifest,
    write_mission_manifest,
)

AGENTS = [
    {"name": "alpha", "brief": "look at area A", "tools": ["Read", "Grep"], "owns": ["src/a.py"]},
    {"name": "beta", "brief": "look at area B"},
]


def build_manifest(tmp_path: Path, **overrides) -> dict:
    kwargs = dict(
        slug="demo",
        agents=[dict(a) for a in AGENTS],
        synthesis="rank the issues.",
        gate={"preset": "none"},
        model="claude-sonnet-4-5-20250929",
        max_turns=7,
        max_concurrency=2,
        target_repo=str(tmp_path / "repo"),
    )
    kwargs.update(overrides)
    return manifest_from_request(**kwargs)


def scripted() -> ScriptedWorker:
    def responder(request, api):
        if request.worker == "synthesizer":
            return "ranked"
        api.emit_finding("area", f"{request.worker} confirmed one thing")
        return f"{request.worker} done"

    return ScriptedWorker(responder)


def test_manifest_round_trips_through_the_run_dir(tmp_path: Path) -> None:
    manifest = build_manifest(tmp_path)
    run_dir = tmp_path / "runs" / "demo"

    path = write_mission_manifest(run_dir, manifest)

    assert path == run_dir / MANIFEST_FILENAME
    assert read_mission_manifest(run_dir) == manifest
    # Defaults are materialized, not implied, so a reader never has to guess.
    assert manifest["schema"] == 1 and manifest["kind"] == "mission"
    assert manifest["agents"][1] == {
        "name": "beta",
        "brief": "look at area B",
        "tools": ["Read", "Grep", "Glob"],
        "sdk": "claude",
        "owns": [],
    }
    assert manifest["created_at"].endswith("Z")
    assert not list(run_dir.glob("*.tmp"))


def test_read_manifest_is_none_when_missing_or_corrupt(tmp_path: Path) -> None:
    assert read_mission_manifest(tmp_path / "nope") is None
    (tmp_path / "bad").mkdir()
    (tmp_path / "bad" / MANIFEST_FILENAME).write_text("{not json", encoding="utf-8")
    assert read_mission_manifest(tmp_path / "bad") is None


def _identities(mission: Mission, tmp_path: Path, name: str) -> dict[str, dict]:
    result = mission.run(tmp_path / name, worker=scripted())
    return {
        e.payload["worker"]: e.payload["identity"]
        for e in result.graph.events
        if e.type == "agent.requested"
    }


def test_rebuilt_specs_hash_to_the_same_request_identity(tmp_path: Path) -> None:
    manifest = build_manifest(tmp_path)
    original_specs = [
        AgentSpec("alpha", "look at area A", tools=("Read", "Grep"), owns=("src/a.py",)),
        AgentSpec("beta", "look at area B"),
    ]
    assert specs_from_manifest(manifest) == original_specs

    repo = str(tmp_path / "repo")
    original = Mission(
        "demo",
        original_specs,
        synthesis="rank the issues.",
        model="claude-sonnet-4-5-20250929",
        max_turns=7,
        cwd=repo,
        claim_root=repo,
        max_concurrency=2,
        transcript_dir=str(tmp_path / "a" / "transcripts"),
    )
    rebuilt = mission_from_manifest(manifest, run_dir=tmp_path / "a")

    assert _identities(original, tmp_path, "orig.jsonl") == _identities(
        rebuilt, tmp_path, "rebuilt.jsonl"
    )


def test_replay_through_a_manifest_rebuilt_mission_is_byte_identical(
    tmp_path: Path,
) -> None:
    manifest = build_manifest(tmp_path)
    run_dir = tmp_path / "runs" / "demo"
    write_mission_manifest(run_dir, manifest)

    live = mission_from_manifest(read_mission_manifest(run_dir), run_dir=run_dir)
    live.run(run_dir / "run.jsonl", worker=scripted())

    replay_dir = tmp_path / "runs" / "demo-replay"
    write_mission_manifest(
        replay_dir,
        manifest_from_request(
            **{
                k: manifest[k]
                for k in (
                    "slug",
                    "agents",
                    "synthesis",
                    "gate",
                    "model",
                    "max_turns",
                    "max_concurrency",
                    "target_repo",
                )
            },
            kind="replay",
            parent_run_id="demo",
        ),
    )
    replayed = mission_from_manifest(read_mission_manifest(replay_dir), run_dir=replay_dir)
    # $0: a cache miss here raises ReplayCacheMiss instead of calling a worker.
    replayed.replay(run_dir / "run.jsonl", replay_dir / "run.jsonl")

    assert (replay_dir / "run.jsonl").read_bytes() == (run_dir / "run.jsonl").read_bytes()


def test_factory_runner_leaves_a_manifest_in_each_wave_dir(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    spec = FactorySpec(
        slug="fact",
        waves=[
            FactoryWave(slug="one", agents=[dict(AGENTS[0])], gate={}, max_turns=5),
            FactoryWave(slug="two", agents=[dict(AGENTS[1])], gate={}, max_turns=5),
        ],
    )
    runner = FactoryRunner(
        spec,
        repo,
        factory_run_id="fr-1",
        worker_factory=lambda run_dir, specs: scripted(),
    )
    state = runner.run()
    assert state.status == "completed", state.waves

    for wave in spec.waves:
        manifest = read_mission_manifest(runner.run_dir(wave))
        assert manifest is not None, f"no manifest for wave {wave.slug}"
        assert manifest["kind"] == "factory-wave"
        assert manifest["slug"] == f"fact-{wave.slug}"
        assert manifest["parent_run_id"] == "fr-1"
        assert manifest["target_repo"] == str(repo)
        assert [a["name"] for a in manifest["agents"]] == [a["name"] for a in wave.agents]
    # The manifest is written before the run, next to the log it describes.
    assert (runner.run_dir(spec.waves[0]) / "run.jsonl").exists()
    assert (
        json.loads((runner.run_dir(spec.waves[0]) / MANIFEST_FILENAME).read_text(encoding="utf-8"))[
            "schema"
        ]
        == 1
    )
