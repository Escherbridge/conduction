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

import sys
import os
import socket
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from urllib.request import urlopen
from urllib.error import URLError

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
                with urlopen(f"{base_url}/api/runs", timeout=2) as response:
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
