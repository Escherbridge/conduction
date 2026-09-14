"""Rules/goals/schedules API: ecosystem config, per-project config, schedules.

Shared state is reached only through request.app.ctx and lazy `from app import`
inside handlers -- app.py imports this module, so a module-level import of app
would be circular.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

from sanic import Blueprint
from sanic.response import json as sanic_json

from agentgraph import policy

from routes.scheduler import (
    collect_schedules,
    find_schedule,
    launch_schedule_blocking,
    schedule_next_due,
)

bp = Blueprint("config")


def goal_counts(doc: dict) -> dict:
    counts = {"open": 0, "done": 0, "blocked": 0}
    for goal in doc.get("goals", []) or []:
        status = goal.get("status", "open")
        if status in counts:
            counts[status] += 1
    return counts


def project_item_blocking(app, repo: Path) -> dict:
    from app import repo_key

    project = policy.load_project(repo)
    connection = app.ctx.mirror._ensure_open()
    rows = connection.execute(
        "SELECT COUNT(*), MAX(started_at) FROM runs WHERE LOWER(target_repo) = ?",
        (str(repo).lower(),),
    ).fetchone()
    return {
        "repo_key": repo_key(repo),
        "target_repo": str(repo),
        "name": project.get("name") or repo.name,
        "rules_count": len(project.get("rules", []) or []),
        "goals": goal_counts(project),
        "schedules_count": len(project.get("schedules", []) or []),
        "runs_count": rows[0] if rows else 0,
        "last_run_at": rows[1] if rows else None,
    }


async def project_item(app, repo: Path) -> dict:
    async with app.ctx.mirror_lock:
        return await asyncio.to_thread(project_item_blocking, app, repo)


def resolve_repo_key(key: str) -> Path | None:
    from app import repo_key, repos_to_scan

    for repo in repos_to_scan():
        if repo_key(repo) == key:
            return repo
    return None


@bp.get("/api/ecosystem")
async def get_ecosystem(request):
    """The ecosystem-wide rules/goals/schedules document."""
    from app import ECOSYSTEM_ROOT

    return sanic_json(await asyncio.to_thread(policy.load_ecosystem, ECOSYSTEM_ROOT))


@bp.put("/api/ecosystem")
async def put_ecosystem(request):
    """Replace the ecosystem document (validated; 400 lists every problem)."""
    from app import ECOSYSTEM_ROOT

    data = request.json
    if not isinstance(data, dict):
        return sanic_json({"errors": ["body must be a JSON object"]}, status=400)
    data.setdefault("schema", 1)
    errors = policy.validate_ecosystem(data)
    if errors:
        return sanic_json({"errors": list(errors)}, status=400)
    await asyncio.to_thread(policy.save_ecosystem, ECOSYSTEM_ROOT, data)
    return sanic_json(await asyncio.to_thread(policy.load_ecosystem, ECOSYSTEM_ROOT))


@bp.get("/api/projects")
async def list_projects(request):
    """Every registered target repo with its rule/goal/schedule/run counts."""
    from app import repos_to_scan

    repos = await asyncio.to_thread(repos_to_scan)
    return sanic_json([await project_item(request.app, repo) for repo in repos])


@bp.post("/api/projects")
async def register_project(request):
    """Register a target repo and create its .agentgraph/project.json."""
    from app import remember_repo, validate_target_repo

    body = request.json or {}
    target_repo, repo_error = validate_target_repo(body.get("target_repo"))
    if repo_error:
        return sanic_json({"error": repo_error}, status=400)

    def ensure_project() -> None:
        remember_repo(target_repo)
        project = dict(policy.load_project(target_repo))
        project.setdefault("schema", 1)
        project["name"] = body.get("name") or project.get("name") or target_repo.name
        policy.save_project(target_repo, project)

    await asyncio.to_thread(ensure_project)
    return sanic_json(await project_item(request.app, target_repo))


@bp.get("/api/projects/<key:str>")
async def get_project(request, key: str):
    """One project's document, plus the repo it belongs to."""
    repo = await asyncio.to_thread(resolve_repo_key, key)
    if repo is None:
        return sanic_json({"error": "Unknown project: %s" % key}, status=404)
    project = dict(await asyncio.to_thread(policy.load_project, repo))
    project["target_repo"] = str(repo)
    project["repo_key"] = key
    return sanic_json(project)


@bp.put("/api/projects/<key:str>")
async def put_project(request, key: str):
    """Replace one project's document (validated; 400 lists every problem)."""
    repo = await asyncio.to_thread(resolve_repo_key, key)
    if repo is None:
        return sanic_json({"error": "Unknown project: %s" % key}, status=404)
    data = request.json
    if not isinstance(data, dict):
        return sanic_json({"errors": ["body must be a JSON object"]}, status=400)
    data = dict(data)
    data.pop("target_repo", None)
    data.pop("repo_key", None)
    data.setdefault("schema", 1)
    data.setdefault("name", repo.name)
    errors = policy.validate_project(data)
    if errors:
        return sanic_json({"errors": list(errors)}, status=400)
    await asyncio.to_thread(policy.save_project, repo, data)
    saved = dict(await asyncio.to_thread(policy.load_project, repo))
    saved["target_repo"] = str(repo)
    saved["repo_key"] = key
    return sanic_json(saved)


@bp.get("/api/schedules")
async def list_schedules(request):
    """Every schedule anywhere, annotated with its next due time."""
    from app import ECOSYSTEM_ROOT, repos_to_scan

    def collect() -> list[dict]:
        items = []
        for item in collect_schedules(ECOSYSTEM_ROOT, repos_to_scan()):
            schedule = dict(item["schedule"])
            schedule["target_repo"] = item["target_repo"]
            schedule["source"] = item["source"]
            schedule["next_due"] = schedule_next_due(item["schedule"])
            items.append(schedule)
        return items

    return sanic_json(await asyncio.to_thread(collect))


@bp.post("/api/schedules/<schedule_id:str>/run-now")
async def run_schedule_now(request, schedule_id: str):
    """Start this schedule's factory run immediately (same machinery as the ticker)."""
    from app import ECOSYSTEM_ROOT, repos_to_scan

    item = await asyncio.to_thread(
        lambda: find_schedule(ECOSYSTEM_ROOT, repos_to_scan(), schedule_id)
    )
    if item is None:
        return sanic_json({"error": "Unknown schedule: %s" % schedule_id}, status=404)
    payload, error, status = await asyncio.to_thread(
        launch_schedule_blocking, request.app, item
    )
    if error:
        return sanic_json({"error": error}, status=status)
    return sanic_json(payload)


@bp.get("/api/rules/effective")
async def effective_rules(request):
    """The merged ecosystem+project rules for a repo, with a rendered preview."""
    from app import ECOSYSTEM_ROOT, validate_target_repo

    target_repo, repo_error = validate_target_repo(request.args.get("target_repo"))
    if repo_error:
        return sanic_json({"error": repo_error}, status=400)

    def compute() -> dict:
        ecosystem = policy.load_ecosystem(ECOSYSTEM_ROOT)
        project = policy.load_project(target_repo)
        rules = list(policy.effective_rules(ecosystem, project))
        return {
            "target_repo": str(target_repo),
            "rules": rules,
            "writer_block": policy.rules_block(rules, writer=True),
            "reader_block": policy.rules_block(rules, writer=False),
        }

    return sanic_json(await asyncio.to_thread(compute))
