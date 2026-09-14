import os
import asyncio
import hashlib
import json as stdlib_json
import re
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import unquote
from sanic import Sanic
from sanic.response import html, json as sanic_json
from datastar_py import ServerSentEventGenerator as SSE
from datastar_py.sanic import datastar_response
from agentgraph.sqlite_sink import SqliteMirror
from agentgraph import Mission, AgentSpec
from agentgraph.mission import EDIT_TOOLS, READ_TOOLS
from agentgraph.dispatcher import ScriptedWorker
from agentgraph.gates import gate_from_spec, validate_gate_spec
from agentgraph.sdk_workers import resolve_workers, available_sdks

APP_ROOT = Path(__file__).resolve().parent
KNOWN_REPOS_PATH = APP_ROOT / ".agentgraph" / "known_repos.json"
MIRROR_DB_PATH = APP_ROOT / ".agentgraph" / "missions.db"

SLUG_PATTERN = re.compile(r"^[A-Za-z0-9._-]{1,64}$")
MAX_CONCURRENT_MISSIONS = 4
MISSION_JOIN_TIMEOUT_SECONDS = 30
STREAM_POLL_INTERVAL_SECONDS = 2
STREAM_MAX_DURATION_SECONDS = 2 * 60 * 60
TERMINAL_RUN_STATUSES = ("completed", "failed", "errored", "stale")
# A run this process did not launch, whose log has not grown for this long and
# never recorded mission.completed (pre-hardening logs), is reported as stale
# so its stream can end instead of polling until the 2 h cap.
STALE_RUN_SECONDS = 600
DEFAULT_MISSION_MODEL = "claude-sonnet-4-5-20250929"

app = Sanic("ConductionApp")
# Sanic hands path params over still percent-encoded; run ids contain "@".
app.router.register_pattern("runid", unquote, r"[^/]+")

app.static("/static", str(APP_ROOT / "static"))


@dataclass(frozen=True)
class RunRef:
    """A run on disk, addressed by the composite app-level run_id."""

    run_id: str
    slug: str
    target_repo: Path
    log_path: Path
    run_dir: Path


def repo_key(target_repo: Path | str) -> str:
    return hashlib.sha1(str(Path(target_repo).resolve()).lower().encode()).hexdigest()[:8]


def compose_run_id(slug: str, target_repo: Path | str) -> str:
    return f"MISSION-{slug}@{repo_key(target_repo)}"


def read_known_repos() -> list[str]:
    if not KNOWN_REPOS_PATH.exists():
        return []
    try:
        entries = stdlib_json.loads(KNOWN_REPOS_PATH.read_text(encoding="utf-8"))
    except (ValueError, OSError):
        return []
    if not isinstance(entries, list):
        return []
    return [entry for entry in entries if isinstance(entry, str)]


def remember_repo(target_repo: Path) -> None:
    known_repos = read_known_repos()
    target_repo_str = str(target_repo.resolve())
    if any(existing.lower() == target_repo_str.lower() for existing in known_repos):
        return
    known_repos.append(target_repo_str)
    KNOWN_REPOS_PATH.parent.mkdir(parents=True, exist_ok=True)
    KNOWN_REPOS_PATH.write_text(stdlib_json.dumps(known_repos, indent=2), encoding="utf-8")


def allowed_repo_roots() -> list[Path]:
    configured = os.environ.get("CONDUCTION_ALLOWED_ROOTS", "").strip()
    if not configured:
        return [Path.home().resolve()]
    roots = []
    for entry in configured.split(os.pathsep):
        entry = entry.strip()
        if not entry:
            continue
        try:
            roots.append(Path(entry).resolve())
        except OSError:
            continue
    return roots


def validate_slug(slug: object) -> tuple[str | None, str | None]:
    if not isinstance(slug, str) or not SLUG_PATTERN.match(slug):
        return None, "slug must match ^[A-Za-z0-9._-]{1,64}$"
    if ".." in slug:
        return None, "slug must not contain '..'"
    return slug, None


def validate_target_repo(raw_target_repo: object) -> tuple[Path | None, str | None]:
    if not isinstance(raw_target_repo, str) or not raw_target_repo.strip():
        return None, "target_repo is required"
    try:
        target_repo = Path(raw_target_repo).expanduser().resolve()
    except OSError:
        return None, f"target_repo is not a usable path: {raw_target_repo}"
    if not target_repo.is_dir():
        return None, f"target_repo does not exist or is not a directory: {raw_target_repo}"
    if target_repo == APP_ROOT:
        return target_repo, None
    target_repo_str = str(target_repo)
    for known in read_known_repos():
        try:
            if str(Path(known).resolve()).lower() == target_repo_str.lower():
                return target_repo, None
        except OSError:
            continue
    for root in allowed_repo_roots():
        if target_repo == root or target_repo.is_relative_to(root):
            return target_repo, None
    return None, (
        f"target_repo is outside the allowed roots: {raw_target_repo}. "
        "Set CONDUCTION_ALLOWED_ROOTS to permit it."
    )


def repos_to_scan() -> list[Path]:
    repos = [APP_ROOT]
    seen = {repo_key(APP_ROOT)}
    for entry in read_known_repos():
        try:
            resolved = Path(entry).resolve()
        except OSError:
            continue
        key = repo_key(resolved)
        if key in seen:
            continue
        seen.add(key)
        repos.append(resolved)
    return repos


def resolve_runs(app) -> list[RunRef]:
    """Every run.jsonl under conduction's own repo plus every known repo."""
    refs: list[RunRef] = []
    for repo in repos_to_scan():
        runs_dir = repo / ".agentgraph" / "runs"
        if not runs_dir.is_dir():
            continue
        key = repo_key(repo)
        for log_path in sorted(runs_dir.rglob("run.jsonl")):
            run_dir = log_path.parent
            slug = run_dir.name
            refs.append(
                RunRef(
                    run_id=f"MISSION-{slug}@{key}",
                    slug=slug,
                    target_repo=repo,
                    log_path=log_path,
                    run_dir=run_dir,
                )
            )
    return refs


def find_run(app, run_id: str) -> RunRef | None:
    for ref in resolve_runs(app):
        if ref.run_id == run_id:
            return ref
    return None


def mirror_run(app, ref: RunRef) -> None:
    app.ctx.mirror.mirror_log_file(ref.run_id, ref.log_path, target_repo=str(ref.target_repo))


def is_managed_and_alive(app, run_id: str) -> bool:
    entry = app.ctx.mission_processes.get(run_id)
    return bool(entry and entry["thread"].is_alive())


def effective_status(app, run_id: str, recorded_status: str | None) -> str | None:
    if recorded_status != "running" or is_managed_and_alive(app, run_id):
        return recorded_status
    ref = find_run(app, run_id)
    if ref is None or not ref.log_path.exists():
        return recorded_status
    if time.time() - ref.log_path.stat().st_mtime > STALE_RUN_SECONDS:
        return "stale"
    return recorded_status


def mirror_runs(app, refs: list[RunRef]) -> None:
    for ref in refs:
        mirror_run(app, ref)


def fetch_rows_blocking(app, query: str, params: tuple) -> list:
    return app.ctx.mirror._ensure_open().execute(query, params).fetchall()


async def fetch_rows(app, query: str, params: tuple = ()) -> list:
    # One sqlite connection shared across worker threads: the lock serializes it.
    async with app.ctx.mirror_lock:
        return await asyncio.to_thread(fetch_rows_blocking, app, query, tuple(params))


async def mirror_all_runs(app) -> list[RunRef]:
    refs = await asyncio.to_thread(resolve_runs, app)
    async with app.ctx.mirror_lock:
        await asyncio.to_thread(mirror_runs, app, refs)
    return refs


async def mirror_single_run(app, ref: RunRef) -> None:
    async with app.ctx.mirror_lock:
        await asyncio.to_thread(mirror_run, app, ref)


def purge_legacy_run_ids(app) -> None:
    """Rows written before composite run ids exist would double every run in the UI."""
    connection = app.ctx.mirror._ensure_open()
    for table in ("runs", "agents", "events", "findings", "claims"):
        connection.execute(f"DELETE FROM {table} WHERE run_id NOT LIKE '%@%'")
    connection.commit()


def reap_mission_threads(app) -> None:
    for entry in app.ctx.mission_processes.values():
        if entry["status"] == "running" and not entry["thread"].is_alive():
            entry["status"] = "finished"


def active_mission_count(app) -> int:
    reap_mission_threads(app)
    return sum(1 for entry in app.ctx.mission_processes.values() if entry["status"] == "running")


def join_mission_threads(threads: list[threading.Thread], timeout_seconds: float) -> None:
    deadline = time.monotonic() + timeout_seconds
    for thread in threads:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return
        thread.join(remaining)


@app.before_server_start
async def setup_mirror(app):
    """Initialize the SqliteMirror instance and mission process tracker on app.ctx"""
    app.ctx.mirror = SqliteMirror(MIRROR_DB_PATH)
    app.ctx.mirror_lock = asyncio.Lock()
    app.ctx.mission_processes = {}
    async with app.ctx.mirror_lock:
        await asyncio.to_thread(purge_legacy_run_ids, app)


@app.before_server_stop
async def stop_missions(app):
    """Signal every tracked mission and give its thread a bounded chance to unwind"""
    for entry in app.ctx.mission_processes.values():
        entry["stop_event"].set()
    threads = [
        entry["thread"]
        for entry in app.ctx.mission_processes.values()
        if entry["thread"].is_alive()
    ]
    if threads:
        await asyncio.to_thread(join_mission_threads, threads, MISSION_JOIN_TIMEOUT_SECONDS)


def render_template(filename: str) -> str:
    path = APP_ROOT / "templates" / filename
    return path.read_text(encoding="utf-8")


@app.get("/")
async def home(request):
    """Serves the main app"""
    return html(render_template("index.html"))


@app.get("/runs")
async def runs_list(request):
    """Serves the runs list view"""
    return html(render_template("runs_list.html"))


@app.get("/runs/<run_id:runid>")
async def run_detail(request, run_id: str):
    """Serves the run detail view"""
    return html(render_template("run_detail.html"))


@app.get("/api/runs")
async def list_runs(request):
    """List all mission runs, mirroring any new ones found on disk from all known repos"""
    await mirror_all_runs(request.app)
    rows = await fetch_rows(
        request.app,
        "SELECT run_id, slug, started_at, status, target_repo FROM runs ORDER BY started_at DESC",
    )
    runs = [
        {
            "run_id": row[0],
            "slug": row[1],
            "started_at": row[2],
            "status": effective_status(request.app, row[0], row[3]),
            "target_repo": row[4],
        }
        for row in rows
    ]
    return sanic_json(runs)


@app.get("/api/runs/<run_id:runid>/agents")
async def get_run_agents(request, run_id: str):
    """Return agents for a specific run"""
    run_rows = await fetch_rows(
        request.app,
        "SELECT target_repo FROM runs WHERE run_id = ?",
        (run_id,),
    )
    target_repo = run_rows[0][0] if run_rows else None

    agent_rows = await fetch_rows(
        request.app,
        """
        SELECT name, model, status, cost_usd, turns, error
        FROM agents
        WHERE run_id = ?
        ORDER BY name
        """,
        (run_id,),
    )
    agents = [
        {
            "name": row[0],
            "model": row[1],
            "status": row[2],
            "cost_usd": row[3],
            "turns": row[4],
            "error": row[5],
        }
        for row in agent_rows
    ]
    return sanic_json({"agents": agents, "target_repo": target_repo})


@app.get("/api/runs/<run_id:runid>/events")
async def stream_run_events(request, run_id: str):
    """Plain text/event-stream of run events: named `run-event` frames, then `run-complete`"""
    current_app = request.app
    ref = await asyncio.to_thread(find_run, current_app, run_id)
    if ref is None:
        return sanic_json({"error": f"Run not found: {run_id}"}, status=404)

    response = await request.respond(
        content_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )

    last_seq = 0
    deadline = time.monotonic() + STREAM_MAX_DURATION_SECONDS

    async def send_frame(event_name: str, data: dict) -> bool:
        transport = getattr(request, "transport", None)
        if transport is None or transport.is_closing():
            return False
        try:
            await response.send(f"event: {event_name}\ndata: {stdlib_json.dumps(data)}\n\n")
        except Exception:
            return False
        return True

    while time.monotonic() < deadline:
        transport = getattr(request, "transport", None)
        if transport is None or transport.is_closing():
            return

        await mirror_single_run(current_app, ref)

        event_rows = await fetch_rows(
            current_app,
            """
            SELECT seq, type, actor, payload_json, ts
            FROM events
            WHERE run_id = ? AND seq > ?
            ORDER BY seq
            """,
            (run_id, last_seq),
        )

        for row in event_rows:
            try:
                payload = stdlib_json.loads(row[3]) if row[3] else {}
            except ValueError:
                payload = {}
            delivered = await send_frame(
                "run-event",
                {
                    "seq": row[0],
                    "type": row[1],
                    "actor": row[2],
                    "ts": row[4],
                    "payload": payload,
                },
            )
            if not delivered:
                return
            last_seq = max(last_seq, row[0])

        status_rows = await fetch_rows(
            current_app,
            "SELECT status FROM runs WHERE run_id = ?",
            (run_id,),
        )
        status = effective_status(current_app, run_id, status_rows[0][0] if status_rows else None)

        if status in TERMINAL_RUN_STATUSES and not event_rows:
            await send_frame("run-complete", {"status": status})
            await response.eof()
            return

        await asyncio.sleep(STREAM_POLL_INTERVAL_SECONDS)

    await send_frame("run-complete", {"status": "stream-timeout"})
    await response.eof()


@app.get("/api/runs/<run_id:runid>/findings")
async def get_run_findings(request, run_id: str):
    """Return findings for a specific run"""
    rows = await fetch_rows(
        request.app,
        """
        SELECT seq, worker, topic, summary
        FROM findings
        WHERE run_id = ?
        ORDER BY seq
        """,
        (run_id,),
    )
    findings = [
        {"seq": row[0], "worker": row[1], "topic": row[2], "summary": row[3]}
        for row in rows
    ]
    return sanic_json(findings)


@app.get("/api/ping")
@datastar_response
async def ping(request):
    """grab sse for datastar"""
    fragment = '<div>Hello! Welcome to the D A T A S T A R </div>'
    yield SSE.patch_elements(fragment)


def prepare_run_directory(run_dir: Path) -> None:
    run_dir.mkdir(parents=True, exist_ok=True)
    (run_dir / "transcripts").mkdir(exist_ok=True)
    gitignore_path = run_dir.parent.parent / ".gitignore"
    if not gitignore_path.exists():
        gitignore_path.write_text("*\n", encoding="utf-8")


DRY_RUN_AGENT_SECONDS = 1.5
DRY_RUN_INVOCATIONS_FILE = "dry-run-invocations.txt"


def create_dry_run_worker(run_dir: Path, agent_names: list[str]):
    """ScriptedWorker for CONDUCTION_DRY_RUN=1: no model calls, ~1.5 s per agent.

    Cache hits never reach the worker and are indistinguishable in the log by
    design, so each real invocation is appended to run_dir/dry-run-invocations.txt
    -- the only honest record of which agents actually executed.
    """
    invocations_path = run_dir / DRY_RUN_INVOCATIONS_FILE

    def dry_run_responder(request, api):
        with invocations_path.open("a", encoding="utf-8") as invocations:
            invocations.write(f"{request.worker}\n")
        api.emit_finding(
            topic="result",
            summary="dry-run completed",
            detail='{"files_changed": [], "verification": "dry-run", "done": true}',
        )
        return f"dry-run: {request.worker}"

    return ScriptedWorker(dry_run_responder, delays={name: DRY_RUN_AGENT_SECONDS for name in agent_names})


def build_agent_specs(agent_data: list[dict]) -> list[AgentSpec]:
    """AgentSpec per UI spec dict, carrying the optional `owns` partition."""
    return [
        AgentSpec(
            name=spec["name"],
            brief=spec["brief"],
            tools=tuple(spec.get("tools", READ_TOOLS)),
            owns=tuple(spec.get("owns", ()) or ()),
            meta={"sdk": spec["sdk"]} if spec.get("sdk") else {},
        )
        for spec in agent_data
    ]


def resolve_mission_gate(gate_spec: object, target_repo: Path, agents: list[AgentSpec]):
    """(gate_callable, error) -- gate presets compiled from the request body."""
    if gate_spec is None:
        return None, None
    if not isinstance(gate_spec, dict):
        return None, "gate must be an object"
    errors = validate_gate_spec(gate_spec)
    if errors:
        return None, "; ".join(errors)
    owns = {spec.name: tuple(spec.owns) for spec in agents}
    return gate_from_spec(gate_spec, cwd=str(target_repo), owns=owns), None


def resolve_mission_worker(run_dir: Path, agents: list[AgentSpec]):
    """(worker, error). CONDUCTION_DRY_RUN=1 wins over any per-agent sdk."""
    if os.environ.get("CONDUCTION_DRY_RUN") == "1":
        return create_dry_run_worker(run_dir, [spec.name for spec in agents]), None
    agent_sdks = {spec.name: spec.meta["sdk"] for spec in agents if spec.meta.get("sdk")}
    if not agent_sdks:
        return None, None
    try:
        return resolve_workers(agent_sdks), None
    except ValueError as error:
        return None, str(error)


def validate_agent_sdks(agent_data: list[dict]) -> str | None:
    """Reject unknown sdk names up front, dry-run or not."""
    agent_sdks = {
        spec.get("name"): spec["sdk"] for spec in agent_data if spec.get("sdk")
    }
    if not agent_sdks:
        return None
    try:
        resolve_workers(agent_sdks)
    except ValueError as error:
        return str(error)
    return None


@app.get("/api/sdks")
async def list_sdks(request):
    """Which per-agent SDKs this host can actually reach, plus the dry-run flag."""
    return sanic_json(
        {
            "sdks": available_sdks(),
            "dry_run": os.environ.get("CONDUCTION_DRY_RUN") == "1",
        }
    )


@app.get("/launch")
async def launch_page(request):
    """Serves the mission launch form"""
    return html(render_template("launch.html"))


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
        body = request.json or {}
        slug, slug_error = validate_slug(body.get("slug"))
        if slug_error:
            return sanic_json({"error": slug_error}, status=400)

        target_repo, repo_error = validate_target_repo(body.get("target_repo"))
        if repo_error:
            return sanic_json({"error": repo_error}, status=400)

        agent_data = body.get("agents", [])
        if not agent_data:
            return sanic_json({"error": "No agents provided"}, status=400)

        synthesis = body.get("synthesis")
        max_turns = body.get("max_turns", 30)
        max_concurrency = body.get("max_concurrency", 4)

        sdk_error = validate_agent_sdks(agent_data)
        if sdk_error:
            return sanic_json({"error": sdk_error}, status=400)

        agents = build_agent_specs(agent_data)

        gate_spec = body.get("gate")
        gate, gate_error = resolve_mission_gate(gate_spec, target_repo, agents)
        if gate_error:
            return sanic_json({"error": gate_error}, status=400)

        run_dir = (target_repo / ".agentgraph" / "runs" / slug).resolve()
        if not run_dir.is_relative_to(target_repo):
            return sanic_json({"error": "resolved run directory escapes target_repo"}, status=400)

        log_path = run_dir / "run.jsonl"
        if log_path.exists():
            return sanic_json(
                {
                    "error": f"A run already exists for slug '{slug}' in this repo. "
                             "Use /resume or a new slug.",
                    "run_id": compose_run_id(slug, target_repo),
                },
                status=409,
            )

        if active_mission_count(request.app) >= MAX_CONCURRENT_MISSIONS:
            return sanic_json(
                {"error": f"Too many missions running (max {MAX_CONCURRENT_MISSIONS})"},
                status=409,
            )

        await asyncio.to_thread(prepare_run_directory, run_dir)
        await asyncio.to_thread(remember_repo, target_repo)

        # Mission.name stays the slug (the JSONL run_id derives from it); the
        # app-level run_id below is the repo-qualified composite.
        run_id = compose_run_id(slug, target_repo)

        mission = Mission(
            slug,
            agents,
            synthesis=synthesis,
            cwd=str(target_repo),
            claim_root=str(target_repo),
            model=DEFAULT_MISSION_MODEL,
            max_turns=max_turns,
            max_concurrency=max_concurrency,
            transcript_dir=str(run_dir / "transcripts"),
            gate=gate,
        )

        worker, worker_error = resolve_mission_worker(run_dir, agents)
        if worker_error:
            return sanic_json({"error": worker_error}, status=400)

        stop_event = threading.Event()
        entry = {
            "thread": None,
            "slug": slug,
            "run_dir": run_dir,
            "log_path": log_path,
            "stop_event": stop_event,
            "target_repo": str(target_repo),
            "status": "running",
        }

        def run_mission_thread():
            try:
                mission.run(str(log_path), worker=worker, stop_when=stop_event.is_set)
            except Exception as error:
                print(f"Mission {run_id} errored: {error}")
            finally:
                entry["status"] = "finished"

        thread = threading.Thread(target=run_mission_thread, name=f"mission-{run_id}")
        entry["thread"] = thread
        request.app.ctx.mission_processes[run_id] = entry
        thread.start()

        return sanic_json(
            {
                "run_id": run_id,
                "slug": slug,
                "status": "launched",
                "log_path": str(log_path),
                "target_repo": str(target_repo),
                "gate": gate is not None,
            }
        )

    except Exception as error:
        return sanic_json({"error": str(error)}, status=500)


@app.post("/api/runs/<run_id:runid>/interrupt")
async def interrupt_mission(request, run_id: str):
    """
    Signal a running mission to stop after its in-flight agent completions.

    Sets the mission's stop_event (polled by the runtime via stop_when) and
    writes run_dir/interrupt.signal as a durable marker of the request.
    """
    try:
        reap_mission_threads(request.app)
        entry = request.app.ctx.mission_processes.get(run_id)

        if not entry:
            return sanic_json(
                {"error": "Mission not found or not launched by this server"},
                status=404,
            )

        if not entry["thread"].is_alive():
            return sanic_json(
                {"error": "Mission thread is no longer alive", "status": entry["status"]},
                status=409,
            )

        entry["stop_event"].set()
        signal_path = Path(entry["run_dir"]) / "interrupt.signal"
        await asyncio.to_thread(
            signal_path.write_text,
            f"interrupt requested at {time.time()}\n",
            "utf-8",
        )

        return sanic_json(
            {"run_id": run_id, "will_stop_after": "current agent completions"},
            status=202,
        )

    except Exception as error:
        return sanic_json({"error": str(error)}, status=500)


@app.post("/api/runs/<run_id:runid>/resume")
async def resume_mission(request, run_id: str):
    """
    Resume an interrupted or completed mission with optionally-amended agent briefs.

    Request body (JSON):
    {
        "original_agents": [{"name": "agent1", "brief": "...", "tools": [...]}],
        "agent_edits": [{"name": "agent1", "brief": "Updated brief"}]
    }
    """
    try:
        body = request.json or {}
        agent_edits = body.get("agent_edits", [])

        ref = await asyncio.to_thread(find_run, request.app, run_id)
        if ref is None or not ref.log_path.exists():
            return sanic_json({"error": "Original run log not found"}, status=404)

        if "original_agents" not in body:
            return sanic_json(
                {"error": "original_agents required in request body for resume"},
                status=400,
            )

        edit_map = {edit["name"]: edit for edit in agent_edits}
        merged_specs = []
        for original in body["original_agents"]:
            name = original["name"]
            edit = edit_map.get(name, {})
            merged = {
                "name": name,
                "brief": edit.get("brief", original.get("brief")),
                "tools": edit.get("tools", original.get("tools", READ_TOOLS)),
                "owns": edit.get("owns", original.get("owns", ())),
            }
            sdk = edit.get("sdk", original.get("sdk"))
            if sdk:
                merged["sdk"] = sdk
            merged_specs.append(merged)

        sdk_error = validate_agent_sdks(merged_specs)
        if sdk_error:
            return sanic_json({"error": sdk_error}, status=400)
        merged_agents = build_agent_specs(merged_specs)

        target_repo = ref.target_repo
        resume_suffix = f"-resume-{int(time.time())}"
        resume_slug = f"{ref.slug[: 64 - len(resume_suffix)]}{resume_suffix}"
        resume_slug, slug_error = validate_slug(resume_slug)
        if slug_error:
            return sanic_json({"error": f"generated resume slug rejected: {slug_error}"}, status=400)

        resume_dir = (target_repo / ".agentgraph" / "runs" / resume_slug).resolve()
        new_log_path = resume_dir / "run.jsonl"
        if new_log_path.exists():
            return sanic_json(
                {"error": f"A resume run already exists for slug '{resume_slug}'"},
                status=409,
            )

        if active_mission_count(request.app) >= MAX_CONCURRENT_MISSIONS:
            return sanic_json(
                {"error": f"Too many missions running (max {MAX_CONCURRENT_MISSIONS})"},
                status=409,
            )

        await asyncio.to_thread(prepare_run_directory, resume_dir)

        synthesis = body.get("synthesis")
        max_turns = body.get("max_turns", 30)
        max_concurrency = body.get("max_concurrency", 4)

        gate, gate_error = resolve_mission_gate(body.get("gate"), target_repo, merged_agents)
        if gate_error:
            return sanic_json({"error": gate_error}, status=400)

        mission = Mission(
            resume_slug,
            merged_agents,
            synthesis=synthesis,
            cwd=str(target_repo),
            claim_root=str(target_repo),
            model=DEFAULT_MISSION_MODEL,
            max_turns=max_turns,
            max_concurrency=max_concurrency,
            transcript_dir=str(resume_dir / "transcripts"),
            gate=gate,
        )

        worker, worker_error = resolve_mission_worker(resume_dir, merged_agents)
        if worker_error:
            return sanic_json({"error": worker_error}, status=400)

        resume_run_id = compose_run_id(resume_slug, target_repo)
        original_log_path = ref.log_path

        stop_event = threading.Event()
        entry = {
            "thread": None,
            "slug": resume_slug,
            "run_dir": resume_dir,
            "log_path": new_log_path,
            "stop_event": stop_event,
            "target_repo": str(target_repo),
            "status": "running",
        }

        def resume_mission_thread():
            try:
                mission.resume(
                    str(original_log_path),
                    str(new_log_path),
                    worker=worker,
                    stop_when=stop_event.is_set,
                )
            except Exception as error:
                print(f"Mission resume {resume_run_id} errored: {error}")
            finally:
                entry["status"] = "finished"

        thread = threading.Thread(target=resume_mission_thread, name=f"mission-{resume_run_id}")
        entry["thread"] = thread
        request.app.ctx.mission_processes[resume_run_id] = entry
        thread.start()

        return sanic_json(
            {
                "run_id": resume_run_id,
                "slug": resume_slug,
                "status": "resumed",
                "log_path": str(new_log_path),
                "original_run": run_id,
                "target_repo": str(target_repo),
                "gate": gate is not None,
            }
        )

    except Exception as error:
        return sanic_json({"error": str(error)}, status=500)


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
    await mirror_all_runs(request.app)

    query = """
        SELECT f.run_id, f.seq, f.worker, f.topic, f.summary, r.slug
        FROM findings f
        LEFT JOIN runs r ON f.run_id = r.run_id
        WHERE 1=1
    """
    params = []

    text_filter = request.args.get("text")
    if text_filter:
        query += " AND (LOWER(f.summary) LIKE ? OR LOWER(f.topic) LIKE ?)"
        params.extend([f"%{text_filter.lower()}%"] * 2)

    worker_filter = request.args.get("worker")
    if worker_filter:
        query += " AND f.worker = ?"
        params.append(worker_filter)

    run_id_filter = request.args.get("run_id")
    if run_id_filter:
        query += " AND f.run_id = ?"
        params.append(run_id_filter)

    query += " ORDER BY f.run_id, f.seq"

    rows = await fetch_rows(request.app, query, tuple(params))
    findings = [
        {
            "run_id": row[0],
            "seq": row[1],
            "worker": row[2],
            "topic": row[3],
            "summary": row[4],
            "slug": row[5],
        }
        for row in rows
    ]
    return sanic_json(findings)


@app.get("/api/query/costs")
async def query_costs(request):
    """
    Aggregate cost and turn data across all runs.
    Returns costs grouped by run and by agent, sorted descending by cost.
    """
    await mirror_all_runs(request.app)

    run_rows = await fetch_rows(
        request.app,
        """
        SELECT a.run_id, r.slug, SUM(a.cost_usd) as total_cost, SUM(a.turns) as total_turns
        FROM agents a
        LEFT JOIN runs r ON a.run_id = r.run_id
        GROUP BY a.run_id, r.slug
        ORDER BY total_cost DESC
        """,
    )
    by_run = [
        {
            "run_id": row[0],
            "slug": row[1],
            "total_cost_usd": row[2],
            "total_turns": row[3],
        }
        for row in run_rows
    ]

    agent_rows = await fetch_rows(
        request.app,
        """
        SELECT a.name, SUM(a.cost_usd) as total_cost, SUM(a.turns) as total_turns, COUNT(*) as run_count
        FROM agents a
        GROUP BY a.name
        ORDER BY total_cost DESC
        """,
    )
    by_agent = [
        {
            "agent_name": row[0],
            "total_cost_usd": row[1],
            "total_turns": row[2],
            "run_count": row[3],
        }
        for row in agent_rows
    ]

    return sanic_json({"by_run": by_run, "by_agent": by_agent})


@app.get("/api/query/claims/conflicts")
async def query_claim_conflicts(request):
    """
    Return all claim conflicts (rejected or violated) across all runs.
    Shows partition-overlap mistakes for review.
    """
    await mirror_all_runs(request.app)

    rows = await fetch_rows(
        request.app,
        """
        SELECT c.run_id, r.slug, c.path, c.owner, c.status, c.seq
        FROM claims c
        LEFT JOIN runs r ON c.run_id = r.run_id
        WHERE c.status IN ('rejected', 'violated')
        ORDER BY c.run_id, c.seq
        """,
    )
    conflicts = [
        {
            "run_id": row[0],
            "slug": row[1],
            "path": row[2],
            "owner": row[3],
            "status": row[4],
            "seq": row[5],
        }
        for row in rows
    ]
    return sanic_json(conflicts)


if __name__ == "__main__":
    app.run(
        host="127.0.0.1",
        port=int(os.environ.get("CONDUCTION_PORT", "8000")),
        dev=False,
        single_process=True,
    )
