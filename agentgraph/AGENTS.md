# AgentGraph Runtime Architecture

AgentGraph is a deterministic, single-threaded event-driven executor for multi-agent missions. The runtime is a thin shim (mission.py) over a substrate: Host, Dispatcher, ClaimLedger, AgentCache, ReplayPlan, and JSONLLog. Every concept is justified by a load-bearing invariant.

## Layering

**Mission shim** (mission.py:1–24): Declares agents and briefs; emits seeded findings (references) so they live on the board instead of in prompts; wires up the gate and synthesis; owns the system prompt template.

**Host loop** (host.py:1–34): Single-threaded, synchronous, drains a bounded event quantum before injecting results so the graph reaches quiescence at every injection point. Three invariants make this work:
1. asyncio (not threads) — one writer, no interleave risk.
2. Occurrence indices assigned at request time — stable no matter worker completion order.
3. Drained-to-quiescence before injection — logs are independent of worker timing.

**Dispatcher** (dispatcher.py:1–13): Worker pool (`ClaudeAgentWorker` or `ScriptedWorker` for testing). Runs concurrently outside the loop; the host hands it requests and injects only results. `Worker` is a protocol so substitution is trivial.

**ClaimLedger** (claims.py:1–10): Event-sourced map of file ownership. Claims are events in the log; the owner map is a read-only projection. The `PreToolUse` hook (claims.py) is the only enforcement — workers do not consult it voluntarily.

**AgentCache** (agentcache.py:1–14): Content-addressed store of agent responses, keyed by `(identity_hash, nth-occurrence)`. The occurrence index keeps two identical prompts distinct (ToolCache collapses them, wrong for agents where identical work costs twice).

**ReplayPlan** (replay.py:1–22): The recorded injection interleaving, grouped into the quantum windows it shared. Maximal runs of injected events between quanta form one window; replaying window by window reproduces the interleaving by construction.

**JSONLLog** (log.py:1–12): Synchronous append-only writer, flushed per event. Not `activegraph.sinks.JSONLEventSink` — the sink has a background worker with a bounded queue, decorative under needs-to-not-drop-events logic. This log is the source of truth for replay.

## Determinism Invariants

**Single-writer host loop** (host.py:6–14): `run_quantum()` is synchronous and cannot interleave with an injection. Worker MCP callbacks only run at `await` points on the host's event loop. The graph has exactly one writer.

**Occurrence indices at request time** (host.py:16–19): When an agent is requested, its occurrence index in the call sequence is assigned on the single-threaded loop. Two identical calls from different requests have different occurrences, so `(identity_hash, occurrence)` is a unique cache key.

**FrozenClock** (mission.py): The graph is built with a `FrozenClock` so wall-time does not vary between runs. References and facts must be deterministically computed from inputs, not from `Date.now()` or randomness.

**Identity hash vs `meta`** (dispatcher.py:74–115): Every field that changes the answer (prompt, model, system prompt, tool set) is folded into `identity()` (dispatcher.py:AgentRequest). Fields that change behavior but the model never sees (permission hooks, config fingerprints, routing metadata like `owns`) live in `meta` instead. The hash does **not** include `meta`, so a partition edit does not invalidate cached responses.

**config_fingerprint "v2-hardened"** (dispatcher.py:37): Stamped into every identity hash. When host-side policy changes (hook sets, tool binding, permission rules), bump this. Old logs fail replay loud instead of silently passing under a different policy.

## Sandbox

**disallowed_tools complement** (dispatcher.py:40–71): `allowed_tools` is only an auto-approve list. Under `bypassPermissions`, unlisted built-in tools are still reachable (a read-only worker once ran `Stop-Process` this way). `denied_tools()` turns a tool set into a boundary by naming the **complement** over `CLAUDE_TOOL_NAMES`. MCP tools (`mcp__*`) are not in this list — graph access is granted by `mcp_server_names`.

**PreToolUse hook and destructive-command rules** (claims.py; host.py writes the hook). The hook intercepts every tool call and enforces two rules:
1. **Claim validation**: writing tools (Write, Edit, NotebookEdit, MultiEdit) require a granted claim. A phantom claim incident: an MSYS-style path `/c/x` resolves to `C:/c/x` on Windows, so a claim on `/c/x` is granted and every later Edit on `C:/x` is denied (claims.py:30–33, rejection reason in claims.py:82–88).
2. **Command policy**: Bash and PowerShell bypass claims entirely but are subject to pattern matching. Two incidents motivated this: a shell command once deleted production data, and a worker wrote a file it didn't claim by shelling out.

**owns + claim validation**: An agent spec carries `owns: tuple[str, ...]` (mission.py:120) — the paths it is partitioned to claim. The claim hook checks this against the agent's `meta.owns` (dispatcher.py:100–118, claim_rejection_reason in claims.py:97–104). Root/parent resolution uses `_resolved()` to avoid MSYS phantom paths.

## Gate

The gate is host-side code (a callable) that runs between the join (all agents finish) and synthesis. It is dispatched as a pseudo-agent named `host` so its finding is attributed to the host (mission.py:56). The verdict is a `GateResult` (mission.py:146–150) with a `passed: bool` and a `checks: list`.

Replaying a gate does **not** recompute it; the verdict is served from the log cache, keyed by request event ID. This ensures replay is bit-identical: a gate that makes non-deterministic choices (e.g., probes external services) cannot be re-run.

## Result Contract

When a mission finishes, it emits `mission.completed` (events.py:34) with payload:
- `agents_total`: count of agents in the spec.
- `agents_failed`: count of agents that errored.
- `gate_passed`: boolean, only when a gate ran.
- `status`: one of ("completed", "failed", "errored", "stale").

The app treats this as the definitive mission outcome. A run without `mission.completed` is reported as "stale" if Conduction did not launch it and its log has not grown for >10 min.

## Event Classification for Replay

**INJECTED_TYPES** (replay.py:43–62): Events emitted **between quanta** by the host or a hook, which must be served from the recording to avoid window shifting:
- `agent.responded`, `finding.recorded`, `claim.*`, `command.violated` — all host/hook injections.

**Behavior/mission code events** (replay.py:48–51): `mission.completed` is emitted by behavior code during a quantum, so replay re-runs that code and would emit it twice if replayed. Never in INJECTED_TYPES.

**Rule**: emitted between quanta by hook or host → injected, or the window shifts. Produced by behavior or mission → not injected, because replay re-runs that code.

## Worker Protocol and SDK Routing

**Worker**: a protocol (dispatcher.py:9) with `run()` async method. `ClaudeAgentWorker` calls the Claude SDK; `ScriptedWorker` reads deterministic scripts; `CliWorker` wraps external agent CLIs.

**RoutingWorker** (sdk_workers.py:30–61): Dispatches each agent to a different Worker by name. Maps agent → (Copilot | Codex | Gemini | Claude) worker. No routing logic needed in Mission or dispatcher; the Dispatcher calls one router, which selects the worker per request (sdk_workers.py: `resolve_workers`, `make_worker`, `available_sdks`).

**CliWorker limits**: External CLIs (Copilot, Codex, Gemini) run in subprocess, **no MCP tools or graph API**. Claims inside external CLIs are not enforced — the CLI has its own boundary. Tool name mappings live in `TOOL_NAME_MAP` (sdk_workers.py:225–249); Claude columns are the source of truth; other SDKs are documented as unknown until verified against actual tool --help output.

**Agents need a shell to self-verify**: An agent can read git output (e.g., `git diff`, `git status`) to examine its own changes, but must shell-escape the paths and cannot rely on file handles from a prior step (shebangs get re-invoked, file state is not shared across tool calls).

## Gate Presets

**Gate** (agentgraph/gates.py: `gate_from_spec`) builds deterministic host-side checks from a spec dict. Presets (agentgraph/gates.py:111–159):

- **pytest**: Run `python -m pytest [args]` with optional python binary and timeout (default: 1200 s). Reports last ~8 lines of stdout.
- **command**: Run `argv` with optional timeout (default: 600 s). Reports exit code or last 500 chars of stdout/stderr.
- **probe**: Start a server (argv), poll `ready_path` (default: /), then GET each route. Picks a free port, sets `port_env` (default: PORT). All routes must 200 (agentgraph/gates.py:228–315).
- **owns**: Validate that all changed files (git status) are under agent ownership paths. Filters `.agentgraph/` (agentgraph/gates.py:318–374).

`validate_gate_spec` (agentgraph/gates.py:35–108) rejects unknown keys and type mismatches. An empty spec returns a gate that passes with zero checks.

## Manifest and Replay (Contract Item 1)

**manifest.py** (NOT YET WRITTEN — describe contract):
- `write_mission_manifest(run_dir, manifest)`: Atomic write to `mission.json` (temp + os.replace).
- `read_mission_manifest(run_dir)`: Read `mission.json`, return dict | None.
- `manifest_from_request(*, slug, agents: list[dict], synthesis, gate: dict, model, max_turns, max_concurrency, target_repo, kind: str = "mission", parent_run_id: str | None)`: Build manifest dict with schema version, kind, agents with tools/owns, created_at ISO-8601.
- `mission_from_manifest(manifest, *, run_dir, gate_callable=None)`: Rebuild AgentSpec list (tools and owns as tuples, meta.sdk only when present). Rebuild Mission with exact same cwd, model, max_turns, max_concurrency. Specs must hash to same identity or replay raises ReplayCacheMiss.

Schema (version 1): `{"schema": 1, "kind": "mission"|"resume"|"replay"|"factory-wave", "slug", "agents": [{name, brief, tools: [...], owns: [...], sdk: ...}], "synthesis", "gate": {...}, "model", "max_turns", "max_concurrency", "target_repo", "parent_run_id", "created_at": ISO-8601}`.

## Factory Waves and State

**FactoryWave** (agentgraph/factory.py:29–37): One wave = agents + gate + synthesis + max_turns/max_concurrency. Loaded from `factory.json`.

**FactoryRunState** (agentgraph/factory.py:50–70): Persists to `<target_repo>/.agentgraph/factory-runs/<factory_run_id>/state.json`. Tracks current_wave, per-wave status, gate_passed, agents_failed. Resumable: `FactoryRunner.run(start_wave=N)` skips prior waves if they already passed.

**FactoryRunner** (agentgraph/factory.py:173–303): Executes each wave in order, halts on first gate failure. Routes per-agent `sdk` choices via `_resolve_sdk_worker()` → `sdk_workers.resolve_workers()`. Writes a manifest (kind "factory-wave") before each wave (agentgraph/factory.py:280–303). Test with CONDUCTION_DRY_RUN=1.

## SQLite Mirror and Deletion

**SqliteMirror.delete_run(run_id)** (NOT YET WRITTEN — contract item 2): One transaction, delete from runs, agents, events, findings, claims where run_id matches. Called by `DELETE /api/runs/<id>` (409 if running).

## JSONL Envelope Trap

**EVERY LINE is wrapped**: `{"seq": n, "run_id": "...", "event": {...event...}}`. The event payload is under the "event" key. Code that checks `line["type"]` at the top level silently matches nothing. Always unwrap first: `envelope.get("event", envelope)` or use `agentgraph.log.read_envelopes` / `read_events` (log.py).
