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

## Running and Testing

**Environment**: Conduction's venv (venv/) has sanic, datastar_py, sqlalchemy, aiosqlite. The AgentGraph module (agentgraph/) is imported in-process.

**Test harness** (from shared findings): run the suite via the Projects/.venv (which has pytest and the SDK):
```
cd C:\Users\atooz\Programming\conduction && \
  C:\Users\atooz\Programming\Projects\agentgraph\.venv\Scripts\python.exe -m pytest tests -q
```

The orchestrator's gate, after all agents finish, runs this suite plus HTTP probes on `/api/runs`, `/runs`, `/query`, `/api/query/costs`, `/api/query/claims/conflicts` on a dedicated port.
