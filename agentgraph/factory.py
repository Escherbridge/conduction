"""Factories: an ordered list of waves, each executed as one gated Mission.

A FACTORY is versioned as data in the target repo at
``<target_repo>/.agentgraph/factory.json``. A FACTORY RUN executes its waves
sequentially, halting on the first failed gate and resumable from that wave.
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Optional

from agentgraph import gates
from agentgraph.dispatcher import Worker
from agentgraph.manifest import ensure_agentgraph_gitignore, manifest_from_request, write_mission_manifest
from agentgraph.mission import READ_TOOLS, AgentSpec, Mission

SLUG_PATTERN = re.compile(r"^[A-Za-z0-9._-]{1,64}$")


class FactorySpecError(Exception):
    """Raised when a factory spec file is unreadable or invalid."""


@dataclass
class FactoryWave:
    """One wave: the agents that run together plus the gate that judges them."""

    slug: str
    agents: list[dict]
    gate: dict
    synthesis: Optional[str] = None
    max_turns: int = 30
    max_concurrency: int = 4


@dataclass
class FactorySpec:
    """An ordered list of waves, identified by slug."""

    slug: str
    waves: list[FactoryWave]
    description: str = ""


@dataclass
class FactoryRunState:
    """The source of truth for one factory run, persisted as JSON."""

    factory_run_id: str
    factory_slug: str
    target_repo: str
    started_at: str
    status: str  # running | completed | failed | interrupted
    current_wave: int = 0
    waves: list[dict] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "factory_run_id": self.factory_run_id,
            "factory_slug": self.factory_slug,
            "target_repo": self.target_repo,
            "started_at": self.started_at,
            "status": self.status,
            "current_wave": self.current_wave,
            "waves": self.waves,
        }


def validate_factory_spec(data: dict) -> list[str]:
    """Validate a factory spec dict, returning every problem found."""
    errors: list[str] = []
    if not isinstance(data, dict):
        return ["factory spec must be a dict"]

    slug = data.get("slug")
    if not isinstance(slug, str) or not SLUG_PATTERN.match(slug):
        errors.append("slug must match ^[A-Za-z0-9._-]{1,64}$")

    waves = data.get("waves")
    if not isinstance(waves, list) or not waves:
        errors.append("waves must be a non-empty list")
        return errors

    seen: set[str] = set()
    for index, wave in enumerate(waves):
        where = f"wave[{index}]"
        if not isinstance(wave, dict):
            errors.append(f"{where} must be a dict")
            continue
        wave_slug = wave.get("slug")
        if not isinstance(wave_slug, str) or not SLUG_PATTERN.match(wave_slug):
            errors.append(f"{where}.slug must match ^[A-Za-z0-9._-]{{1,64}}$")
        elif wave_slug in seen:
            errors.append(f"{where}.slug: duplicate wave slug {wave_slug!r}")
        else:
            seen.add(wave_slug)

        agents = wave.get("agents")
        if not isinstance(agents, list) or not agents:
            errors.append(f"{where}.agents must be a non-empty list")
        else:
            for j, agent in enumerate(agents):
                if not isinstance(agent, dict):
                    errors.append(f"{where}.agents[{j}] must be a dict")
                    continue
                if not isinstance(agent.get("name"), str) or not agent.get("name"):
                    errors.append(f"{where}.agents[{j}].name is required")
                if not isinstance(agent.get("brief"), str) or not agent.get("brief"):
                    errors.append(f"{where}.agents[{j}].brief is required")
                for key in agent:
                    if key not in ("name", "brief", "tools", "sdk", "owns"):
                        errors.append(f"{where}.agents[{j}]: unknown key {key!r}")

        gate = wave.get("gate", {})
        if not isinstance(gate, dict):
            errors.append(f"{where}.gate must be a dict")
        else:
            errors.extend(f"{where}.gate: {e}" for e in gates.validate_gate_spec(gate))

        for key in ("max_turns", "max_concurrency"):
            if key in wave and not isinstance(wave[key], int):
                errors.append(f"{where}.{key} must be an integer")
        if wave.get("synthesis") is not None and not isinstance(
            wave.get("synthesis"), str
        ):
            errors.append(f"{where}.synthesis must be a string or null")

    return errors


def load_factory_spec(path) -> FactorySpec:
    """Load and validate a factory spec from JSON. Raises FactorySpecError."""
    path = Path(path)
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise FactorySpecError(f"factory spec not found: {path}") from exc
    except json.JSONDecodeError as exc:
        raise FactorySpecError(f"factory spec is not valid JSON: {exc}") from exc

    errors = validate_factory_spec(data)
    if errors:
        raise FactorySpecError("; ".join(errors))

    waves = [
        FactoryWave(
            slug=wave["slug"],
            agents=list(wave["agents"]),
            gate=dict(wave.get("gate") or {}),
            synthesis=wave.get("synthesis"),
            max_turns=int(wave.get("max_turns", 30)),
            max_concurrency=int(wave.get("max_concurrency", 4)),
        )
        for wave in data["waves"]
    ]
    return FactorySpec(
        slug=data["slug"], waves=waves, description=data.get("description", "")
    )


def _write_json_atomic(path: Path, payload: dict[str, Any]) -> None:
    """Write JSON via temp file + os.replace — readers poll this file."""
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(".json.tmp")
    temp.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    os.replace(temp, path)


class FactoryRunner:
    """Runs a factory's waves in order, one gated Mission per wave."""

    def __init__(
        self,
        spec: FactorySpec,
        target_repo: Path,
        *,
        factory_run_id: str,
        worker_factory: Optional[
            Callable[[Path, list[AgentSpec]], Optional[Worker]]
        ] = None,
        model: str = "claude-sonnet-4-5-20250929",
        stop_when: Optional[Callable[[], bool]] = None,
    ) -> None:
        self.spec = spec
        self.target_repo = Path(target_repo)
        self.factory_run_id = factory_run_id
        self.worker_factory = worker_factory
        self.model = model
        self.stop_when = stop_when

    # ---- paths ----

    @property
    def state_path(self) -> Path:
        return (
            self.target_repo
            / ".agentgraph"
            / "factory-runs"
            / self.factory_run_id
            / "state.json"
        )

    def run_dir(self, wave: FactoryWave) -> Path:
        return self.target_repo / ".agentgraph" / "runs" / f"{self.spec.slug}-{wave.slug}"

    # ---- state ----

    def _load_prior_waves(self) -> dict[str, dict]:
        try:
            data = json.loads(self.state_path.read_text(encoding="utf-8"))
        except (FileNotFoundError, json.JSONDecodeError, OSError):
            return {}
        return {w["slug"]: w for w in data.get("waves", []) if "slug" in w}

    def _persist(self, state: FactoryRunState) -> None:
        _write_json_atomic(self.state_path, state.to_dict())

    # ---- execution ----

    def run(self, *, start_wave: int = 0) -> FactoryRunState:
        ensure_agentgraph_gitignore(self.target_repo)
        prior = self._load_prior_waves()
        state = FactoryRunState(
            factory_run_id=self.factory_run_id,
            factory_slug=self.spec.slug,
            target_repo=str(self.target_repo),
            started_at="",
            status="running",
            current_wave=start_wave,
            waves=[{"slug": wave.slug, "status": "pending"} for wave in self.spec.waves],
        )
        self._persist(state)

        for index, wave in enumerate(self.spec.waves):
            entry = state.waves[index]
            run_dir = self.run_dir(wave)
            already = prior.get(wave.slug, {})
            passed_before = (run_dir / "run.jsonl").exists() and already.get(
                "status"
            ) in ("passed", "skipped")
            if index < start_wave or passed_before:
                entry.update(already)
                entry["slug"] = wave.slug
                entry["status"] = "skipped"
                self._persist(state)
                continue

            state.current_wave = index
            entry["status"] = "running"
            self._persist(state)

            result = self._run_wave(wave, run_dir)
            entry["mission_run_id"] = f"{self.spec.slug}-{wave.slug}"
            entry["run_id"] = f"{self.spec.slug}-{wave.slug}"
            entry["gate_passed"] = None if result.gate is None else bool(result.gate.passed)
            entry["agents_failed"] = sum(
                1 for report in result.reports.values() if not report.ok
            )
            entry["status"] = "failed" if result.failed else "passed"
            self._persist(state)

            if result.failed:
                state.status = "failed"
                self._persist(state)
                return state

            if self.stop_when is not None and self.stop_when():
                state.status = "interrupted"
                self._persist(state)
                return state

        state.status = "completed"
        state.current_wave = len(self.spec.waves)
        self._persist(state)
        return state

    def _run_wave(self, wave: FactoryWave, run_dir: Path):
        run_dir.mkdir(parents=True, exist_ok=True)
        specs = [self._agent_spec(agent) for agent in wave.agents]
        owns = {spec.name: tuple(spec.owns) for spec in specs}
        gate = gates.gate_from_spec(wave.gate, cwd=str(self.target_repo), owns=owns)

        # The wave's run dir describes itself: replay/story/delete need no client.
        write_mission_manifest(
            run_dir,
            manifest_from_request(
                slug=f"{self.spec.slug}-{wave.slug}",
                agents=list(wave.agents),
                synthesis=wave.synthesis,
                gate=dict(wave.gate or {}),
                model=self.model,
                max_turns=wave.max_turns,
                max_concurrency=wave.max_concurrency,
                target_repo=str(self.target_repo),
                kind="factory-wave",
                parent_run_id=self.factory_run_id,
            ),
        )

        if self.worker_factory is not None:
            worker = self.worker_factory(run_dir, specs)
        else:
            worker = self._resolve_sdk_worker(specs)

        mission = Mission(
            f"{self.spec.slug}-{wave.slug}",
            specs,
            synthesis=wave.synthesis,
            model=self.model,
            max_turns=wave.max_turns,
            cwd=str(self.target_repo),
            claim_root=str(self.target_repo),
            max_concurrency=wave.max_concurrency,
            transcript_dir=str(run_dir / "transcripts"),
            gate=gate,
        )
        return mission.run(run_dir / "run.jsonl", worker=worker, stop_when=self.stop_when)

    @staticmethod
    def _resolve_sdk_worker(specs: list[AgentSpec]) -> Optional[Worker]:
        """Route per-agent `sdk` choices, or None to let Mission use its default."""
        agent_sdks = {
            spec.name: spec.meta["sdk"] for spec in specs if spec.meta.get("sdk")
        }
        if not agent_sdks:
            return None
        from agentgraph import sdk_workers

        return sdk_workers.resolve_workers(agent_sdks)

    def _agent_spec(self, agent: dict) -> AgentSpec:
        meta: dict[str, Any] = {}
        if agent.get("sdk"):
            meta["sdk"] = agent["sdk"]
        return AgentSpec(
            name=agent["name"],
            brief=agent["brief"],
            tools=tuple(agent.get("tools") or READ_TOOLS),
            owns=tuple(agent.get("owns") or ()),
            meta=meta,
        )


__all__ = [
    "FactoryRunState",
    "FactoryRunner",
    "FactorySpec",
    "FactorySpecError",
    "FactoryWave",
    "load_factory_spec",
    "validate_factory_spec",
]
