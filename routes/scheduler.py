"""Schedule discovery, one-shot launching, and the 60 s background ticker.

Why this lives apart from routes/config.py: the HTTP handler for
POST /api/schedules/<id>/run-now and the background scheduler must launch a
factory run *the same way*, so the launch machinery is factored out here and
imported by both. See routes/AGENTS.md for the wave-L contract.
"""

from __future__ import annotations

import asyncio
import os
from datetime import datetime, timezone
from pathlib import Path

from agentgraph import policy

SCHEDULER_TICK_SECONDS = 60


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def iso(moment: datetime | None) -> str | None:
    return moment.isoformat() if moment is not None else None


def collect_schedules(app_root: Path, repos: list[Path]) -> list[dict]:
    """Every schedule, ecosystem-wide and per project, with its target repo."""
    from app import repo_key

    ecosystem = policy.load_ecosystem(app_root)
    items: list[dict] = []
    for schedule in ecosystem.get("schedules", []) or []:
        items.append(
            {
                "schedule": schedule,
                "target_repo": schedule.get("target_repo") or str(app_root),
                "source": "ecosystem",
            }
        )
    for repo in repos:
        project = policy.load_project(repo)
        for schedule in project.get("schedules", []) or []:
            items.append(
                {
                    "schedule": schedule,
                    "target_repo": str(repo),
                    "source": repo_key(repo),
                }
            )
    return items


def find_schedule(app_root: Path, repos: list[Path], schedule_id: str) -> dict | None:
    for item in collect_schedules(app_root, repos):
        if item["schedule"].get("id") == schedule_id:
            return item
    return None


def schedule_next_due(schedule: dict, now: datetime | None = None) -> str | None:
    try:
        due = policy.next_due(schedule, now or utcnow(), schedule.get("last_run_at"))
    except Exception:
        return None
    return iso(due)


def record_schedule_run(
    app_root: Path,
    source: str,
    schedule_id: str,
    *,
    target_repo: Path,
    factory_run_id: str,
) -> None:
    """Stamp last_run_at/last_factory_run_id on the doc that owns the schedule."""
    stamp = iso(utcnow())
    if source == "ecosystem":
        doc = policy.load_ecosystem(app_root)
        save = lambda data: policy.save_ecosystem(app_root, data)
    else:
        doc = policy.load_project(target_repo)
        save = lambda data: policy.save_project(target_repo, data)
    changed = False
    for schedule in doc.get("schedules", []) or []:
        if schedule.get("id") == schedule_id:
            schedule["last_run_at"] = stamp
            schedule["last_factory_run_id"] = factory_run_id
            changed = True
    if changed:
        save(doc)


def launch_schedule_blocking(app, item: dict) -> tuple[dict | None, str | None, int]:
    """Start the schedule's factory run. Returns (payload, error, status)."""
    import time

    from app import (
        ECOSYSTEM_ROOT,
        effective_policy_rules,
        factory_spec_path,
        load_spec_or_errors,
        remember_repo,
        start_factory_thread,
        validate_target_repo,
    )

    schedule = item["schedule"]
    if not schedule.get("enabled", True):
        return None, "schedule is disabled", 409
    if schedule.get("kind", "factory") != "factory":
        return None, "only factory schedules can be run", 400

    target_repo, repo_error = validate_target_repo(item.get("target_repo"))
    if repo_error:
        return None, repo_error, 400

    spec_path, path_error = factory_spec_path(target_repo, schedule.get("factory_path"))
    if path_error:
        return None, path_error, 400
    spec, errors = load_spec_or_errors(spec_path)
    if errors:
        return None, "; ".join(errors), 400

    for entry in app.ctx.factory_runs.values():
        alive = entry["thread"] is not None and entry["thread"].is_alive()
        if alive and Path(entry["target_repo"]).resolve() == target_repo:
            return None, "A factory run is already running for this repo", 409

    remember_repo(target_repo)
    rules = effective_policy_rules(target_repo)
    factory_run_id = "%s-%d" % (spec.slug, int(time.time()))
    start_factory_thread(app, spec, target_repo, factory_run_id, 0, rules=rules)
    record_schedule_run(
        ECOSYSTEM_ROOT,
        item["source"],
        schedule.get("id"),
        target_repo=target_repo,
        factory_run_id=factory_run_id,
    )
    return (
        {
            "factory_run_id": factory_run_id,
            "target_repo": str(target_repo),
            "waves": [wave.slug for wave in spec.waves],
        },
        None,
        200,
    )


async def scheduler_tick(app) -> list[str]:
    """One pass: launch everything due, record what happened on app.ctx."""
    from app import ECOSYSTEM_ROOT, repo_key, repos_to_scan

    launched: list[str] = []
    try:
        repos = await asyncio.to_thread(repos_to_scan)
        ecosystem = await asyncio.to_thread(policy.load_ecosystem, ECOSYSTEM_ROOT)
        projects = {}
        for repo in repos:
            projects[str(repo)] = await asyncio.to_thread(policy.load_project, repo)
        due = policy.due_schedules(ecosystem, projects, utcnow())
        for schedule, target_repo in due:
            source = "ecosystem"
            resolved = Path(target_repo)
            for repo in repos:
                project = projects.get(str(repo)) or {}
                ids = {s.get("id") for s in (project.get("schedules") or [])}
                if schedule.get("id") in ids and resolved == repo:
                    source = repo_key(repo)
                    break
            item = {"schedule": schedule, "target_repo": str(target_repo), "source": source}
            payload, error, _ = await asyncio.to_thread(launch_schedule_blocking, app, item)
            if payload:
                launched.append(payload["factory_run_id"])
    except Exception as error:  # a scheduler crash must never take the app down
        print("Scheduler tick failed: %s" % error)
    app.ctx.scheduler_state = {"last_tick": iso(utcnow()), "launched": launched}
    return launched


def start_scheduler(app) -> None:
    """Register the 60 s ticker unless CONDUCTION_SCHEDULER=0."""
    app.ctx.scheduler_state = {"last_tick": None, "launched": []}
    if os.environ.get("CONDUCTION_SCHEDULER") == "0":
        return

    async def loop():
        while True:
            await scheduler_tick(app)
            await asyncio.sleep(SCHEDULER_TICK_SECONDS)

    app.add_task(loop())
