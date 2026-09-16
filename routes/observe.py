"""Fleet-wide observability API (blueprint "observe").

Endpoints: /api/observe/summary, /agents, /feed (SSE), /timeline.
Shared state is reached ONLY through request.app.ctx and lazy `from app import ...`
inside handlers -- app.py imports this module, so a module-level import would cycle.
See routes/AGENTS.md for the Wave L interface contract.
"""

from __future__ import annotations

import asyncio
import json as stdlib_json
import time
from datetime import UTC, datetime

from sanic import Blueprint
from sanic.response import json as sanic_json

bp = Blueprint("observe", url_prefix="/api/observe")

ACTIVITY_EVENT_TYPES = (
    "finding.recorded",
    "claim.rejected",
    "claim.violated",
    "command.violated",
    "mission.completed",
)

FEED_POLL_INTERVAL_SECONDS = 2.0
FEED_MAX_DURATION_SECONDS = 2 * 60 * 60
TIMELINE_DEFAULT_LIMIT = 200
TIMELINE_MAX_LIMIT = 1000


def _app_module():
    """The already-imported app module.

    app.py is run as a script, so it lives in sys.modules as "__main__"; a plain
    `import app` would execute the file a SECOND time and blow up with
    "Sanic app name ... already in use". Prefer the live module object.
    """
    import sys

    main = sys.modules.get("__main__")
    if main is not None and hasattr(main, "fetch_rows") and hasattr(main, "mirror_all_runs"):
        return main
    import app as app_module

    return app_module


def _utc_today() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%d")


def _payload(raw: str | None) -> dict:
    if not raw:
        return {}
    try:
        value = stdlib_json.loads(raw)
    except ValueError:
        return {}
    return value if isinstance(value, dict) else {}


def _activity_summary(event_type: str, payload: dict) -> str:
    if event_type == "finding.recorded":
        topic = payload.get("topic") or ""
        summary = payload.get("summary") or ""
        return f"{topic}: {summary}" if topic else summary
    if event_type in ("claim.rejected", "claim.violated"):
        paths = payload.get("paths") or []
        return f"{event_type} {', '.join(str(path) for path in paths)}".strip()
    if event_type == "command.violated":
        return f"blocked {payload.get('tool_name') or ''}: {payload.get('command') or ''}".strip()
    if event_type == "mission.completed":
        status = payload.get("status") or "completed"
        gate = payload.get("gate_passed")
        gate_note = "" if gate is None else f" (gate {'passed' if gate else 'failed'})"
        return f"mission {status}{gate_note}"
    return event_type


def _activity_row(row) -> dict:
    payload = _payload(row[4])
    return {
        "run_id": row[0],
        "slug": row[6],
        "target_repo": row[7],
        "seq": row[1],
        "type": row[2],
        "worker": payload.get("worker") or row[3],
        "summary": _activity_summary(row[2], payload),
        "ts": row[5],
    }


_ACTIVITY_QUERY = """
    SELECT e.run_id, e.seq, e.type, e.actor, e.payload_json, e.ts,
           r.slug, r.target_repo
    FROM events e
    LEFT JOIN runs r ON r.run_id = e.run_id
    WHERE e.type IN ({placeholders})
    ORDER BY e.ts DESC, e.seq DESC
    LIMIT ?
""".format(placeholders=", ".join("?" for _ in ACTIVITY_EVENT_TYPES))


async def _recent_activity(app, limit: int) -> list[dict]:
    fetch_rows = _app_module().fetch_rows

    rows = await fetch_rows(app, _ACTIVITY_QUERY, (*ACTIVITY_EVENT_TYPES, limit))
    return [_activity_row(row) for row in rows]


@bp.get("/summary")
async def observe_summary(request):
    """Fleet KPIs for the dashboard top bar and dashboard cards."""
    _app = _app_module()
    fetch_rows, mirror_all_runs = _app.fetch_rows, _app.mirror_all_runs
    effective_status, repos_to_scan = _app.effective_status, _app.repos_to_scan

    await mirror_all_runs(request.app)

    run_rows = await fetch_rows(
        request.app,
        "SELECT run_id, status, gate_passed, started_at FROM runs",
    )
    runs_by_status: dict[str, int] = {}
    active_runs = 0
    gated_recent: list[int] = []
    today_run_ids: set[str] = set()
    today = _utc_today()

    # Newest first so "last 20" means the twenty most recent gated runs.
    ordered = sorted(run_rows, key=lambda row: (row[3] or "", row[0]), reverse=True)
    for row in ordered:
        status = effective_status(request.app, row[0], row[1]) or "unknown"
        runs_by_status[status] = runs_by_status.get(status, 0) + 1
        if status == "running":
            active_runs += 1
        if row[2] is not None and len(gated_recent) < 20:
            gated_recent.append(1 if row[2] else 0)
        if (row[3] or "").startswith(today):
            today_run_ids.add(row[0])

    cost_rows = await fetch_rows(
        request.app, "SELECT run_id, COALESCE(SUM(cost_usd), 0) FROM agents GROUP BY run_id"
    )
    cost_total = 0.0
    cost_today = 0.0
    for run_id, cost in cost_rows:
        cost_total += cost or 0.0
        if run_id in today_run_ids:
            cost_today += cost or 0.0

    live_agents = await _live_agents(request.app)

    violation_rows = await fetch_rows(
        request.app,
        """
        SELECT COUNT(*) FROM events
        WHERE type IN ('command.violated', 'claim.violated')
          AND ts >= ?
        """,
        (datetime.fromtimestamp(time.time() - 86400, tz=UTC).isoformat().replace("+00:00", "Z"),),
    )

    gate_pass_rate = None if not gated_recent else round(sum(gated_recent) / len(gated_recent), 4)

    return sanic_json(
        {
            "active_runs": active_runs,
            "live_agents": len(live_agents),
            "cost_today_usd": round(cost_today, 6),
            "cost_total_usd": round(cost_total, 6),
            "gate_pass_rate_last_20": gate_pass_rate,
            "violations_24h": violation_rows[0][0] if violation_rows else 0,
            "runs_by_status": runs_by_status,
            "projects": len(repos_to_scan()),
        }
    )


async def _live_agents(app) -> list[dict]:
    """Agents belonging to runs that are effectively running, normalised to status running."""
    _app = _app_module()
    fetch_rows, effective_status = _app.fetch_rows, _app.effective_status

    run_rows = await fetch_rows(
        app, "SELECT run_id, slug, target_repo, status FROM runs WHERE status = 'running'"
    )
    live_run_ids = [
        (row[0], row[1], row[2])
        for row in run_rows
        if effective_status(app, row[0], row[3]) == "running"
    ]
    if not live_run_ids:
        return []

    placeholders = ", ".join("?" for _ in live_run_ids)
    ids = [item[0] for item in live_run_ids]

    agent_rows = await fetch_rows(
        app,
        f"""
        SELECT run_id, name, model, status, turns, cost_usd
        FROM agents WHERE run_id IN ({placeholders})
        """,
        tuple(ids),
    )
    finding_rows = await fetch_rows(
        app,
        f"""
        SELECT run_id, worker, topic, summary, seq
        FROM findings WHERE run_id IN ({placeholders}) ORDER BY seq
        """,
        tuple(ids),
    )
    event_rows = await fetch_rows(
        app,
        f"""
        SELECT run_id, actor, type, ts, seq
        FROM events WHERE run_id IN ({placeholders}) ORDER BY seq
        """,
        tuple(ids),
    )

    last_finding = {
        (row[0], row[1]): {"topic": row[2], "summary": row[3], "seq": row[4]}
        for row in finding_rows
    }
    last_event = {(row[0], row[1]): (row[2], row[3]) for row in event_rows}
    run_meta = {item[0]: (item[1], item[2]) for item in live_run_ids}

    agents = []
    for run_id, name, model, status, turns, cost in agent_rows:
        if status in ("completed", "error"):
            continue
        slug, target_repo = run_meta.get(run_id, (None, None))
        event_type, event_ts = last_event.get((run_id, name), (None, None))
        agents.append(
            {
                "run_id": run_id,
                "slug": slug,
                "target_repo": target_repo,
                "agent": name,
                "model": model,
                "status": "running",
                "turns": turns or 0,
                "cost_usd": cost or 0.0,
                "last_finding": last_finding.get((run_id, name)),
                "last_event_type": event_type,
                "last_event_ts": event_ts,
            }
        )
    return agents


@bp.get("/agents")
async def observe_agents(request):
    """Every live agent across every run."""
    mirror_all_runs = _app_module().mirror_all_runs

    await mirror_all_runs(request.app)
    return sanic_json(await _live_agents(request.app))


@bp.get("/timeline")
async def observe_timeline(request):
    """The most recent activity rows -- same shape as the feed, for first paint."""
    mirror_all_runs = _app_module().mirror_all_runs

    try:
        limit = int(request.args.get("limit", TIMELINE_DEFAULT_LIMIT))
    except (TypeError, ValueError):
        limit = TIMELINE_DEFAULT_LIMIT
    limit = max(1, min(limit, TIMELINE_MAX_LIMIT))

    await mirror_all_runs(request.app)
    return sanic_json(await _recent_activity(request.app, limit))


@bp.get("/feed")
async def observe_feed(request):
    """SSE `activity` frames for new cross-run activity; ends on disconnect or 2 h cap."""
    mirror_all_runs = _app_module().mirror_all_runs

    response = await request.respond(
        content_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )

    seen: set[tuple[str, int]] = set()
    deadline = time.monotonic() + FEED_MAX_DURATION_SECONDS
    first_pass = True

    while time.monotonic() < deadline:
        transport = getattr(request, "transport", None)
        if transport is None or transport.is_closing():
            return

        await mirror_all_runs(request.app)
        rows = await _recent_activity(request.app, TIMELINE_MAX_LIMIT)

        fresh = [row for row in rows if (row["run_id"], row["seq"]) not in seen]
        for row in rows:
            seen.add((row["run_id"], row["seq"]))

        if first_pass:
            # Replay the newest handful so a late subscriber sees context.
            fresh = list(reversed(fresh[:25]))
            first_pass = False
        else:
            fresh = list(reversed(fresh))

        for row in fresh:
            try:
                await response.send(f"event: activity\ndata: {stdlib_json.dumps(row)}\n\n")
            except Exception:
                return

        await asyncio.sleep(FEED_POLL_INTERVAL_SECONDS)

    await response.eof()
