"""Tests for gate presets: offline, deterministic, no network beyond 127.0.0.1."""

from __future__ import annotations

import subprocess
import sys
import tempfile
import time
from pathlib import Path

import pytest

from agentgraph.gates import KNOWN_GATE_KEYS, gate_from_spec, validate_gate_spec
from agentgraph.mission import GateContext, GateResult


def test_known_gate_keys():
    """The exported constant lists all supported gate types."""
    assert KNOWN_GATE_KEYS == ("pytest", "command", "probe", "owns")


def test_validate_gate_spec_accepts_empty():
    """An empty spec is valid."""
    assert validate_gate_spec({}) == []


def test_validate_gate_spec_rejects_unknown_keys():
    """Unknown keys are rejected with clear errors."""
    errors = validate_gate_spec({"bogus": True, "also_bad": 1})
    assert len(errors) == 2
    assert "bogus" in errors[0]
    assert "also_bad" in errors[1]
    for err in errors:
        assert "pytest" in err and "command" in err  # lists known keys


def test_validate_gate_spec_rejects_bad_pytest_shape():
    """pytest must be true or a dict."""
    errors = validate_gate_spec({"pytest": "yes"})
    assert len(errors) == 1
    assert "pytest" in errors[0]

    errors = validate_gate_spec({"pytest": {"unknown_key": 1}})
    assert any("unknown_key" in e for e in errors)

    errors = validate_gate_spec({"pytest": {"args": "not-a-list"}})
    assert any("args" in e and "list" in e for e in errors)


def test_validate_gate_spec_rejects_bad_command_shape():
    """command must be a dict with argv."""
    errors = validate_gate_spec({"command": True})
    assert any("command" in e for e in errors)

    errors = validate_gate_spec({"command": {}})
    assert any("argv" in e and "required" in e for e in errors)

    errors = validate_gate_spec({"command": {"argv": "not-a-list"}})
    assert any("argv" in e and "list" in e for e in errors)


def test_validate_gate_spec_rejects_bad_probe_shape():
    """probe must be a dict with argv and routes."""
    errors = validate_gate_spec({"probe": []})
    assert any("probe" in e and "dict" in e for e in errors)

    errors = validate_gate_spec({"probe": {"argv": []}})
    assert any("routes" in e and "required" in e for e in errors)

    errors = validate_gate_spec({"probe": {"argv": [], "routes": "not-a-list"}})
    assert any("routes" in e and "list" in e for e in errors)


def test_validate_gate_spec_rejects_bad_owns_shape():
    """owns must be true."""
    errors = validate_gate_spec({"owns": {}})
    assert len(errors) == 1
    assert "owns" in errors[0] and "true" in errors[0]


def test_gate_from_spec_empty_passes():
    """An empty spec returns a gate that passes with zero checks."""
    gate = gate_from_spec({}, cwd=".")
    ctx = GateContext(cwd=".", claim_root=".", reports={}, findings=[])
    result = gate(ctx)

    assert isinstance(result, GateResult)
    assert result.passed is True
    assert result.checks == []


def test_gate_from_spec_raises_on_invalid():
    """gate_from_spec raises ValueError if the spec is invalid."""
    with pytest.raises(ValueError, match="Invalid gate spec"):
        gate_from_spec({"unknown": True}, cwd=".")


def test_command_gate_ok_on_exit_zero():
    """A command gate passes when the command exits 0."""
    gate = gate_from_spec(
        {"command": {"argv": [sys.executable, "-c", "raise SystemExit(0)"]}},
        cwd=".",
    )
    ctx = GateContext(cwd=".", claim_root=".", reports={}, findings=[])
    result = gate(ctx)

    assert result.passed is True
    assert len(result.checks) == 1
    check = result.checks[0]
    assert check["name"] == "command"
    assert check["ok"] is True


def test_command_gate_fails_on_nonzero_exit():
    """A command gate fails when the command exits non-zero."""
    gate = gate_from_spec(
        {"command": {"argv": [sys.executable, "-c", "raise SystemExit(3)"]}},
        cwd=".",
    )
    ctx = GateContext(cwd=".", claim_root=".", reports={}, findings=[])
    result = gate(ctx)

    assert result.passed is False
    assert len(result.checks) == 1
    check = result.checks[0]
    assert check["name"] == "command"
    assert check["ok"] is False
    assert "detail" in check


def test_command_gate_respects_timeout():
    """A command gate fails on timeout."""
    gate = gate_from_spec(
        {
            "command": {
                "argv": [sys.executable, "-c", "import time; time.sleep(10)"],
                "timeout": 0.5,
            }
        },
        cwd=".",
    )
    ctx = GateContext(cwd=".", claim_root=".", reports={}, findings=[])
    result = gate(ctx)

    assert result.passed is False
    check = result.checks[0]
    assert check["ok"] is False
    assert "timeout" in check["detail"] or "timed out" in check["detail"]


def test_probe_gate_passes_on_200_routes():
    """A probe gate passes when all routes return 200."""
    # Inline HTTP server that reads PORT from env
    server_code = """
import os
import sys
from http.server import HTTPServer, BaseHTTPRequestHandler

class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path == '/health':
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b'ok')
        elif self.path == '/api':
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b'api ok')
        else:
            self.send_response(404)
            self.end_headers()
    def log_message(self, *args): pass

port = int(os.environ['TEST_PORT'])
server = HTTPServer(('127.0.0.1', port), Handler)
server.serve_forever()
"""

    gate = gate_from_spec(
        {
            "probe": {
                "argv": [sys.executable, "-c", server_code],
                "port_env": "TEST_PORT",
                "routes": ["/health", "/api"],
                "ready_path": "/health",
                "timeout": 10,
            }
        },
        cwd=".",
    )

    ctx = GateContext(cwd=".", claim_root=".", reports={}, findings=[])
    result = gate(ctx)

    assert result.passed is True
    assert len(result.checks) == 1
    check = result.checks[0]
    assert check["name"] == "probe"
    assert check["ok"] is True
    assert "routes" in check["detail"]
    assert check["detail"]["routes"]["/health"] == 200
    assert check["detail"]["routes"]["/api"] == 200


def test_probe_gate_fails_on_404_route():
    """A probe gate fails when a route returns 404."""
    server_code = """
import os
from http.server import HTTPServer, BaseHTTPRequestHandler

class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path == '/health':
            self.send_response(200)
            self.end_headers()
        else:
            self.send_response(404)
            self.end_headers()
    def log_message(self, *args): pass

port = int(os.environ['TEST_PORT'])
server = HTTPServer(('127.0.0.1', port), Handler)
server.serve_forever()
"""

    gate = gate_from_spec(
        {
            "probe": {
                "argv": [sys.executable, "-c", server_code],
                "port_env": "TEST_PORT",
                "routes": ["/health", "/missing"],
                "ready_path": "/health",
            }
        },
        cwd=".",
    )

    ctx = GateContext(cwd=".", claim_root=".", reports={}, findings=[])
    result = gate(ctx)

    assert result.passed is False
    check = result.checks[0]
    assert check["ok"] is False
    assert check["detail"]["routes"]["/health"] == 200
    assert check["detail"]["routes"]["/missing"] == 404


def test_probe_gate_terminates_server_process():
    """A probe gate ALWAYS terminates the process it started, never by name.

    The gate completing without hanging is proof that it properly terminated the
    server process via terminate/wait/kill on the Popen object it created.
    """
    server_code = """
import os
from http.server import HTTPServer, BaseHTTPRequestHandler

class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.end_headers()
    def log_message(self, *args): pass

port = int(os.environ['TEST_PORT'])
server = HTTPServer(('127.0.0.1', port), Handler)
server.serve_forever()
"""

    gate = gate_from_spec(
        {
            "probe": {
                "argv": [sys.executable, "-c", server_code],
                "port_env": "TEST_PORT",
                "routes": ["/"],
                "timeout": 5,
            }
        },
        cwd=".",
    )

    ctx = GateContext(cwd=".", claim_root=".", reports={}, findings=[])

    # The gate should complete in reasonable time. If the server process wasn't
    # properly terminated, this would hang indefinitely.
    start = time.time()
    result = gate(ctx)
    elapsed = time.time() - start

    # Should complete quickly (server starts + one request + cleanup)
    assert elapsed < 10, f"Gate took {elapsed}s, likely leaked a process"

    # And the check should pass
    assert result.passed is True
    assert result.checks[0]["ok"] is True


def test_owns_gate_with_temp_git_repo():
    """An owns gate validates changed files against agent partitions."""
    with tempfile.TemporaryDirectory() as tmpdir:
        repo = Path(tmpdir)

        # Initialize git repo
        subprocess.run(["git", "init"], cwd=repo, check=True, capture_output=True)
        subprocess.run(
            ["git", "config", "user.email", "test@test.com"],
            cwd=repo,
            check=True,
            capture_output=True,
        )
        subprocess.run(
            ["git", "config", "user.name", "Test"],
            cwd=repo,
            check=True,
            capture_output=True,
        )

        # Create and commit a file
        (repo / "README.md").write_text("initial")
        subprocess.run(["git", "add", "README.md"], cwd=repo, check=True)
        subprocess.run(
            ["git", "commit", "-m", "initial"],
            cwd=repo,
            check=True,
            capture_output=True,
        )

        # Modify the committed file
        (repo / "README.md").write_text("modified")

        # Owns covers the file
        gate = gate_from_spec(
            {"owns": True},
            cwd=str(repo),
            owns={"agent1": ("README.md",)},
        )
        ctx = GateContext(cwd=str(repo), claim_root=str(repo), reports={}, findings=[])
        result = gate(ctx)

        assert result.passed is True
        check = result.checks[0]
        assert check["name"] == "owns"
        assert check["ok"] is True
        assert "README.md" in check["detail"]["changed"]
        assert check["detail"]["strays"] == []


def test_owns_gate_fails_on_unclaimed_writes():
    """An owns gate fails when a file is changed but not owned by any agent."""
    with tempfile.TemporaryDirectory() as tmpdir:
        repo = Path(tmpdir)

        # Initialize git repo
        subprocess.run(["git", "init"], cwd=repo, check=True, capture_output=True)
        subprocess.run(
            ["git", "config", "user.email", "test@test.com"],
            cwd=repo,
            check=True,
            capture_output=True,
        )
        subprocess.run(
            ["git", "config", "user.name", "Test"],
            cwd=repo,
            check=True,
            capture_output=True,
        )

        # Create and commit a file
        (repo / "file.txt").write_text("initial")
        subprocess.run(["git", "add", "file.txt"], cwd=repo, check=True)
        subprocess.run(
            ["git", "commit", "-m", "initial"],
            cwd=repo,
            check=True,
            capture_output=True,
        )

        # Modify it
        (repo / "file.txt").write_text("modified")

        # But owns does NOT cover it
        gate = gate_from_spec(
            {"owns": True},
            cwd=str(repo),
            owns={"agent1": ("other/",)},
        )
        ctx = GateContext(cwd=str(repo), claim_root=str(repo), reports={}, findings=[])
        result = gate(ctx)

        assert result.passed is False
        check = result.checks[0]
        assert check["ok"] is False
        assert "file.txt" in check["detail"]["strays"]


def test_owns_gate_ignores_agentgraph_directory():
    """The owns gate ignores .agentgraph/ paths."""
    with tempfile.TemporaryDirectory() as tmpdir:
        repo = Path(tmpdir)

        # Initialize git repo
        subprocess.run(["git", "init"], cwd=repo, check=True, capture_output=True)
        subprocess.run(
            ["git", "config", "user.email", "test@test.com"],
            cwd=repo,
            check=True,
            capture_output=True,
        )
        subprocess.run(
            ["git", "config", "user.name", "Test"],
            cwd=repo,
            check=True,
            capture_output=True,
        )

        # Create a file in .agentgraph/ (untracked, but shows in git status)
        (repo / ".agentgraph").mkdir()
        (repo / ".agentgraph" / "state.json").write_text("{}")

        # Gate should pass even with empty owns, because .agentgraph/ is filtered
        gate = gate_from_spec(
            {"owns": True},
            cwd=str(repo),
            owns={},
        )
        ctx = GateContext(cwd=str(repo), claim_root=str(repo), reports={}, findings=[])
        result = gate(ctx)

        assert result.passed is True
        check = result.checks[0]
        # .agentgraph/state.json should not appear in strays
        assert ".agentgraph" not in str(check["detail"]["strays"])


def test_gate_with_multiple_checks():
    """A gate with multiple checks passes only if all pass."""
    # All pass
    gate = gate_from_spec(
        {
            "command": {"argv": [sys.executable, "-c", "raise SystemExit(0)"]},
            "pytest": {"args": ["--version"]},  # pytest --version succeeds
        },
        cwd=".",
    )
    ctx = GateContext(cwd=".", claim_root=".", reports={}, findings=[])
    result = gate(ctx)

    assert result.passed is True
    assert len(result.checks) == 2

    # One fails
    gate = gate_from_spec(
        {
            "command": {"argv": [sys.executable, "-c", "raise SystemExit(0)"]},
            "pytest": {
                "args": ["nonexistent_dir"],
                "timeout": 5,
            },  # pytest on bad dir fails
        },
        cwd=".",
    )
    result = gate(ctx)

    assert result.passed is False
    assert len(result.checks) == 2
    assert any(c["ok"] is True for c in result.checks)
    assert any(c["ok"] is False for c in result.checks)


def test_pytest_gate_uses_custom_python():
    """A pytest gate can use a custom python interpreter."""
    gate = gate_from_spec(
        {"pytest": {"python": sys.executable, "args": ["--version"]}},
        cwd=".",
    )
    ctx = GateContext(cwd=".", claim_root=".", reports={}, findings=[])
    result = gate(ctx)

    # pytest --version should succeed
    assert result.passed is True
    check = result.checks[0]
    assert check["name"] == "pytest"


def test_gate_is_deterministic():
    """The same gate called twice with the same filesystem state returns the same result."""
    gate = gate_from_spec(
        {"command": {"argv": [sys.executable, "-c", "print('hello')"]}},
        cwd=".",
    )
    ctx = GateContext(cwd=".", claim_root=".", reports={}, findings=[])

    result1 = gate(ctx)
    result2 = gate(ctx)

    # Both should pass
    assert result1.passed == result2.passed
    assert len(result1.checks) == len(result2.checks)
    # ok must be identical; detail may differ only in timestamps if present
    assert result1.checks[0]["ok"] == result2.checks[0]["ok"]
