"""Factory runtime tests: offline, ScriptedWorker, temp target repo.

Every mission runs through `worker_factory` returning a `ScriptedWorker`, so
nothing here touches the network or an API key. Gates are `command` gates
running the current interpreter, which makes pass/fail deterministic.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

from agentgraph import ScriptedWorker
from agentgraph.factory import (
    FactoryRunner,
    FactorySpec,
    FactorySpecError,
    FactoryWave,
    load_factory_spec,
    validate_factory_spec,
)
from agentgraph.log import read_envelopes

PASSING_GATE = {"command": {"argv": [sys.executable, "-c", "raise SystemExit(0)"]}}
FAILING_GATE = {"command": {"argv": [sys.executable, "-c", "raise SystemExit(1)"]}}


def scripted_factory(*, record: list[str] | None = None):
    """A worker_factory whose workers answer instantly and log agent names."""

    def make(run_dir: Path, specs):
        def responder(request, api):
            if record is not None:
                record.append(request.worker)
            return f"{request.worker} ok"

        return ScriptedWorker(responder)

    return make


def two_wave_spec(second_gate: dict = PASSING_GATE) -> FactorySpec:
    return FactorySpec(
        slug="demo",
        waves=[
            FactoryWave(
                slug="one",
                agents=[{"name": "alpha", "brief": "do a", "owns": ["src/"]}],
                gate=dict(PASSING_GATE),
                synthesis=None,
                max_turns=5,
            ),
            FactoryWave(
                slug="two",
                agents=[{"name": "beta", "brief": "do b"}],
                gate=dict(second_gate),
                synthesis=None,
                max_turns=5,
            ),
        ],
    )


# ---- validation ----------------------------------------------------------


def test_validate_catches_duplicate_wave_slugs() -> None:
    errors = validate_factory_spec(
        {
            "slug": "demo",
            "waves": [
                {"slug": "one", "agents": [{"name": "a", "brief": "b"}], "gate": {}},
                {"slug": "one", "agents": [{"name": "c", "brief": "d"}], "gate": {}},
            ],
        }
    )
    assert any("duplicate wave slug" in e for e in errors), errors


def test_validate_catches_empty_agents_and_bad_slug() -> None:
    errors = validate_factory_spec(
        {
            "slug": "demo",
            "waves": [
                {"slug": "one", "agents": [], "gate": {}},
                {"slug": "not a slug!", "agents": [{"name": "a", "brief": "b"}], "gate": {}},
            ],
        }
    )
    assert any("agents must be a non-empty list" in e for e in errors), errors
    assert any("wave[1].slug" in e for e in errors), errors


def test_validate_catches_bad_gate_keys() -> None:
    errors = validate_factory_spec(
        {
            "slug": "demo",
            "waves": [
                {
                    "slug": "one",
                    "agents": [{"name": "a", "brief": "b"}],
                    "gate": {"nonsense": True},
                }
            ],
        }
    )
    assert any("unknown gate key" in e for e in errors), errors


def test_load_factory_spec_raises_listing_every_problem(tmp_path: Path) -> None:
    path = tmp_path / "factory.json"
    path.write_text(
        json.dumps({"slug": "demo", "waves": [{"slug": "one", "agents": [], "gate": {}}]}),
        encoding="utf-8",
    )
    with pytest.raises(FactorySpecError) as excinfo:
        load_factory_spec(path)
    assert "agents must be a non-empty list" in str(excinfo.value)


def test_load_factory_spec_round_trips(tmp_path: Path) -> None:
    path = tmp_path / "factory.json"
    path.write_text(
        json.dumps(
            {
                "slug": "demo",
                "description": "two waves",
                "waves": [
                    {
                        "slug": "one",
                        "agents": [{"name": "a", "brief": "b", "owns": ["src/"]}],
                        "gate": {},
                        "max_turns": 7,
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    spec = load_factory_spec(path)
    assert spec.slug == "demo"
    assert [wave.slug for wave in spec.waves] == ["one"]
    assert spec.waves[0].max_turns == 7
    assert spec.waves[0].agents[0]["owns"] == ["src/"]


# ---- running -------------------------------------------------------------


def read_state(target_repo: Path, factory_run_id: str) -> dict:
    return json.loads(
        (
            target_repo
            / ".agentgraph"
            / "factory-runs"
            / factory_run_id
            / "state.json"
        ).read_text(encoding="utf-8")
    )


def last_event_type(run_jsonl: Path) -> str:
    envelopes = list(read_envelopes(run_jsonl))
    return envelopes[-1]["event"]["type"]


def test_two_passing_waves_complete_and_write_state(tmp_path: Path) -> None:
    seen: list[str] = []
    runner = FactoryRunner(
        two_wave_spec(),
        tmp_path,
        factory_run_id="demo-1",
        worker_factory=scripted_factory(record=seen),
    )
    state = runner.run()

    assert state.status == "completed"
    assert [wave["status"] for wave in state.waves] == ["passed", "passed"]

    on_disk = read_state(tmp_path, "demo-1")
    assert on_disk["status"] == "completed"
    assert [wave["status"] for wave in on_disk["waves"]] == ["passed", "passed"]
    assert on_disk["factory_slug"] == "demo"

    assert seen == ["alpha", "beta"]
    for wave_slug in ("one", "two"):
        run_jsonl = tmp_path / ".agentgraph" / "runs" / f"demo-{wave_slug}" / "run.jsonl"
        assert run_jsonl.exists()
        assert last_event_type(run_jsonl) == "mission.completed"


def test_failing_gate_on_first_wave_halts_the_factory(tmp_path: Path) -> None:
    spec = two_wave_spec()
    spec.waves[0].gate = dict(FAILING_GATE)
    seen: list[str] = []
    runner = FactoryRunner(
        spec, tmp_path, factory_run_id="demo-2", worker_factory=scripted_factory(record=seen)
    )
    state = runner.run()

    assert state.status == "failed"
    assert state.waves[0]["status"] == "failed"
    assert state.waves[0]["gate_passed"] is False
    assert state.waves[1]["status"] == "pending"
    assert seen == ["alpha"], "wave two must never dispatch after a failed gate"

    on_disk = read_state(tmp_path, "demo-2")
    assert on_disk["status"] == "failed"
    assert on_disk["waves"][1]["status"] == "pending"
    assert not (tmp_path / ".agentgraph" / "runs" / "demo-two").exists()


def test_resume_skips_the_already_passed_wave(tmp_path: Path) -> None:
    spec = two_wave_spec()
    spec.waves[1].gate = dict(FAILING_GATE)
    first = FactoryRunner(
        spec, tmp_path, factory_run_id="demo-3", worker_factory=scripted_factory()
    )
    failed_state = first.run()
    assert failed_state.status == "failed"
    assert failed_state.waves[0]["status"] == "passed"

    spec.waves[1].gate = dict(PASSING_GATE)
    seen: list[str] = []
    second = FactoryRunner(
        spec, tmp_path, factory_run_id="demo-3", worker_factory=scripted_factory(record=seen)
    )
    state = second.run(start_wave=1)

    assert state.waves[0]["status"] == "skipped"
    assert state.waves[1]["status"] == "passed"
    assert state.status == "completed"
    assert seen == ["beta"], "the skipped wave must not re-dispatch its agents"
    assert read_state(tmp_path, "demo-3")["status"] == "completed"


def test_stop_when_after_first_wave_interrupts(tmp_path: Path) -> None:
    stop = {"now": False}
    seen: list[str] = []

    def make(run_dir: Path, specs):
        def responder(request, api):
            seen.append(request.worker)
            return f"{request.worker} ok"

        return ScriptedWorker(responder)

    runner = FactoryRunner(
        two_wave_spec(),
        tmp_path,
        factory_run_id="demo-4",
        worker_factory=make,
        stop_when=lambda: stop["now"],
    )
    # Flip the switch once the first wave's only agent has answered.
    original = runner._run_wave

    def run_wave(wave, run_dir):
        result = original(wave, run_dir)
        stop["now"] = True
        return result

    runner._run_wave = run_wave  # type: ignore[method-assign]
    state = runner.run()

    assert state.status == "interrupted"
    assert state.waves[0]["status"] == "passed"
    assert state.waves[1]["status"] == "pending"
    assert seen == ["alpha"]
    assert read_state(tmp_path, "demo-4")["status"] == "interrupted"
