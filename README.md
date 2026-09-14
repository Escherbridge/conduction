# Conduction

Wave L Interface Contract — multi-agent orchestration harness providing app shell, fleet observability, rules/goals/schedules, and one-click install+launch.

## 60-Second Quickstart

### 1. Install

**Windows (PowerShell):**
```powershell
.\install.ps1
```

**Linux/macOS:**
```bash
./install.sh
```

This creates a virtual environment and installs all dependencies. Idempotent — safe to run multiple times.

### 2. Launch

**Windows (PowerShell):**
```powershell
.\conduction.ps1
```

**Windows (Command Prompt):**
```cmd
conduction.cmd
```

**Linux/macOS:**
```bash
./conduction.sh
```

The launcher automatically:
- Detects if the server is already running
- Starts the server in the background if needed
- Opens your browser to `http://127.0.0.1:8000`

### 3. Create Desktop Shortcut (Optional, Windows only)

```powershell
.\scripts\create-shortcut.ps1
```

Creates shortcuts on your Desktop and in the Start Menu for one-click access.

**To remove shortcuts:**
```powershell
.\scripts\create-shortcut.ps1 -Remove
```

## Pages

### Dashboard (`/`)
Fleet KPIs, live agents across all runs, activity feed, goals, and upcoming schedules. Your central command center.

### Runs (`/runs`)
Every run from every project. Click a run to see:
- **Swim lanes** — visual timeline of agent activity
- **Story** — chronological event log
- **Manifest** — agents, costs, findings, claims

### Factory (`/factory`)
Factory runs and pipeline view. Launch multi-wave orchestrated missions.

### Launch (`/launch`)
Launch a single mission with custom agents and configuration.

### Projects (`/projects`)
Registered target repositories. Each project shows:
- Rules, goals, and schedules
- Run history and statistics
- Project-specific configuration

### Query (`/query`)
Cross-run trace queries. Search findings, events, and agent activity across all runs.

### Settings (`/settings`)
Ecosystem-wide configuration:
- Rules (apply to all runs or specific agent types)
- Goals (track objectives across runs)
- Schedules (automated factory/mission launches)
- SDK availability
- Dry-run mode toggle

## Data Storage

### Target Repository
Each target repository gets a `.agentgraph/` directory containing:
- `project.json` — Project-level rules, goals, and schedules
- `run-<id>.jsonl` — Event stream for each run (findings, claims, commands)
- `factory.json` — Factory configuration (if using factory mode)
- Per-agent logs and transcripts

### Conduction Repository
- `.agentgraph/mirror.db` — SQLite database mirroring all runs for observability
- `.agentgraph/app.pid` — Running server process ID
- `.agentgraph/app.log` — Server logs
- `.agentgraph/ecosystem.json` — Ecosystem-wide rules, goals, and schedules

## Environment Variables

### `CONDUCTION_PORT`
Server port. Default: `8000`

**Example:**
```bash
export CONDUCTION_PORT=9000
./conduction.sh
```

### `CONDUCTION_DRY_RUN`
Enable dry-run mode. When `1`, missions use a ScriptedWorker instead of making real API calls.

**Example:**
```bash
CONDUCTION_DRY_RUN=1 python app.py
```

### `CONDUCTION_ALLOWED_ROOTS`
Comma-separated list of allowed parent directories for target repositories. Security control to limit which directories can be targeted.

**Example:**
```bash
export CONDUCTION_ALLOWED_ROOTS="/home/user/projects,/opt/repos"
```

### `CONDUCTION_SCHEDULER`
Enable/disable the automated scheduler. Set to `0` to disable. Default: enabled

**Example:**
```bash
export CONDUCTION_SCHEDULER=0  # Disable scheduler
```

## Running Tests

### Full Test Suite

From the **parent directory** (the one containing the Conduction repo):

```bash
cd /path/to/parent/of/conduction
source agentgraph/.venv/bin/activate  # or activate the venv that has pytest
python -m pytest conduction/tests -v
```

**Note:** Use the parent project's venv that has pytest installed, not `conduction/venv`.

### Quick Test (Dry-Run Mode)

Tests using `CONDUCTION_DRY_RUN=1` run much faster (~1.5s per agent) since they use a ScriptedWorker:

```bash
CONDUCTION_DRY_RUN=1 python -m pytest conduction/tests/test_steering_flow.py -v
```

### Test Suites

- `tests/test_app_routes.py` — HTTP routes and server behavior
- `tests/test_steering_flow.py` — Mission orchestration and steering
- More test files added by other agents...

## Development Notes

### PowerShell Scripts
All `.ps1` scripts require Windows PowerShell 5.1+ and avoid modern syntax (`&&`, ternary operators, `??`) for maximum compatibility.

### Background Processes
The launcher scripts track server PIDs in `.agentgraph/app.pid`. To stop a running server manually:

**Windows:**
```powershell
$pid = Get-Content .agentgraph\app.pid
Stop-Process -Id $pid
```

**Linux/macOS:**
```bash
kill $(cat .agentgraph/app.pid)
```

### Logs
Server logs are written to `.agentgraph/app.log`. Tail them to see real-time activity:

```bash
tail -f .agentgraph/app.log
```

## Architecture

Conduction implements the Wave L Interface Contract:
- **Templating:** Jinja2 with `templates/base.html` providing the app shell
- **Styling:** CSS custom properties in `static/css/styles.css`
- **API:** Sanic blueprints in `conduction/routes/`
- **Policy:** Rules, goals, and schedules in `agentgraph/policy.py`
- **Scheduler:** Background task launching scheduled runs
- **Observability:** SQLite mirror for cross-run queries and fleet monitoring

## License

See parent project for license information.
