# Conduction Architecture

Conduction is a local-first, multi-repo UI for running Agent missions. It hosts a Sanic web server (app.py:35) that orchestrates missions against a target repository's `.agentgraph/` directory and provides a queryable SQLite mirror of run status for the UI.

## What Conduction Is

A **mission** is a declarative program: agents, tools, briefs, and a gate (agentgraph/mission.py:1–24). Missions run inside the AgentGraph runtime (in-process) and write their logs to the target repo's `.agentgraph/runs/<slug>/` directory. Conduction mirrors the JSONL log into SQLite and serves it over HTTP with Server-Sent Events, providing a live read of run progress and findings.

The app does **not** execute missions itself; missions are launched by external code (an orchestrator, a test harness, the agent SDK itself) and write their logs directly. Conduction observes and projects.

## Run Placement and Identity

**Run directory structure** (app.py:48–50):
- Missions write to `<target_repo>/.agentgraph/runs/<slug>/` (JSONL log, replay state).
- `.agentgraph/.gitignore` = `"*"` — the directory is excluded from version control.
- `<target_repo>/.agentgraph/known_repos.json` lists repos Conduction has seen (app.py:20).

**Run identity** (app.py:57–58): `MISSION-{slug}@{repo_key(target_repo)}`
- `repo_key` is the first 8 chars of sha1(resolved + lowercased repo path) (app.py:53–54).
- The `@<sha1>` suffix disambiguates runs with the same slug on different repos.
- A run this process did **not launch**, whose log has not grown for >10 min and never recorded `mission.completed`, is marked "stale" so SSE streams can end instead of polling the 2-hour cap (app.py:28–31).

## The Mirror: JSONL → SQLite

**Source of truth**: the JSONL log written by the mission (log.py:1–12). One line per event: `{"seq": n, "run_id": ..., "event": {...}}`, synchronously flushed per event.

**Projection**: SqliteMirror (sqlite_sink.py:17–24) reads the JSONL and indexes into SQLite (WAL mode) for fast queries. Tables: `runs`, `agents`, `findings`, `claims` — rebuilt on demand, not authoritative. If the SQLite database is lost or stale, it is rebuilt from the JSONL on the next mirror call (sqlite_sink.py reads log file byte offsets to support incremental mirroring).

**Status stale**: a run reported as "stale" (app.py:29–31) when Conduction itself did not launch it, it never wrote `mission.completed`, and its log has not grown for `STALE_RUN_SECONDS` (app.py:32). This prevents SSE streams from polling forever.

## SSE Contract

**Event stream** (app.py:/api/runs/{run_id}): `event: run-event` carrying `{"type": "...", "payload": {...}}` (the raw event from the log) and `event: run-complete` with the final `mission.completed` payload.

Conduction does **not** stream Datastar fragments because:
- Fragments are UI-specific; the event envelope is data.
- The UI can project fragments client-side without forcing server-side coupling.
- A raw JSON event survives replay, testing, and log inspection.

## Steering and Interruption

**stop_when**: a user request arrives as HTTP POST to `/api/runs/{run_id}/control/stop` and writes an `interrupt` signal to the run directory. The mission's host loop checks this marker (host.py: check before each `run_quantum`) and gracefully shuts down.

The signal is a **durable file marker**, not an in-memory flag, so it survives if Conduction restarts between request and pickup.

**CONDUCTION_DRY_RUN**: when set, the Dispatcher uses `ScriptedWorker` instead of `ClaudeAgentWorker` (dispatcher.py:12). Responses come from a deterministic script file instead of the Claude API, enabling full integration testing without API cost.

## Security Posture

- **Bind address**: 127.0.0.1 only (app.py). Remote access requires a proxy.
- **Target repo allow-list**: `CONDUCTION_ALLOWED_ROOTS` env var (app.py:83–96). Defaults to `$HOME` if unset. Prevents missions on arbitrary paths.
- **Slug validation** (app.py:23–24): `^[A-Za-z0-9._-]{1,64}$` enforces well-formed run IDs.
- **Relaunch protection**: attempt to launch a run that is already in progress returns 409 (Conflict).

## Manifests and Replay

A **mission manifest** (`mission.json`) is written by app.py and factory.py into each run's directory before the mission starts. It is the source of truth for replaying a run without re-calling the API (app.py: `write_mission_manifest`, `manifest_from_request`). The manifest contains the exact agent specs, tools, model, gate, and other invariants needed to recompute a run bit-identically (agentgraph/manifest.py: schema version 1, kind: "mission" | "resume" | "replay" | "factory-wave").

**Replay** (`POST /api/runs/<id>/replay` — not yet implemented) uses the manifest to rerun a mission deterministically. A replay run is $0 cost (uses cached agent responses per `(identity_hash, occurrence)`). The replay is served from the original log via `Mission.replay()` with no worker calls (agentgraph/manifest.py: `mission_from_manifest`, replay raises `ReplayCacheMiss` if specs don't hash to the same identity as the original).

**Run story** (`GET /api/runs/<id>/story` — not yet implemented) narrates the run from the JSONL log as escaped markdown (file:line references, headings, list items). Uses `narrate_path(run.jsonl)` in a bounded thread to avoid blocking the event loop.

## Factory Runs

**Factory spec** is versioned as `<target_repo>/.agentgraph/factory.json`. Each entry is a `FactoryWave` with agents, gate, synthesis, and limits (agentgraph/factory.py: `FactoryWave`, `load_factory_spec`). `FactoryRunner` executes waves in order; each wave is one gated `Mission` (agentgraph/factory.py:280–303). State is persisted to `<factory_run_id>/state.json` and is resumable (agentgraph/factory.py: `FactoryRunState`).

**factory-wave manifests** are written by `FactoryRunner` before each wave runs (agentgraph/factory.py: `_run_wave`) so waves are separately replayable.

## Running and Testing

**Environment**: any Python 3.12 with `requirements.txt` + `requirements-dev.txt` installed. The AgentGraph module (`agentgraph/`) is a
vendored copy imported in-process; `tests/conftest.py` puts the repo root on
`sys.path` so it always resolves to the copy here rather than another checkout.

**Test harness**:
```
python -m pip install -r requirements.txt -r requirements-dev.txt
python -m pytest tests -q          # ~257 tests, ~2m40s
ruff check . && ruff format --check .
```

The orchestrator's gate, after all agents finish, runs this suite plus HTTP probes on `/api/runs`, `/runs`, `/query`, `/api/query/costs`, `/api/query/claims/conflicts` on a dedicated port.

## Policy Data (Wave L)

**Ecosystem scope** (`<conduction>/.agentgraph/ecosystem.json`): Global rules, goals, and schedules. Loaded by app.py on start.

**Project scope** (`<target_repo>/.agentgraph/project.json`): Per-repo rules, goals, and schedules. Versioned in the target repo (manifest.AGENTGRAPH_GITIGNORE gains `!project.json` and `!ecosystem.json`).

**Schema v1** (policy.py:1–40):
- `Rule`: {id, text, scope ("all"|"writers"|"readers"), enabled}
- `Goal`: {id, title, description, status ("open"|"done"|"blocked"), linked_runs, updated_at}
- `Schedule`: {id, kind ("factory"|"mission"), factory_path, every ("30m"|"6h"|"1d")|null, cron, enabled, last_run_at, last_factory_run_id, target_repo (ecosystem only)}

**Policy API** (policy.py: `load_ecosystem`, `save_ecosystem`, `load_project`, `save_project`, `validate_ecosystem`, `validate_project`, `effective_rules`, `rules_block`, `apply_rules`, `rules_fact`, `next_due`, `due_schedules`). Atomic writes via temp+replace. `effective_rules(ecosystem, project) → list[Rule]` (enabled, ecosystem first). `rules_block(rules, *, writer: bool) → str` renders a "RULES (binding)" section for agent briefs. `apply_rules(specs, rules)` prepends binding rules to each brief (idempotent marker: "RULES (binding)"); writers = specs with Edit/Write tools.

## Routes and Blueprints (Wave L)

**Sanic blueprints** (routes/): Each blueprint registers via app.py; reaches shared state ONLY via `request.app.ctx` (mirror, mirror_lock, mission_processes, factory_processes). No module-level imports of app — lazy import inside handlers to avoid circular dependencies.

**routes/config.py** (bp "config"): Ecosystem config (GET/PUT /api/ecosystem), project registry (GET/POST /api/projects), project detail (GET/PUT /api/projects/<repo_key>), schedules (GET /api/schedules, POST /api/schedules/<id>/run-now), merged rules (GET /api/rules/effective?target_repo=...).

**routes/observe.py** (bp "observe"): Fleet observability (GET /api/observe/summary → KPIs), live agents (GET /api/observe/agents → [{run_id, slug, target_repo, agent, model, status, turns, cost_usd, last_finding, last_event_ts}]), activity feed (GET /api/observe/feed → Server-Sent Events), timeline (GET /api/observe/timeline?limit=200 → recent activity rows).

**routes/scheduler.py**: `start_scheduler(app)` registered on `after_server_start`. Async task fires every 60 s, computes `due_schedules(ecosystem, projects, now) → list[(schedule, target_repo)]`, launches factory runs (honoring CONDUCTION_DRY_RUN), updates `last_run_at` and `last_factory_run_id` in the owning doc. Skips if that repo already has a running factory. Sets `app.ctx.scheduler_state = {last_tick, launched}`. Disabled when env `CONDUCTION_SCHEDULER=0` (tests set this unless testing the scheduler).

## Templating and Design System (Wave L)

**Jinja2 setup** (app.py: `render_template`): Environment with FileSystemLoader(templates), autoescape=True.

**templates/base.html**: Root shell. Every page `{% extends "base.html" %}` with blocks `title`, `content`, `scripts`. Sets `<html data-theme="dark">`, includes `/static/css/styles.css` and `/static/js/app.js`, marks active nav item via `{{ active }}`.

**Static design tokens** (static/css/styles.css, rewritten by shell agent; no other agent adds to it): CSS custom properties on `:root` (dark) and `[data-theme="light"]`: `--bg`, `--surface`, `--surface-2`, `--border`, `--text`, `--text-muted`, `--accent`, `--ok`, `--warn`, `--danger`, `--info`, `--mono`, `--radius`. Component classes: `.app-shell`, `.sidebar`, `.topbar`, `.page`, `.card`, `.kpi`, `.pill` (with status variants), `.table`, `.form-grid`, `.field`, `.btn`, `.tabs`, `.feed`, `.empty-state`, `.badge`, `.mono`. Status colors: running=warn(amber), completed/passed=ok(green), failed/errored=danger(red), stale/pending=muted, replay/info=info(blue), skipped=muted. Grep existing selectors (`swim-lane`, `timeline-event.*`, `mission-completion-pill`, `run-status.*`, etc.) before rewriting and keep or alias them.

**Shared JS** (static/js/app.js): `window.Conduction = { escapeHtml, fetchJson(url, opts), openStream(url, handlers), pill(status), fmtCost(n), fmtAgo(iso), encodeId(id) }`.

## Observability and Information Architecture (Wave L)

**Dashboard** (/): Fleet KPIs, live agents across all runs, activity feed, goals, upcoming schedules.

**Runs** (/runs): Every run from every project. Detail at /runs/<id> with swim lanes, story, manifest views.

**Factory** (/factory): Factory runs and pipeline view; launch a factory.

**Launch** (/launch): Launch a single mission.

**Projects** (/projects): Registered target repos. /projects/<repo_key> shows rules, goals, schedules, runs.

**Query** (/query): Cross-run trace queries.

**Settings** (/settings): Ecosystem-wide rules, goals, schedules; SDK availability; dry-run flag.

**Top bar**: Page title, "dry-run mode" badge (when CONDUCTION_DRY_RUN=1), "N agents live" counter (polls /api/observe/summary every 5 s), theme toggle (dark default, light tokens).

## Install and Launch Scripts (Wave L)

**install.ps1 / install.sh** (repo root): Create ./venv if missing (prefer `uv venv` + `uv pip install`, fall back to `python -m venv + pip`). Install requirements.txt + requirements-dev.txt. Verify `import app`. Print next steps. Idempotent.

**conduction.ps1 / conduction.cmd / conduction.sh**: If http://$HOST:$PORT/api/ping already answers → open browser. Else start `venv\Scripts\python app.py` detached with logs in `.agentgraph/app.log` (previous run rotated to `app.log.prev`; PowerShell also writes `app.log.err`). Wait for /api/ping, polling every 500 ms, and bail early if the child exits. Defaults, each settable by flag or env var: port 8000 (`--port`/`-Port`, CONDUCTION_PORT), host 127.0.0.1 (`--host`/`-BindHost`, CONDUCTION_HOST), timeout 30 s (`--timeout`/`-TimeoutSeconds`, CONDUCTION_START_TIMEOUT), browser on (`--no-browser`/`-NoBrowser`, CONDUCTION_NO_BROWSER=1). `--stop`/`-Stop` kills the recorded PID; a stale PID file is cleared on start so `--stop` never targets a recycled PID. Never kills by name. Note: Start-Process has no -Environment parameter on PS 5.1 — the launcher exports CONDUCTION_PORT/HOST into its own environment so the child inherits them.

**scripts/create-shortcut.ps1**: Creates "Conduction.lnk" on Desktop and Start Menu Programs, targeting `powershell.exe -NoProfile -ExecutionPolicy Bypass -File conduction.ps1`, working dir = repo root, icon from shell32.dll. `-Remove` to delete. install.ps1 offers to run it (`-Shortcut` switch runs non-interactively).

**README.md**: 60-second quickstart (install, launch, shortcut), each page's role, data locations (.agentgraph/, mirror db), env vars (CONDUCTION_PORT, CONDUCTION_DRY_RUN, CONDUCTION_ALLOWED_ROOTS, CONDUCTION_SCHEDULER), how to run tests.
