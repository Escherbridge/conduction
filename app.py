import os
import sys
import asyncio
import hashlib
import json as stdlib_json
import re
import shutil
import threading
import time
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path
from urllib.parse import unquote
from jinja2 import Environment, FileSystemLoader
from sanic import Sanic
from sanic.response import html, json as sanic_json, raw as _raw, text as raw_text, empty as empty_response
from datastar_py import ServerSentEventGenerator as SSE
from datastar_py.sanic import datastar_response
from agentgraph.sqlite_sink import SqliteMirror
from agentgraph import Mission, AgentSpec
from agentgraph.mission import EDIT_TOOLS, READ_TOOLS
from agentgraph.dispatcher import ScriptedWorker
from agentgraph.gates import gate_from_spec, validate_gate_spec
from agentgraph.sdk_workers import resolve_workers, available_sdks
from agentgraph.manifest import (
    ensure_agentgraph_gitignore,
    manifest_from_request,
    mission_from_manifest,
    read_mission_manifest,
    write_mission_manifest,
)
from agentgraph.narrate import narrate_path
from agentgraph import policy
from agentgraph.factory import (
    FactoryRunner,
    load_factory_spec,
    validate_factory_spec,
)

APP_ROOT = Path(__file__).resolve().parent
# Where ecosystem.json lives; tests point this at a temp dir so they never
# write rules into the real one.
ECOSYSTEM_ROOT = Path(os.environ.get("CONDUCTION_ECOSYSTEM_ROOT") or APP_ROOT).resolve()
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

# Blueprint handlers do `from app import ...` lazily. When app.py is executed
# as a script this module is "__main__", so that import would re-execute the
# file and build a second Sanic instance with the same name; aliasing it here
# makes both names one module object.
sys.modules.setdefault("app", sys.modules[__name__])

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


def effective_policy_rules(target_repo: Path) -> list[dict]:
    """Enabled ecosystem rules first, then the target repo's own -- the binding set."""
    try:
        ecosystem = policy.load_ecosystem(ECOSYSTEM_ROOT)
        project = policy.load_project(target_repo)
        return list(policy.effective_rules(ecosystem, project))
    except Exception as error:  # config problems must not block a launch
        print("Could not resolve rules for %s: %s" % (target_repo, error))
        return []


def apply_policy_rules(agents: list[AgentSpec], rules: list[dict]) -> list[AgentSpec]:
    """Prepend the RULES (binding) block to every brief (idempotent)."""
    if not rules:
        return agents
    return list(policy.apply_rules(agents, rules))


def rules_facts(rules: list[dict]) -> list[tuple]:
    """The single seeded fact carrying the binding rules, or nothing."""
    if not rules:
        return []
    fact = policy.rules_fact(rules)
    return [fact] if fact else []


# --- blueprints ------------------------------------------------------------
# Imported after the helpers above exist: blueprint handlers `from app import`
# them lazily at request time, never at module import.
from routes.config import bp as config_bp  # noqa: E402
from routes.scheduler import start_scheduler  # noqa: E402

app.blueprint(config_bp)

try:  # observe.py is a sibling wave-L deliverable; the app boots without it
    from routes.observe import bp as observe_bp  # noqa: E402

    app.blueprint(observe_bp)
except ImportError as error:  # pragma: no cover - only while observe.py lands
    print("routes.observe unavailable: %s" % error)


@app.after_server_start
async def setup_scheduler(app):
    """Start the 60 s schedule ticker (disabled by CONDUCTION_SCHEDULER=0)."""
    start_scheduler(app)


@app.before_server_start
async def setup_mirror(app):
    """Initialize the SqliteMirror instance and mission process tracker on app.ctx"""
    app.ctx.mirror = SqliteMirror(MIRROR_DB_PATH)
    app.ctx.mirror_lock = asyncio.Lock()
    app.ctx.mission_processes = {}
    app.ctx.factory_runs = {}
    async with app.ctx.mirror_lock:
        await asyncio.to_thread(purge_legacy_run_ids, app)


@app.before_server_stop
async def stop_missions(app):
    """Signal every tracked mission/factory run and give its thread a bounded chance to unwind"""
    tracked = list(app.ctx.mission_processes.values()) + list(
        getattr(app.ctx, "factory_runs", {}).values()
    )
    for entry in tracked:
        entry["stop_event"].set()
    threads = [
        entry["thread"]
        for entry in tracked
        if entry["thread"] is not None and entry["thread"].is_alive()
    ]
    if threads:
        await asyncio.to_thread(join_mission_threads, threads, MISSION_JOIN_TIMEOUT_SECONDS)


environment = Environment(loader=FileSystemLoader(APP_ROOT / "templates"), autoescape=True)


def dry_run_enabled() -> bool:
    return os.environ.get("CONDUCTION_DRY_RUN") == "1"


def render_template(name: str, **context) -> str:
    """Render templates/<name> with the shell context every page needs.

    Templates that contain no Jinja syntax render unchanged, so raw pages keep
    working until the template agents convert them.
    """
    return environment.get_template(name).render(
        active=context.pop("active", None),
        dry_run=dry_run_enabled(),
        **context,
    )


FALLBACK_PAGE = (
    "<!DOCTYPE html><html data-theme=\"dark\"><head><title>%s</title></head>"
    "<body><h1>%s</h1></body></html>"
)


def render_page(name: str, *, title: str, **context) -> str:
    """render_template, but a page whose template has not landed yet still 200s."""
    if not (APP_ROOT / "templates" / name).exists():
        return FALLBACK_PAGE % (title, title)
    return render_template(name, **context)


@app.get("/")
async def home(request):
    """Serves the main app"""
    return html(render_template("index.html", active="dashboard"))


@app.get("/runs")
async def runs_list(request):
    """Serves the runs list view"""
    return html(render_template("runs_list.html", active="runs"))


@app.get("/runs/<run_id:runid>")
async def run_detail(request, run_id: str):
    """Serves the run detail view"""
    return html(render_template("run_detail.html", active="runs", run_id=run_id))


@app.get("/projects")
async def projects_page(request):
    """Registered target repos."""
    return html(render_page("projects.html", title="Projects", active="projects"))


@app.get("/projects/<key:str>")
async def project_detail_page(request, key: str):
    """One project: its rules, goals, schedules and runs."""
    return html(
        render_page(
            "project_detail.html", title="Project", active="projects", repo_key=key
        )
    )


@app.get("/settings")
async def settings_page(request):
    """Ecosystem-wide rules, goals, schedules; SDK availability; dry-run flag."""
    return html(render_page("settings.html", title="Settings", active="settings"))


def read_run_manifests(refs: list[RunRef]) -> dict[str, dict]:
    """run_id -> its mission.json (absent for legacy runs written before Wave K)."""
    manifests: dict[str, dict] = {}
    for ref in refs:
        manifest = read_mission_manifest(ref.run_dir)
        if manifest:
            manifests[ref.run_id] = manifest
    return manifests


@app.get("/api/runs")
async def list_runs(request):
    """List all mission runs, mirroring any new ones found on disk from all known repos"""
    refs = await mirror_all_runs(request.app)
    manifests = await asyncio.to_thread(read_run_manifests, refs)
    rows = await fetch_rows(
        request.app,
        "SELECT run_id, slug, started_at, status, target_repo, gate_passed, agents_failed "
        "FROM runs ORDER BY started_at DESC",
    )
    runs = [
        {
            "run_id": row[0],
            "slug": row[1],
            "started_at": row[2],
            "status": effective_status(request.app, row[0], row[3]),
            "target_repo": row[4],
            "kind": (manifests.get(row[0]) or {}).get("kind", "legacy"),
            "parent_run_id": (manifests.get(row[0]) or {}).get("parent_run_id"),
            "gate_passed": None if row[5] is None else bool(row[5]),
            "agents_failed": row[6],
        }
        for row in rows
    ]
    return sanic_json(runs)


@app.get("/api/runs/<run_id:runid>/agents")
async def get_run_agents(request, run_id: str):
    """Return agents for a specific run, mirroring it from disk first"""
    ref = await asyncio.to_thread(find_run, request.app, run_id)
    if ref is not None:
        await mirror_single_run(request.app, ref)

    run_rows = await fetch_rows(
        request.app,
        "SELECT target_repo FROM runs WHERE run_id = ?",
        (run_id,),
    )
    if ref is None and not run_rows:
        return sanic_json({"error": f"Run not found: {run_id}"}, status=404)
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


@app.get("/api/runs/<run_id:runid>/manifest")
async def get_run_manifest(request, run_id: str):
    """The run's mission.json — what it would take to replay it."""
    ref = await asyncio.to_thread(find_run, request.app, run_id)
    if ref is None:
        return sanic_json({"error": f"Run not found: {run_id}"}, status=404)
    manifest = await asyncio.to_thread(read_mission_manifest, ref.run_dir)
    if manifest is None:
        return sanic_json({"error": f"No manifest for run: {run_id}"}, status=404)
    return sanic_json(manifest)


@app.get("/api/runs/<run_id:runid>/story")
async def get_run_story(request, run_id: str):
    """Markdown narration of run.jsonl, computed on request (never stored)."""
    ref = await asyncio.to_thread(find_run, request.app, run_id)
    if ref is None or not ref.log_path.exists():
        return sanic_json({"error": f"Run not found: {run_id}"}, status=404)
    story = await asyncio.to_thread(narrate_path, str(ref.log_path))
    return raw_text(story, content_type="text/markdown; charset=utf-8")


@app.post("/api/runs/<run_id:runid>/replay")
async def replay_run(request, run_id: str):
    """Re-execute a finished run against its own recording. $0: no worker runs."""
    try:
        ref = await asyncio.to_thread(find_run, request.app, run_id)
        if ref is None or not ref.log_path.exists():
            return sanic_json({"error": f"Run not found: {run_id}"}, status=404)

        reap_mission_threads(request.app)
        if is_managed_and_alive(request.app, run_id):
            return sanic_json({"error": "Run is still running"}, status=409)

        manifest = await asyncio.to_thread(read_mission_manifest, ref.run_dir)
        if manifest is None:
            return sanic_json({"error": "Run has no manifest; cannot replay"}, status=400)

        if active_mission_count(request.app) >= MAX_CONCURRENT_MISSIONS:
            return sanic_json(
                {"error": f"Too many missions running (max {MAX_CONCURRENT_MISSIONS})"},
                status=409,
            )

        target_repo = ref.target_repo
        suffix = f"-replay-{int(time.time())}"
        replay_slug = f"{ref.slug[: 64 - len(suffix)]}{suffix}"
        replay_slug, slug_error = validate_slug(replay_slug)
        if slug_error:
            return sanic_json({"error": f"generated replay slug rejected: {slug_error}"}, status=400)

        replay_dir = (target_repo / ".agentgraph" / "runs" / replay_slug).resolve()
        if not replay_dir.is_relative_to(target_repo):
            return sanic_json({"error": "resolved run directory escapes target_repo"}, status=400)
        new_log_path = replay_dir / "run.jsonl"
        if new_log_path.exists():
            return sanic_json({"error": f"A replay run already exists for '{replay_slug}'"}, status=409)

        await asyncio.to_thread(prepare_run_directory, replay_dir)

        replay_manifest = dict(manifest)
        replay_manifest = manifest_from_request(
            slug=replay_slug,
            agents=manifest.get("agents", []),
            synthesis=manifest.get("synthesis"),
            gate=manifest.get("gate"),
            model=manifest.get("model", DEFAULT_MISSION_MODEL),
            max_turns=manifest.get("max_turns", 30),
            max_concurrency=manifest.get("max_concurrency", 4),
            target_repo=manifest.get("target_repo", str(target_repo)),
            kind="replay",
            parent_run_id=run_id,
            facts=manifest.get("facts") or [],
        )
        await asyncio.to_thread(write_mission_manifest, replay_dir, replay_manifest)

        # The rebuilt mission must keep the *original* slug: request identity
        # hashes over it, and a renamed mission is a guaranteed cache miss.
        rebuild_manifest = dict(replay_manifest)
        rebuild_manifest["slug"] = manifest.get("slug", ref.slug)
        mission = await asyncio.to_thread(
            mission_from_manifest, rebuild_manifest, run_dir=replay_dir
        )

        replay_run_id = compose_run_id(replay_slug, target_repo)
        original_log_path = ref.log_path
        stop_event = threading.Event()
        entry = {
            "thread": None,
            "slug": replay_slug,
            "run_dir": replay_dir,
            "log_path": new_log_path,
            "stop_event": stop_event,
            "target_repo": str(target_repo),
            "status": "running",
            "error": None,
        }

        def replay_mission_thread():
            try:
                mission.replay(str(original_log_path), str(new_log_path))
            except Exception as error:
                entry["error"] = str(error)
                print(f"Replay {replay_run_id} errored: {error}")
            finally:
                entry["status"] = "finished"

        thread = threading.Thread(target=replay_mission_thread, name=f"replay-{replay_run_id}")
        entry["thread"] = thread
        request.app.ctx.mission_processes[replay_run_id] = entry
        thread.start()

        return sanic_json(
            {
                "run_id": replay_run_id,
                "slug": replay_slug,
                "parent_run_id": run_id,
                "status": "replaying",
                "log_path": str(new_log_path),
                "target_repo": str(target_repo),
            }
        )
    except Exception as error:
        return sanic_json({"error": str(error)}, status=500)


@app.delete("/api/runs/<run_id:runid>")
async def delete_run(request, run_id: str):
    """Remove a finished run: its directory on disk and its mirror rows."""
    ref = await asyncio.to_thread(find_run, request.app, run_id)
    if ref is None:
        return sanic_json({"error": f"Run not found: {run_id}"}, status=404)

    reap_mission_threads(request.app)
    if is_managed_and_alive(request.app, run_id):
        return sanic_json({"error": "Run is still running"}, status=409)

    await asyncio.to_thread(shutil.rmtree, ref.run_dir, True)
    async with request.app.ctx.mirror_lock:
        await asyncio.to_thread(request.app.ctx.mirror.delete_run, run_id)
    request.app.ctx.mission_processes.pop(run_id, None)
    return empty_response(status=204)


@app.get("/api/ping")
@datastar_response
async def ping(request):
    """grab sse for datastar"""
    fragment = '<div>Hello! Welcome to the D A T A S T A R </div>'
    yield SSE.patch_elements(fragment)


def prepare_run_directory(run_dir: Path) -> None:
    run_dir.mkdir(parents=True, exist_ok=True)
    (run_dir / "transcripts").mkdir(exist_ok=True)
    ensure_agentgraph_gitignore(run_dir.parent.parent.parent)


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

    # cost_usd=0: a dry run spends nothing, and the recorded zero is what a
    # later $0 replay re-serves from the log.
    return ScriptedWorker(
        dry_run_responder,
        delays={name: DRY_RUN_AGENT_SECONDS for name in agent_names},
        cost_usd=Decimal("0"),
    )


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


def manifest_agents(agents: list[AgentSpec]) -> list[dict]:
    """AgentSpec list -> the manifest's agent dicts (sdk defaults to "claude")."""
    return [
        {
            "name": spec.name,
            "brief": spec.brief,
            "tools": list(spec.tools),
            "sdk": spec.meta.get("sdk") or "claude",
            "owns": list(spec.owns),
        }
        for spec in agents
    ]


def write_run_manifest(
    run_dir: Path,
    *,
    slug: str,
    agents: list[AgentSpec],
    synthesis,
    gate_spec,
    model: str,
    max_turns,
    max_concurrency,
    target_repo: Path,
    kind: str,
    parent_run_id: str | None = None,
    facts: list[tuple] = (),
) -> dict:
    """Build and persist mission.json so the run can replay/narrate itself later."""
    manifest = manifest_from_request(
        slug=slug,
        agents=manifest_agents(agents),
        synthesis=synthesis,
        gate=gate_spec if isinstance(gate_spec, dict) else None,
        model=model,
        max_turns=max_turns,
        max_concurrency=max_concurrency,
        target_repo=str(target_repo),
        kind=kind,
        parent_run_id=parent_run_id,
        facts=facts,
    )
    write_mission_manifest(run_dir, manifest)
    return manifest


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
    return html(render_template("launch.html", active="launch"))


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

        rules = effective_policy_rules(target_repo)
        agents = apply_policy_rules(build_agent_specs(agent_data), rules)

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
            facts=rules_facts(rules),
        )

        worker, worker_error = resolve_mission_worker(run_dir, agents)
        if worker_error:
            return sanic_json({"error": worker_error}, status=400)

        await asyncio.to_thread(
            write_run_manifest,
            run_dir,
            slug=slug,
            agents=agents,
            synthesis=synthesis,
            gate_spec=gate_spec,
            model=DEFAULT_MISSION_MODEL,
            max_turns=max_turns,
            max_concurrency=max_concurrency,
            target_repo=target_repo,
            kind="mission",
            facts=rules_facts(rules),
        )

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
        rules = effective_policy_rules(target_repo)
        merged_agents = apply_policy_rules(merged_agents, rules)
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
            facts=rules_facts(rules),
        )

        worker, worker_error = resolve_mission_worker(resume_dir, merged_agents)
        if worker_error:
            return sanic_json({"error": worker_error}, status=400)

        resume_run_id = compose_run_id(resume_slug, target_repo)
        original_log_path = ref.log_path

        await asyncio.to_thread(
            write_run_manifest,
            resume_dir,
            slug=resume_slug,
            agents=merged_agents,
            synthesis=synthesis,
            gate_spec=body.get("gate"),
            model=DEFAULT_MISSION_MODEL,
            max_turns=max_turns,
            max_concurrency=max_concurrency,
            target_repo=target_repo,
            kind="resume",
            parent_run_id=run_id,
            facts=rules_facts(rules),
        )

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


# ---------------------------------------------------------------------------
# Factory: an ordered list of waves, each wave one gated Mission.
# See agentgraph/factory.py for the runtime; this module only exposes it.
# ---------------------------------------------------------------------------

DEFAULT_FACTORY_PATH = ".agentgraph/factory.json"


def factory_spec_path(target_repo: Path, factory_path: object) -> tuple[Path | None, str | None]:
    """Resolve the repo-relative factory.json path inside target_repo."""
    raw = factory_path if isinstance(factory_path, str) and factory_path.strip() else DEFAULT_FACTORY_PATH
    candidate = Path(raw)
    if candidate.is_absolute():
        return None, "factory_path must be relative to target_repo"
    resolved = (target_repo / candidate).resolve()
    if not resolved.is_relative_to(target_repo):
        return None, "factory_path escapes target_repo"
    return resolved, None


def load_spec_or_errors(spec_path: Path):
    """(spec, errors) -- never raises; every validation problem is listed."""
    if not spec_path.exists():
        return None, ["factory spec not found: %s" % spec_path]
    try:
        data = stdlib_json.loads(spec_path.read_text(encoding="utf-8"))
    except (ValueError, OSError) as error:
        return None, ["factory spec is not readable JSON: %s" % error]
    if not isinstance(data, dict):
        return None, ["factory spec must be a JSON object"]
    errors = validate_factory_spec(data)
    if errors:
        return None, list(errors)
    try:
        return load_factory_spec(spec_path), []
    except Exception as error:
        return None, [str(error)]


def factory_state_paths() -> list[Path]:
    paths: list[Path] = []
    for repo in repos_to_scan():
        runs_dir = repo / ".agentgraph" / "factory-runs"
        if not runs_dir.is_dir():
            continue
        paths.extend(sorted(runs_dir.glob("*/state.json")))
    return paths


def read_factory_state(state_path: Path) -> dict | None:
    try:
        state = stdlib_json.loads(state_path.read_text(encoding="utf-8"))
    except (ValueError, OSError):
        return None
    return state if isinstance(state, dict) else None


def find_factory_state(factory_run_id: str):
    for state_path in factory_state_paths():
        if state_path.parent.name != factory_run_id:
            continue
        state = read_factory_state(state_path)
        if state is not None:
            return state_path, state
    return None


def factory_worker_factory(run_dir: Path, specs):
    """CONDUCTION_DRY_RUN=1 -> ScriptedWorker; otherwise let the runner resolve SDKs."""
    if os.environ.get("CONDUCTION_DRY_RUN") == "1":
        return create_dry_run_worker(run_dir, [spec.name for spec in specs])
    return None


def start_factory_thread(
    app,
    spec,
    target_repo: Path,
    factory_run_id: str,
    start_wave: int,
    rules: list[dict] | None = None,
) -> dict:
    """Bounded-thread + stop_event launch, mirroring launch_mission."""
    stop_event = threading.Event()
    entry = {
        "thread": None,
        "stop_event": stop_event,
        "factory_slug": spec.slug,
        "target_repo": str(target_repo),
        "status": "running",
    }
    runner = FactoryRunner(
        spec,
        target_repo,
        factory_run_id=factory_run_id,
        worker_factory=factory_worker_factory,
        model=DEFAULT_MISSION_MODEL,
        stop_when=stop_event.is_set,
        rules=list(rules or ()),
    )

    def run_factory_thread():
        try:
            runner.run(start_wave=start_wave)
        except Exception as error:
            print("Factory %s errored: %s" % (factory_run_id, error))
        finally:
            entry["status"] = "finished"

    thread = threading.Thread(target=run_factory_thread, name="factory-%s" % factory_run_id)
    entry["thread"] = thread
    app.ctx.factory_runs[factory_run_id] = entry
    thread.start()
    return entry


def active_factory_count(app) -> int:
    for entry in app.ctx.factory_runs.values():
        if entry["status"] == "running" and not entry["thread"].is_alive():
            entry["status"] = "finished"
    return sum(1 for entry in app.ctx.factory_runs.values() if entry["status"] == "running")


@app.get("/factory")
async def factory_page(request):
    """Serves the factory pipeline view"""
    template_path = APP_ROOT / "templates" / "factory.html"
    if not template_path.exists():
        return html("<!doctype html><html><body><h1>Factory</h1></body></html>")
    return html(render_template("factory.html", active="factory"))


@app.get("/api/factory/spec")
async def get_factory_spec(request):
    """The parsed spec for the UI editor, or the validation errors."""
    target_repo, repo_error = validate_target_repo(request.args.get("target_repo"))
    if repo_error:
        return sanic_json({"error": repo_error}, status=400)
    spec_path, path_error = factory_spec_path(target_repo, request.args.get("factory_path"))
    if path_error:
        return sanic_json({"error": path_error}, status=400)
    spec, errors = await asyncio.to_thread(load_spec_or_errors, spec_path)
    if errors:
        return sanic_json({"errors": errors, "spec_path": str(spec_path)}, status=400)
    return sanic_json(
        {
            "slug": spec.slug,
            "description": spec.description,
            "spec_path": str(spec_path),
            "waves": [
                {
                    "slug": wave.slug,
                    "agents": wave.agents,
                    "gate": wave.gate,
                    "synthesis": wave.synthesis,
                    "max_turns": wave.max_turns,
                    "max_concurrency": wave.max_concurrency,
                }
                for wave in spec.waves
            ],
        }
    )


@app.post("/api/factory/runs")
async def launch_factory(request):
    """Launch a factory run: waves run sequentially, halting on the first failed gate."""
    try:
        body = request.json or {}
        target_repo, repo_error = validate_target_repo(body.get("target_repo"))
        if repo_error:
            return sanic_json({"error": repo_error}, status=400)

        spec_path, path_error = factory_spec_path(target_repo, body.get("factory_path"))
        if path_error:
            return sanic_json({"error": path_error}, status=400)

        spec, errors = await asyncio.to_thread(load_spec_or_errors, spec_path)
        if errors:
            return sanic_json({"errors": errors}, status=400)

        if active_factory_count(request.app) >= MAX_CONCURRENT_MISSIONS:
            return sanic_json(
                {"error": "Too many factory runs running (max %d)" % MAX_CONCURRENT_MISSIONS},
                status=409,
            )

        await asyncio.to_thread(remember_repo, target_repo)

        factory_run_id = "%s-%d" % (spec.slug, int(time.time()))
        state_path = target_repo / ".agentgraph" / "factory-runs" / factory_run_id / "state.json"
        start_factory_thread(
            request.app,
            spec,
            target_repo,
            factory_run_id,
            0,
            rules=effective_policy_rules(target_repo),
        )

        return sanic_json(
            {
                "factory_run_id": factory_run_id,
                "target_repo": str(target_repo),
                "waves": [wave.slug for wave in spec.waves],
                "state_path": str(state_path),
            }
        )
    except Exception as error:
        return sanic_json({"error": str(error)}, status=500)


@app.get("/api/factory/runs")
async def list_factory_runs(request):
    """Every factory run state.json across all known repos."""

    def collect() -> list[dict]:
        items = []
        for state_path in factory_state_paths():
            state = read_factory_state(state_path)
            if state is None:
                continue
            item = dict(state)
            item.setdefault("target_repo", str(state_path.parents[3]))
            item["state_path"] = str(state_path)
            items.append(item)
        return items

    return sanic_json(await asyncio.to_thread(collect))


@app.get("/api/factory/runs/<factory_run_id:runid>")
async def get_factory_run(request, factory_run_id: str):
    """The state.json for one factory run (the source of truth on disk)."""
    found = await asyncio.to_thread(find_factory_state, factory_run_id)
    if found is None:
        return sanic_json({"error": "Factory run not found: %s" % factory_run_id}, status=404)
    state_path, state = found
    state = dict(state)
    state["state_path"] = str(state_path)
    return sanic_json(state)


@app.post("/api/factory/runs/<factory_run_id:runid>/interrupt")
async def interrupt_factory(request, factory_run_id: str):
    """Signal a running factory to stop after the in-flight wave."""
    active_factory_count(request.app)
    entry = request.app.ctx.factory_runs.get(factory_run_id)
    if not entry:
        return sanic_json(
            {"error": "Factory run not found or not launched by this server"}, status=404
        )
    if not entry["thread"].is_alive():
        return sanic_json(
            {"error": "Factory thread is no longer alive", "status": entry["status"]},
            status=409,
        )
    entry["stop_event"].set()
    return sanic_json(
        {"factory_run_id": factory_run_id, "will_stop_after": "current wave"}, status=202
    )


@app.post("/api/factory/runs/<factory_run_id:runid>/resume")
async def resume_factory(request, factory_run_id: str):
    """Re-run the factory from its first non-passed wave, under the same factory_run_id."""
    try:
        active_factory_count(request.app)
        entry = request.app.ctx.factory_runs.get(factory_run_id)
        if entry and entry["thread"].is_alive():
            return sanic_json({"error": "Factory run is still running"}, status=409)

        found = await asyncio.to_thread(find_factory_state, factory_run_id)
        if found is None:
            return sanic_json({"error": "Factory run not found: %s" % factory_run_id}, status=404)
        _, state = found

        target_repo, repo_error = validate_target_repo(state.get("target_repo"))
        if repo_error:
            return sanic_json({"error": repo_error}, status=400)

        body = request.json or {}
        spec_path, path_error = factory_spec_path(target_repo, body.get("factory_path"))
        if path_error:
            return sanic_json({"error": path_error}, status=400)
        spec, errors = await asyncio.to_thread(load_spec_or_errors, spec_path)
        if errors:
            return sanic_json({"errors": errors}, status=400)

        waves = state.get("waves") or []
        start_wave = len(spec.waves) - 1
        for index, wave in enumerate(waves):
            if wave.get("status") != "passed":
                start_wave = index
                break
        start_wave = max(0, min(start_wave, len(spec.waves) - 1))

        start_factory_thread(
            request.app,
            spec,
            target_repo,
            factory_run_id,
            start_wave,
            rules=effective_policy_rules(target_repo),
        )
        return sanic_json(
            {
                "factory_run_id": factory_run_id,
                "target_repo": str(target_repo),
                "start_wave": start_wave,
                "waves": [wave.slug for wave in spec.waves],
            }
        )
    except Exception as error:
        return sanic_json({"error": str(error)}, status=500)


# Cross-run query endpoints
@app.get("/query")
async def query_page(request):
    """Serves the cross-run query UI"""
    return html(render_template("query.html", active="query"))


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
