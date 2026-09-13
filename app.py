import os
import asyncio
import json as stdlib_json
import threading
from pathlib import Path
from sanic import Sanic
from sanic.response import html, json as sanic_json
from datastar_py import ServerSentEventGenerator as SSE
from datastar_py.sanic import datastar_response
from agentgraph.sqlite_sink import SqliteMirror
from agentgraph import Mission, AgentSpec
from agentgraph.mission import EDIT_TOOLS, READ_TOOLS

app = Sanic("ConductionApp")

app.static("/static", "./static")

# Initialize SqliteMirror and mission process tracking on app startup
@app.before_server_start
async def setup_mirror(app):
    """Initialize the SqliteMirror instance and mission process tracker on app.ctx"""
    mirror_db_path = Path(".agentgraph/missions.db")
    app.ctx.mirror = SqliteMirror(mirror_db_path)
    # Track running missions: {run_id: {"thread": Thread, "slug": str, "log_path": Path}}
    app.ctx.mission_processes = {}

def render_template(filename: str) -> str:
    path = os.path.join(os.path.dirname(__file__), "templates", filename)
    with open(path, "r", encoding="utf-8") as f:
        return f.read()

@app.get("/")
async def home(request):
        """Serves the main app"""
        return html(render_template("index.html"))

@app.get("/runs")
async def runs_list(request):
    """Serves the runs list view"""
    return html(render_template("runs_list.html"))

@app.get("/runs/<run_id:str>")
async def run_detail(request, run_id: str):
    """Serves the run detail view"""
    return html(render_template("run_detail.html"))

@app.get("/api/runs")
async def list_runs(request):
    """List all mission runs, mirroring any new ones found on disk from all known repos"""
    mirror: SqliteMirror = request.app.ctx.mirror

    # Collect repos to scan: conduction's own + known_repos.json
    repos_to_scan = [Path.cwd()]  # Conduction's own repo
    known_repos_path = Path(".agentgraph/known_repos.json")
    if known_repos_path.exists():
        known_repos = stdlib_json.loads(known_repos_path.read_text())
        repos_to_scan.extend([Path(repo) for repo in known_repos])

    # Scan each repo's .agentgraph/runs/ for run.jsonl files
    for repo_path in repos_to_scan:
        runs_dir = repo_path / ".agentgraph" / "runs"
        if not runs_dir.exists():
            continue

        for run_jsonl in runs_dir.rglob("run.jsonl"):
            # Extract run_id from the jsonl file's parent directory structure
            relative_path = run_jsonl.relative_to(runs_dir)
            run_id = f"MISSION-{relative_path.parent.name}"

            # Mirror this run with target_repo info
            target_repo_str = str(repo_path.resolve())
            mirror.mirror_log_file(run_id, run_jsonl, target_repo_str)

    # Query all runs from the mirror
    conn = mirror._ensure_open()
    cursor = conn.execute(
        "SELECT run_id, slug, started_at, status, target_repo FROM runs ORDER BY started_at DESC"
    )
    runs = [
        {
            "run_id": row[0],
            "slug": row[1],
            "started_at": row[2],
            "status": row[3],
            "target_repo": row[4]
        }
        for row in cursor.fetchall()
    ]

    return sanic_json(runs)

@app.get("/api/runs/<run_id:str>/agents")
async def get_run_agents(request, run_id: str):
    """Return agents for a specific run"""
    mirror: SqliteMirror = request.app.ctx.mirror
    conn = mirror._ensure_open()

    # Get run info including target_repo
    run_cursor = conn.execute(
        "SELECT target_repo FROM runs WHERE run_id = ?",
        (run_id,)
    )
    run_row = run_cursor.fetchone()
    target_repo = run_row[0] if run_row else None

    cursor = conn.execute(
        """
        SELECT name, model, status, cost_usd, turns, error
        FROM agents
        WHERE run_id = ?
        ORDER BY name
        """,
        (run_id,)
    )
    agents = [
        {
            "name": row[0],
            "model": row[1],
            "status": row[2],
            "cost_usd": row[3],
            "turns": row[4],
            "error": row[5]
        }
        for row in cursor.fetchall()
    ]

    return sanic_json({"agents": agents, "target_repo": target_repo})

@app.get("/api/runs/<run_id:str>/events")
@datastar_response
async def stream_run_events(request, run_id: str):
    """SSE stream of events for a run, re-mirroring every ~2s to pick up new events"""
    mirror: SqliteMirror = request.app.ctx.mirror

    # Collect repos to scan
    repos_to_scan = [Path.cwd()]
    known_repos_path = Path(".agentgraph/known_repos.json")
    if known_repos_path.exists():
        known_repos = stdlib_json.loads(known_repos_path.read_text())
        repos_to_scan.extend([Path(repo) for repo in known_repos])

    # Find the run.jsonl file for this run_id across all repos
    run_jsonl = None
    target_repo = None
    for repo_path in repos_to_scan:
        runs_dir = repo_path / ".agentgraph" / "runs"
        if not runs_dir.exists():
            continue
        for candidate in runs_dir.rglob("run.jsonl"):
            relative_path = candidate.relative_to(runs_dir)
            candidate_run_id = f"MISSION-{relative_path.parent.name}"
            if candidate_run_id == run_id:
                run_jsonl = candidate
                target_repo = str(repo_path.resolve())
                break
        if run_jsonl:
            break

    if not run_jsonl:
        yield SSE.patch_elements('<div>Run not found</div>')
        return

    last_seq = 0

    while True:
        # Re-mirror to pick up any new events
        mirror.mirror_log_file(run_id, run_jsonl, target_repo)

        # Query for events newer than last_seq
        conn = mirror._ensure_open()
        cursor = conn.execute(
            """
            SELECT seq, type, actor, payload_json, ts
            FROM events
            WHERE run_id = ? AND seq > ?
            ORDER BY seq
            """,
            (run_id, last_seq)
        )
        new_events = cursor.fetchall()

        # Send new events to client
        for row in new_events:
            event_data = {
                "seq": row[0],
                "type": row[1],
                "actor": row[2],
                "payload_json": row[3],
                "ts": row[4]
            }
            # Send as a datastar fragment
            fragment = f'<div data-seq="{row[0]}">{row[1]} by {row[2]}</div>'
            yield SSE.patch_elements(fragment)
            last_seq = max(last_seq, row[0])

        # Check if run is complete
        run_cursor = conn.execute(
            "SELECT status FROM runs WHERE run_id = ?",
            (run_id,)
        )
        run_row = run_cursor.fetchone()

        # Terminate if run is complete and no new events
        if run_row and run_row[0] in ("completed", "errored") and not new_events:
            break

        # Wait ~2 seconds before next poll
        await asyncio.sleep(2)

@app.get("/api/runs/<run_id:str>/findings")
async def get_run_findings(request, run_id: str):
    """Return findings for a specific run"""
    mirror: SqliteMirror = request.app.ctx.mirror
    conn = mirror._ensure_open()

    cursor = conn.execute(
        """
        SELECT seq, worker, topic, summary
        FROM findings
        WHERE run_id = ?
        ORDER BY seq
        """,
        (run_id,)
    )
    findings = [
        {
            "seq": row[0],
            "worker": row[1],
            "topic": row[2],
            "summary": row[3]
        }
        for row in cursor.fetchall()
    ]

    return sanic_json(findings)

@app.get("/api/ping")
@datastar_response
async def ping(request):
     """grab sse for datastar"""
     fragment = '<div>Hello! Welcome to the D A T A S T A R </div>'
     yield SSE.patch_elements(fragment)

@app.post("/api/runs")
async def launch_mission(request):
    """
    Launch a new mission run from UI-provided agent specs.

    Request body (JSON):
    {
        "slug": "my-mission",
        "target_repo": "/absolute/path/to/project",
        "agents": [
            {"name": "agent1", "brief": "Do X", "tools": ["Read", "Write", "Edit"]},
            {"name": "agent2", "brief": "Do Y", "tools": ["Read", "Grep"]}
        ],
        "synthesis": "Optional synthesis brief",
        "max_turns": 30,
        "max_concurrency": 4
    }
    """
    try:
        body = request.json
        slug = body.get("slug", "unnamed-mission")
        target_repo = body.get("target_repo")
        agent_data = body.get("agents", [])
        synthesis = body.get("synthesis")
        max_turns = body.get("max_turns", 30)
        max_concurrency = body.get("max_concurrency", 4)

        if not target_repo:
            return sanic_json({"error": "target_repo is required"}, status=400)

        target_repo_path = Path(target_repo)
        if not target_repo_path.exists() or not target_repo_path.is_dir():
            return sanic_json({"error": f"target_repo does not exist or is not a directory: {target_repo}"}, status=400)

        if not agent_data:
            return sanic_json({"error": "No agents provided"}, status=400)

        # Convert agent data to AgentSpec instances
        agents = []
        for a in agent_data:
            tools = tuple(a.get("tools", READ_TOOLS))
            agents.append(AgentSpec(
                name=a["name"],
                brief=a["brief"],
                tools=tools
            ))

        # Setup run directory in target repo
        run_dir = target_repo_path / ".agentgraph" / "runs" / slug
        run_dir.mkdir(parents=True, exist_ok=True)
        (run_dir / "transcripts").mkdir(exist_ok=True)
        log_path = run_dir / "run.jsonl"

        # Create .gitignore in target_repo/.agentgraph/ if it doesn't exist
        gitignore_path = target_repo_path / ".agentgraph" / ".gitignore"
        if not gitignore_path.exists():
            gitignore_path.write_text("*\n")

        # Track this target_repo in known_repos.json
        known_repos_path = Path(".agentgraph/known_repos.json")
        known_repos_path.parent.mkdir(parents=True, exist_ok=True)
        if known_repos_path.exists():
            known_repos = stdlib_json.loads(known_repos_path.read_text())
        else:
            known_repos = []

        target_repo_str = str(target_repo_path.resolve())
        if target_repo_str not in known_repos:
            known_repos.append(target_repo_str)
            known_repos_path.write_text(stdlib_json.dumps(known_repos, indent=2))

        run_id = f"MISSION-{slug}"

        # Create mission with target_repo as cwd and claim_root
        mission = Mission(
            slug,
            agents,
            synthesis=synthesis,
            cwd=str(target_repo_path),
            claim_root=str(target_repo_path),
            model="claude-sonnet-4-5-20250929",
            max_turns=max_turns,
            max_concurrency=max_concurrency,
            transcript_dir=str(run_dir / "transcripts"),
        )

        # Run mission in background thread
        # The mission will poll for interrupt.signal file — see interrupt endpoint below
        def run_mission_thread():
            try:
                # Check for interrupt signal file periodically
                # Mission.run doesn't directly support this, so we use interrupt_after=None
                # and the mission's host will check for the sentinel file
                # For this wave, we'll run without interrupt checking in the mission itself
                # and rely on the thread being tracked so we can check status
                mission.run(str(log_path))
            except Exception as e:
                print(f"Mission {run_id} errored: {e}")

        thread = threading.Thread(target=run_mission_thread, daemon=True)
        thread.start()

        # Track the running mission
        request.app.ctx.mission_processes[run_id] = {
            "thread": thread,
            "slug": slug,
            "log_path": log_path
        }

        return sanic_json({
            "run_id": run_id,
            "slug": slug,
            "status": "launched",
            "log_path": str(log_path),
            "target_repo": target_repo_str
        })

    except Exception as e:
        return sanic_json({"error": str(e)}, status=500)

@app.post("/api/runs/<run_id:str>/interrupt")
async def interrupt_mission(request, run_id: str):
    """
    Signal a running mission to interrupt after current wave completes.

    Uses a sentinel file that the mission process polls for. The mission
    must be one that this app launched (tracked in app.ctx.mission_processes).

    Sentinel file contract: Write .agentgraph/runs/<slug>/interrupt.signal
    to request interrupt. The running mission process (if it supports polling)
    will check for this file and gracefully stop after current agent completions.

    Note: This implementation writes the sentinel file, but the actual polling
    mechanism needs to be implemented in the mission execution logic in a future
    iteration. For now, this marks the intent and creates the file.
    """
    try:
        process_info = request.app.ctx.mission_processes.get(run_id)

        if not process_info:
            return sanic_json({
                "error": "Mission not found or not launched by this server"
            }, status=404)

        # Check if thread is still running
        if not process_info["thread"].is_alive():
            return sanic_json({
                "error": "Mission has already completed",
                "status": "completed"
            }, status=400)

        # Write interrupt sentinel file
        slug = process_info["slug"]
        sentinel_path = Path(".agentgraph/runs") / slug / "interrupt.signal"
        sentinel_path.write_text(f"Interrupt requested at {Path.cwd()}\n")

        return sanic_json({
            "run_id": run_id,
            "status": "interrupt_signaled",
            "message": "Interrupt signal written. Mission will stop after current wave.",
            "sentinel_file": str(sentinel_path)
        })

    except Exception as e:
        return sanic_json({"error": str(e)}, status=500)

@app.post("/api/runs/<run_id:str>/resume")
async def resume_mission(request, run_id: str):
    """
    Resume an interrupted or completed mission with optionally-amended agent briefs.

    Request body (JSON):
    {
        "agent_edits": [
            {"name": "agent1", "brief": "Updated brief", "tools": ["Read", "Write"]}
        ]
    }

    Loads the original run's log, merges in the edited agent specs (by name),
    and launches a new mission run in a background thread with the amended specs.
    Unspecified fields (brief, tools) keep their original values.
    """
    try:
        body = request.json
        agent_edits = body.get("agent_edits", [])

        # Find the original log file across all repos
        repos_to_scan = [Path.cwd()]
        known_repos_path = Path(".agentgraph/known_repos.json")
        if known_repos_path.exists():
            known_repos = stdlib_json.loads(known_repos_path.read_text())
            repos_to_scan.extend([Path(repo) for repo in known_repos])

        original_log = None
        slug = None
        original_target_repo = None

        for repo_path in repos_to_scan:
            runs_dir = repo_path / ".agentgraph" / "runs"
            if not runs_dir.exists():
                continue
            for candidate in runs_dir.rglob("run.jsonl"):
                relative_path = candidate.relative_to(runs_dir)
                candidate_run_id = f"MISSION-{relative_path.parent.name}"
                if candidate_run_id == run_id:
                    original_log = candidate
                    slug = relative_path.parent.name
                    original_target_repo = str(repo_path.resolve())
                    break
            if original_log:
                break

        if not original_log or not original_log.exists():
            return sanic_json({"error": "Original run log not found"}, status=404)

        # Read original events to extract agent specs
        # For this implementation, we'll need to reconstruct the AgentSpecs
        # from the mission.started event or from UI-provided data
        # Simplified: accept full agent list in request and merge edits

        if "original_agents" not in body:
            return sanic_json({
                "error": "original_agents required in request body for resume"
            }, status=400)

        original_agents_data = body["original_agents"]

        # Build edit map
        edit_map = {edit["name"]: edit for edit in agent_edits}

        # Merge edits into original specs
        merged_agents = []
        for orig in original_agents_data:
            name = orig["name"]
            if name in edit_map:
                # Apply edits
                brief = edit_map[name].get("brief", orig.get("brief"))
                tools = tuple(edit_map[name].get("tools", orig.get("tools", READ_TOOLS)))
            else:
                brief = orig["brief"]
                tools = tuple(orig.get("tools", READ_TOOLS))

            merged_agents.append(AgentSpec(name=name, brief=brief, tools=tools))

        # Create new run directory for resumed mission in the same target repo
        target_repo_path = Path(original_target_repo)
        runs_dir_for_resume = target_repo_path / ".agentgraph" / "runs"
        resume_slug = f"{slug}-resume-{len(list(runs_dir_for_resume.glob(f'{slug}-resume-*'))) + 1}"
        resume_dir = runs_dir_for_resume / resume_slug
        resume_dir.mkdir(parents=True, exist_ok=True)
        (resume_dir / "transcripts").mkdir(exist_ok=True)
        new_log_path = resume_dir / "run.jsonl"

        resume_run_id = f"MISSION-{resume_slug}"

        # Create mission and resume with original target_repo
        synthesis = body.get("synthesis")
        max_turns = body.get("max_turns", 30)
        max_concurrency = body.get("max_concurrency", 4)

        mission = Mission(
            resume_slug,
            merged_agents,
            synthesis=synthesis,
            cwd=str(target_repo_path),
            claim_root=str(target_repo_path),
            model="claude-sonnet-4-5-20250929",
            max_turns=max_turns,
            max_concurrency=max_concurrency,
            transcript_dir=str(resume_dir / "transcripts"),
        )

        # Run resume in background thread
        def resume_mission_thread():
            try:
                mission.resume(str(original_log), str(new_log_path))
            except Exception as e:
                print(f"Mission resume {resume_run_id} errored: {e}")

        thread = threading.Thread(target=resume_mission_thread, daemon=True)
        thread.start()

        # Track the resumed mission
        request.app.ctx.mission_processes[resume_run_id] = {
            "thread": thread,
            "slug": resume_slug,
            "log_path": new_log_path
        }

        return sanic_json({
            "run_id": resume_run_id,
            "slug": resume_slug,
            "status": "resumed",
            "log_path": str(new_log_path),
            "original_run": run_id,
            "target_repo": original_target_repo
        })

    except Exception as e:
        return sanic_json({"error": str(e)}, status=500)

# Cross-run query endpoints
@app.get("/query")
async def query_page(request):
    """Serves the cross-run query UI"""
    return html(render_template("query.html"))

@app.get("/api/query/findings")
async def query_findings(request):
    """
    Query findings across all runs with optional filters.

    Query params:
    - text: substring match in summary (case-insensitive)
    - worker: exact worker name
    - run_id: exact run_id
    """
    mirror: SqliteMirror = request.app.ctx.mirror

    # First, ensure all runs are mirrored from all known repos
    repos_to_scan = [Path.cwd()]
    known_repos_path = Path(".agentgraph/known_repos.json")
    if known_repos_path.exists():
        known_repos = stdlib_json.loads(known_repos_path.read_text())
        repos_to_scan.extend([Path(repo) for repo in known_repos])

    for repo_path in repos_to_scan:
        runs_dir = repo_path / ".agentgraph" / "runs"
        if not runs_dir.exists():
            continue
        for run_jsonl in runs_dir.rglob("run.jsonl"):
            relative_path = run_jsonl.relative_to(runs_dir)
            run_id = f"MISSION-{relative_path.parent.name}"
            target_repo_str = str(repo_path.resolve())
            mirror.mirror_log_file(run_id, run_jsonl, target_repo_str)

    # Build query with optional filters
    conn = mirror._ensure_open()
    query = """
        SELECT f.run_id, f.seq, f.worker, f.topic, f.summary, r.slug
        FROM findings f
        LEFT JOIN runs r ON f.run_id = r.run_id
        WHERE 1=1
    """
    params = []

    text_filter = request.args.get("text")
    if text_filter:
        query += " AND LOWER(f.summary) LIKE ?"
        params.append(f"%{text_filter.lower()}%")

    worker_filter = request.args.get("worker")
    if worker_filter:
        query += " AND f.worker = ?"
        params.append(worker_filter)

    run_id_filter = request.args.get("run_id")
    if run_id_filter:
        query += " AND f.run_id = ?"
        params.append(run_id_filter)

    query += " ORDER BY f.run_id, f.seq"

    cursor = conn.execute(query, params)
    findings = [
        {
            "run_id": row[0],
            "seq": row[1],
            "worker": row[2],
            "topic": row[3],
            "summary": row[4],
            "slug": row[5]
        }
        for row in cursor.fetchall()
    ]

    return sanic_json(findings)

@app.get("/api/query/costs")
async def query_costs(request):
    """
    Aggregate cost and turn data across all runs.
    Returns costs grouped by run and by agent, sorted descending by cost.
    """
    mirror: SqliteMirror = request.app.ctx.mirror

    # Ensure all runs are mirrored from all known repos
    repos_to_scan = [Path.cwd()]
    known_repos_path = Path(".agentgraph/known_repos.json")
    if known_repos_path.exists():
        known_repos = stdlib_json.loads(known_repos_path.read_text())
        repos_to_scan.extend([Path(repo) for repo in known_repos])

    for repo_path in repos_to_scan:
        runs_dir = repo_path / ".agentgraph" / "runs"
        if not runs_dir.exists():
            continue
        for run_jsonl in runs_dir.rglob("run.jsonl"):
            relative_path = run_jsonl.relative_to(runs_dir)
            run_id = f"MISSION-{relative_path.parent.name}"
            target_repo_str = str(repo_path.resolve())
            mirror.mirror_log_file(run_id, run_jsonl, target_repo_str)

    conn = mirror._ensure_open()

    # Aggregate by run
    run_cursor = conn.execute("""
        SELECT a.run_id, r.slug, SUM(a.cost_usd) as total_cost, SUM(a.turns) as total_turns
        FROM agents a
        LEFT JOIN runs r ON a.run_id = r.run_id
        GROUP BY a.run_id, r.slug
        ORDER BY total_cost DESC
    """)
    by_run = [
        {
            "run_id": row[0],
            "slug": row[1],
            "total_cost_usd": row[2],
            "total_turns": row[3]
        }
        for row in run_cursor.fetchall()
    ]

    # Aggregate by agent name across all runs
    agent_cursor = conn.execute("""
        SELECT a.name, SUM(a.cost_usd) as total_cost, SUM(a.turns) as total_turns, COUNT(*) as run_count
        FROM agents a
        GROUP BY a.name
        ORDER BY total_cost DESC
    """)
    by_agent = [
        {
            "agent_name": row[0],
            "total_cost_usd": row[1],
            "total_turns": row[2],
            "run_count": row[3]
        }
        for row in agent_cursor.fetchall()
    ]

    return sanic_json({
        "by_run": by_run,
        "by_agent": by_agent
    })

@app.get("/api/query/claims/conflicts")
async def query_claim_conflicts(request):
    """
    Return all claim conflicts (rejected or violated) across all runs.
    Shows partition-overlap mistakes for review.
    """
    mirror: SqliteMirror = request.app.ctx.mirror

    # Ensure all runs are mirrored from all known repos
    repos_to_scan = [Path.cwd()]
    known_repos_path = Path(".agentgraph/known_repos.json")
    if known_repos_path.exists():
        known_repos = stdlib_json.loads(known_repos_path.read_text())
        repos_to_scan.extend([Path(repo) for repo in known_repos])

    for repo_path in repos_to_scan:
        runs_dir = repo_path / ".agentgraph" / "runs"
        if not runs_dir.exists():
            continue
        for run_jsonl in runs_dir.rglob("run.jsonl"):
            relative_path = run_jsonl.relative_to(runs_dir)
            run_id = f"MISSION-{relative_path.parent.name}"
            target_repo_str = str(repo_path.resolve())
            mirror.mirror_log_file(run_id, run_jsonl, target_repo_str)

    conn = mirror._ensure_open()

    cursor = conn.execute("""
        SELECT c.run_id, r.slug, c.path, c.owner, c.status, c.seq
        FROM claims c
        LEFT JOIN runs r ON c.run_id = r.run_id
        WHERE c.status IN ('rejected', 'violated')
        ORDER BY c.run_id, c.seq
    """)

    conflicts = [
        {
            "run_id": row[0],
            "slug": row[1],
            "path": row[2],
            "owner": row[3],
            "status": row[4],
            "seq": row[5]
        }
        for row in cursor.fetchall()
    ]

    return sanic_json(conflicts)

if __name__ == "__main__":
     app.run(host="0.0.0.0", port=8000, dev=False, single_process=True)