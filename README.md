# Conduction

A local console for running fleets of AI agents against your repositories — and
for understanding what they did afterwards.

Point it at a repo, define agents with briefs and file ownership, attach gate
checks, and launch. Watch the run live, then go back through it: every mission
writes a manifest, an event log and transcripts **into the target repo**, so a
run can be replayed for $0, re-run from its manifest, narrated as a story, or
queried across every repo you have ever pointed it at.

> **Conduction runs agents with `Bash`, `Write` and `Edit` against local
> repositories.** Anyone who can make it start a mission can run code on the
> host. It has no authentication and binds to loopback by default. Read
> [SECURITY.md](SECURITY.md) before exposing it to anything.

## Why

Agent fleets are opaque. You start five agents, they work for twenty minutes,
and what you get is a wall of text and a changed working tree. Conduction's
whole purpose is legibility: which agent claimed which files, what the gate
said, what it cost, where it went wrong, and what happens if you run it again.

## Install

Requires Python 3.12.

```bash
# Windows
.\install.ps1

# Linux / macOS
./install.sh
```

Both create a virtualenv and install dependencies. Idempotent.

## Run

```bash
.\conduction.ps1        # Windows PowerShell
conduction.cmd          # Windows cmd
./conduction.sh         # Linux / macOS
```

The launcher detects an already-running server, starts one in the background
otherwise, and opens `http://127.0.0.1:8000`.

Try it without spending anything:

```bash
CONDUCTION_DRY_RUN=1 python app.py
```

Dry-run mode swaps in a scripted worker — no model calls, about 1.5 s per
agent — so you can exercise the whole lifecycle for free.

## The pages

| Page | What it is for |
|---|---|
| **Dashboard** (`/`) | Fleet KPIs, live agents across every run, activity feed, goals, schedules |
| **Runs** (`/runs`) | Every run from every repo. Open one for swim lanes, a chronological story, and the manifest |
| **Factory** (`/factory`) | Multi-wave missions: an ordered list of gated waves that halts on the first failure and resumes from it |
| **Launch** (`/launch`) | Start a single mission. `/launch?from=<run_id>` prefills the form from an existing run |
| **Projects** (`/projects`) | Registered repositories, with their rules, goals, schedules and history |
| **Query** (`/query`) | Cross-run search over findings, costs and claim conflicts |
| **Settings** (`/settings`) | Rules, goals, schedules and webhooks, ecosystem-wide |

Target-repository fields are chosen with a folder picker, not typed: the server
runs on your machine, so it opens the real OS folder chooser, falling back to an
in-browser directory browser when it cannot.

## Where data lives

The **target repository** owns everything:

```
<target repo>/.agentgraph/
  runs/<slug>/run.jsonl        event stream for one run
  runs/<slug>/mission.json     manifest: agents, gate, model, limits
  runs/<slug>/transcripts/     per-agent transcripts
  project.json                 rules, goals, schedules, webhooks
  factory.json                 factory wave definitions
```

The Conduction repo keeps only derived or local state: a SQLite mirror for
cross-run queries (rebuildable, never authoritative), the registry of known
repos, and `ecosystem.json`.

## Configuration

| Variable | Default | Effect |
|---|---|---|
| `CONDUCTION_PORT` | `8000` | Server port |
| `CONDUCTION_HOST` | `127.0.0.1` | Bind interface |
| `CONDUCTION_DRY_RUN` | off | `1` uses a scripted worker — no model calls |
| `CONDUCTION_ALLOWED_ROOTS` | `$HOME` | Parent directories a target repo may live under |
| `CONDUCTION_SCHEDULER` | on | `0` disables the schedule ticker |
| `CONDUCTION_ECOSYSTEM_ROOT` | repo root | Where `ecosystem.json` lives |
| `CONDUCTION_STATE_ROOT` | repo root | Where `known_repos.json` lives |

Remote access and webhook trust settings are documented in
[SECURITY.md](SECURITY.md) — they change who can reach the server, so they are
described alongside the threat model rather than here.

## Webhooks

Subscriptions live beside rules and schedules in `ecosystem.json` (or a
project's `project.json`):

```json
{
  "webhooks": [
    {
      "url": "https://hooks.example.com/conduction",
      "events": ["run.failed", "gate.failed"],
      "secret": "shared-secret",
      "target_repo": null
    }
  ]
}
```

Events: `run.launched`, `run.completed`, `run.failed`, `gate.failed`. Each
delivery is signed — see [SECURITY.md](SECURITY.md#webhooks).

## Development

```bash
python -m pip install -r requirements.txt -r requirements-dev.txt
python -m pytest tests -q            # ~270 tests, ~4m
ruff check . && ruff format --check .
```

Tests come in two lanes: most spawn a real `app.py` subprocess, while newer ones
run in-process against the module directly. New tests should prefer the fast
lane and leave the subprocess lane for genuine boot behaviour.

`agentgraph/` is a **vendored copy** of the mission engine. Its modules are
excluded from formatting file by file, so they do not diverge from upstream
while anything added there (`agentgraph/procs.py`) is linted like the rest.

PowerShell scripts target Windows PowerShell 5.1, so they avoid `&&`, ternaries
and `??`.

## Architecture

- **Server** — Sanic, single process, Jinja2 templates
- **Engine** — `agentgraph/`: missions, claims, gates, manifests, replay, SDK workers
- **API** — blueprints in `routes/` (config, observe, scheduler, filesystem)
- **Access** — `access.py`: request scope and the remote allowlist
- **Process lifecycle** — `agentgraph/procs.py`: children are tracked per run,
  killed as a tree on interrupt and shutdown, and held by an OS-level job so a
  hard-killed server cannot orphan them
- **Streaming** — SSE for live run events

The front end is being migrated to a Svelte SPA that Sanic serves; the Jinja
templates are maintenance-only in the meantime.

## License

Apache-2.0 — see [LICENSE](LICENSE).
