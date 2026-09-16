"""Make `import agentgraph` resolve to *this* repo's vendored copy.

Without this, pytest's default rootdir insertion puts `tests/` itself on
`sys.path` (since it has no `__init__.py`), which does not help — and if
anything on `sys.path` already points at the original
`Projects/agentgraph` checkout (e.g. an editor-injected path, or running
the wrong interpreter), imports would silently resolve there instead of
the copy under test here. Inserting the conduction repo root at position 0
makes the vendored `agentgraph/` package under `conduction/` win
unconditionally.
"""

from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from urllib.error import URLError
from urllib.request import urlopen

import pytest

REPO_ROOT = str(Path(__file__).resolve().parent.parent)
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)


@dataclass
class AppServer:
    """A running app.py server instance."""

    base_url: str
    process: subprocess.Popen

    def stop(self):
        """Terminate the server process, waiting up to 10s before force-killing."""
        if self.process.poll() is None:
            self.process.terminate()
            try:
                self.process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                self.process.kill()
                self.process.wait()


KNOWN_REPOS_PATH = Path(REPO_ROOT) / ".agentgraph" / "known_repos.json"


@pytest.fixture(autouse=True)
def prune_known_repos():
    """Drop registrations pointing at directories that no longer exist, before
    every test.

    Registering a repo is permanent by design, so each test that launches a
    mission into a tmp repo leaves an entry behind forever. That matters here
    because server readiness is probed with GET /api/runs, and that scan does an
    rglob per registered repo -- let a few hundred dead tmp paths accumulate and
    startup itself blows the fixture's 20 s budget, so unrelated tests start
    failing with timeouts. `app.py` prunes at boot too; this keeps the file from
    growing across a single suite run.
    """
    if not KNOWN_REPOS_PATH.exists():
        return
    try:
        entries = json.loads(KNOWN_REPOS_PATH.read_text(encoding="utf-8"))
    except (ValueError, OSError):
        return
    live = [
        entry
        for entry in entries
        if isinstance(entry, str) and Path(entry).is_dir() and not looks_like_test_repo(entry)
    ]
    if live != entries:
        KNOWN_REPOS_PATH.write_text(json.dumps(live, indent=2), encoding="utf-8")


def looks_like_test_repo(entry: str) -> bool:
    """A temp repo this suite created. Dropped even while it still exists.

    Pruning only DEAD entries is not enough: pytest keeps its temp directories
    for the last few runs, so live-but-disposable repos accumulate. At 70
    entries, /api/runs took 5.3 s -- enough to time out unrelated tests, since
    every entry costs an rglob on every call.
    """
    name = os.path.basename(os.path.normpath(entry))
    # Deliberately NOT "factory-repo-": those live under $HOME inside a
    # TemporaryDirectory context manager that cleans up after itself, and a
    # factory test registers one mid-run. Prune only what the suite leaves
    # behind permanently.
    return name.startswith(("tmp", "cfg-")) or "pytest-of-" in entry


@pytest.fixture
def app_server_factory(tmp_path):
    """
    Factory fixture that returns start(env_overrides: dict | None = None) -> AppServer.

    Spawns app.py on a free port with optional environment overrides,
    waits for it to become ready, and ensures cleanup of all launched servers.
    """
    servers = []

    def start(env_overrides: dict | None = None) -> AppServer:
        # Find a free port
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.bind(("127.0.0.1", 0))
            s.listen(1)
            port = s.getsockname()[1]

        # Build environment
        env = os.environ.copy()
        env["CONDUCTION_PORT"] = str(port)
        # Isolate per-test: never touch conduction's real ecosystem.json, never
        # let the scheduler launch factories under a test unless asked.
        env.setdefault("CONDUCTION_ECOSYSTEM_ROOT", str(tmp_path))
        env.setdefault("CONDUCTION_SCHEDULER", "0")
        # Sanic reports no peer for some client paths; production fails closed
        # on that, so the suite opts in explicitly rather than the app guessing.
        env.setdefault("CONDUCTION_ASSUME_LOCAL_PEER", "1")
        # NOTE: CONDUCTION_STATE_ROOT deliberately NOT set here. Pointing the
        # repo registry at tmp_path is tempting -- it would stop the suite
        # registering temp repos in the developer's real .agentgraph/ -- but it
        # makes test_failed_second_gate_halts_then_resume_completes hang on
        # resume, and the reason is not yet understood. The registry is instead
        # kept clean by the prune_known_repos fixture below.
        if env_overrides:
            env.update(env_overrides)

        # Launch the server
        log_path = tmp_path / f"app-{port}.log"
        log_file = log_path.open("w", encoding="utf-8")

        process = subprocess.Popen(
            [sys.executable, "app.py"],
            cwd=REPO_ROOT,
            env=env,
            stdout=log_file,
            stderr=subprocess.STDOUT,
            text=True,
        )

        base_url = f"http://127.0.0.1:{port}"

        # Wait for the server to become ready (up to 20 seconds)
        deadline = time.monotonic() + 20
        last_error = None
        while time.monotonic() < deadline:
            if process.poll() is not None:
                log_file.close()
                raise RuntimeError(
                    f"Server process died during startup. Log: {log_path.read_text()}"
                )
            try:
                # /api/ping, not /api/runs: the runs scan does an rglob per
                # registered repo, so probing it makes readiness detection get
                # slower as the suite registers more temp repos -- eventually
                # blowing this very timeout and failing unrelated tests.
                with urlopen(f"{base_url}/api/ping", timeout=2) as response:
                    if response.status == 200:
                        break
            except URLError as e:
                last_error = e
                time.sleep(0.2)
        else:
            process.terminate()
            process.wait()
            log_file.close()
            raise RuntimeError(
                f"Server did not become ready within 20s. Last error: {last_error}. "
                f"Log: {log_path.read_text()}"
            )

        server = AppServer(base_url=base_url, process=process)
        servers.append(server)
        return server

    yield start

    # Cleanup: stop all servers launched during this test
    for server in servers:
        server.stop()
