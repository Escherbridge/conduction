"""Mission manifests — a run directory that describes how to rebuild its own mission.

A manifest is the whole launch request as data, written next to `run.jsonl`
before the mission starts. With it, a run can be replayed for $0, narrated, or
deleted without the client re-sending anything.

The one hard constraint: `mission_from_manifest` must rebuild `AgentSpec`s that
hash to the *same request identity* as the originals (see
`AgentRequest.identity` in dispatcher.py) — prompt, tools, model, cwd,
max_turns. Anything off by a character makes `Mission.replay` raise
`ReplayCacheMiss`. See `agentgraph/AGENTS.md` §manifest.
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Optional

from agentgraph.mission import READ_TOOLS, AgentSpec, Mission

MANIFEST_FILENAME = "mission.json"
SCHEMA_VERSION = 1
DEFAULT_SDK = "claude"
# Run artifacts never dirty the host repo, but the factory spec is source and
# must stay versioned -- hence the negations.
AGENTGRAPH_GITIGNORE = "*\n!.gitignore\n!factory.json\n"


def ensure_agentgraph_gitignore(target_repo: Path) -> Path:
    """Create `<repo>/.agentgraph/.gitignore`, upgrading a legacy bare `*` in place."""
    path = Path(target_repo) / ".agentgraph" / ".gitignore"
    path.parent.mkdir(parents=True, exist_ok=True)
    if not path.exists() or path.read_text(encoding="utf-8").strip() == "*":
        path.write_text(AGENTGRAPH_GITIGNORE, encoding="utf-8")
    return path


def write_mission_manifest(run_dir: Path, manifest: dict) -> Path:
    """Write the manifest into `run_dir` atomically (temp file + os.replace)."""
    run_dir = Path(run_dir)
    run_dir.mkdir(parents=True, exist_ok=True)
    path = run_dir / MANIFEST_FILENAME
    temp = path.with_suffix(".json.tmp")
    temp.write_text(json.dumps(manifest, indent=2, sort_keys=False), encoding="utf-8")
    os.replace(temp, path)
    return path


def read_mission_manifest(run_dir: Path) -> Optional[dict]:
    """Read the manifest from `run_dir`, or None when absent/unreadable."""
    path = Path(run_dir) / MANIFEST_FILENAME
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, NotADirectoryError, OSError, json.JSONDecodeError):
        return None
    return data if isinstance(data, dict) else None


def _normalize_agent(agent: dict) -> dict:
    return {
        "name": agent["name"],
        "brief": agent["brief"],
        "tools": list(agent.get("tools") or READ_TOOLS),
        "sdk": agent.get("sdk") or DEFAULT_SDK,
        "owns": list(agent.get("owns") or ()),
    }


def manifest_from_request(
    *,
    slug: str,
    agents: list[dict],
    synthesis,
    gate: Optional[dict],
    model,
    max_turns,
    max_concurrency,
    target_repo: str,
    kind: str = "mission",
    parent_run_id: Optional[str] = None,
) -> dict:
    """Build the manifest dict for a launch/resume/replay/factory-wave run."""
    return {
        "schema": SCHEMA_VERSION,
        "kind": kind,
        "slug": slug,
        "agents": [_normalize_agent(a) for a in agents],
        "synthesis": synthesis,
        "gate": gate,
        "model": model,
        "max_turns": max_turns,
        "max_concurrency": max_concurrency,
        "target_repo": str(target_repo),
        "parent_run_id": parent_run_id,
        "created_at": datetime.now(timezone.utc)
        .replace(microsecond=0)
        .isoformat()
        .replace("+00:00", "Z"),
    }


def specs_from_manifest(manifest: dict) -> list[AgentSpec]:
    """Rebuild the AgentSpec list — identity-critical, so mirror factory.py."""
    specs: list[AgentSpec] = []
    for agent in manifest.get("agents") or []:
        meta: dict[str, Any] = {}
        sdk = agent.get("sdk")
        # Only a non-default sdk rides in meta, matching FactoryRunner._agent_spec.
        if sdk and sdk != DEFAULT_SDK:
            meta["sdk"] = sdk
        specs.append(
            AgentSpec(
                name=agent["name"],
                brief=agent["brief"],
                tools=tuple(agent.get("tools") or READ_TOOLS),
                owns=tuple(agent.get("owns") or ()),
                meta=meta,
            )
        )
    return specs


def mission_from_manifest(
    manifest: dict,
    *,
    run_dir: Path,
    gate_callable: Optional[Callable[[Any], Any]] = None,
) -> Mission:
    """Rebuild the Mission a manifest describes, wired to `run_dir`."""
    run_dir = Path(run_dir)
    target_repo = manifest.get("target_repo") or None
    return Mission(
        manifest["slug"],
        specs_from_manifest(manifest),
        synthesis=manifest.get("synthesis"),
        model=manifest["model"],
        max_turns=manifest["max_turns"],
        cwd=target_repo,
        claim_root=target_repo,
        max_concurrency=manifest.get("max_concurrency", 4),
        transcript_dir=str(run_dir / "transcripts"),
        gate=gate_callable,
    )


__all__ = [
    "MANIFEST_FILENAME",
    "SCHEMA_VERSION",
    "manifest_from_request",
    "mission_from_manifest",
    "read_mission_manifest",
    "specs_from_manifest",
    "write_mission_manifest",
]
