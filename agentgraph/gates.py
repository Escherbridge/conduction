"""Gate presets: reusable host-side verification checks for AgentGraph missions.

A gate is deterministic host code that runs after all agents complete and before
synthesis. It can build, test, probe servers, and verify file ownership — anything
that should block completion if it fails.

Usage:
    from agentgraph.gates import gate_from_spec

    gate = gate_from_spec({
        "pytest": True,
        "owns": True,
        "probe": {"argv": [...], "routes": ["/api/status"]}
    }, cwd="/path/to/repo", owns={"agent1": ("src/",), "agent2": ("tests/",)})

    mission = Mission(..., gate=gate)
"""

from __future__ import annotations

import os
import socket
import subprocess
import sys
import time
import urllib.request
from pathlib import Path
from typing import Any, Callable, Optional

from agentgraph.mission import GateContext, GateResult

KNOWN_GATE_KEYS = ("pytest", "command", "probe", "owns")


def validate_gate_spec(spec: dict[str, Any]) -> list[str]:
    """Validate a gate spec, returning error strings (empty list = valid)."""
    errors = []

    if not isinstance(spec, dict):
        return ["gate spec must be a dict"]

    for key in spec:
        if key not in KNOWN_GATE_KEYS:
            errors.append(f"unknown gate key: {key!r} (known: {', '.join(KNOWN_GATE_KEYS)})")

    # Validate pytest
    if "pytest" in spec:
        val = spec["pytest"]
        if val is not True and not isinstance(val, dict):
            errors.append("pytest must be true or a dict with optional args/python/timeout")
        elif isinstance(val, dict):
            for k in val:
                if k not in ("args", "python", "timeout"):
                    errors.append(f"pytest: unknown key {k!r} (allowed: args, python, timeout)")
            if "args" in val and not isinstance(val["args"], list):
                errors.append("pytest.args must be a list")
            if "python" in val and not isinstance(val["python"], str):
                errors.append("pytest.python must be a string")
            if "timeout" in val and not isinstance(val["timeout"], (int, float)):
                errors.append("pytest.timeout must be a number")

    # Validate command
    if "command" in spec:
        val = spec["command"]
        if not isinstance(val, dict):
            errors.append("command must be a dict with argv and optional timeout")
        else:
            if "argv" not in val:
                errors.append("command.argv is required")
            elif not isinstance(val["argv"], list):
                errors.append("command.argv must be a list")
            for k in val:
                if k not in ("argv", "timeout"):
                    errors.append(f"command: unknown key {k!r} (allowed: argv, timeout)")
            if "timeout" in val and not isinstance(val["timeout"], (int, float)):
                errors.append("command.timeout must be a number")

    # Validate probe
    if "probe" in spec:
        val = spec["probe"]
        if not isinstance(val, dict):
            errors.append("probe must be a dict")
        else:
            if "argv" not in val:
                errors.append("probe.argv is required")
            elif not isinstance(val["argv"], list):
                errors.append("probe.argv must be a list")
            if "routes" not in val:
                errors.append("probe.routes is required")
            elif not isinstance(val["routes"], list):
                errors.append("probe.routes must be a list")
            for k in val:
                if k not in ("argv", "port_env", "routes", "ready_path", "timeout"):
                    errors.append(f"probe: unknown key {k!r}")
            if "port_env" in val and not isinstance(val["port_env"], str):
                errors.append("probe.port_env must be a string")
            if "ready_path" in val and not isinstance(val["ready_path"], str):
                errors.append("probe.ready_path must be a string")
            if "timeout" in val and not isinstance(val["timeout"], (int, float)):
                errors.append("probe.timeout must be a number")

    # Validate owns
    if "owns" in spec:
        val = spec["owns"]
        if val is not True:
            errors.append("owns must be true")

    return errors


def gate_from_spec(
    spec: dict[str, Any],
    *,
    cwd: str,
    owns: Optional[dict[str, tuple[str, ...]]] = None,
) -> Callable[[GateContext], GateResult]:
    """Build a gate callable from a spec dict.

    An empty spec returns a gate that passes with zero checks.

    Args:
        spec: Gate configuration with optional keys: pytest, command, probe, owns
        cwd: Working directory for commands
        owns: Agent ownership map (agent_name -> paths), required if spec has "owns"

    Returns:
        A callable that takes GateContext and returns GateResult
    """
    validation_errors = validate_gate_spec(spec)
    if validation_errors:
        raise ValueError(f"Invalid gate spec: {'; '.join(validation_errors)}")

    # Build check functions from the spec
    checks_to_run: list[Callable[[GateContext], dict[str, Any]]] = []

    if "pytest" in spec:
        pytest_config = spec["pytest"]
        if pytest_config is True:
            pytest_config = {}
        checks_to_run.append(_make_pytest_check(cwd, pytest_config))

    if "command" in spec:
        checks_to_run.append(_make_command_check(cwd, spec["command"]))

    if "probe" in spec:
        checks_to_run.append(_make_probe_check(cwd, spec["probe"]))

    if "owns" in spec:
        if owns is None:
            raise ValueError("owns gate requires owns parameter")
        checks_to_run.append(_make_owns_check(cwd, owns))

    def gate(context: GateContext) -> GateResult:
        """Run all configured checks and return the verdict."""
        checks = [check(context) for check in checks_to_run]
        passed = all(check.get("ok", False) for check in checks)
        return GateResult(passed=passed, checks=checks)

    return gate


def _make_pytest_check(
    cwd: str, config: dict[str, Any]
) -> Callable[[GateContext], dict[str, Any]]:
    """Create a pytest check function."""
    python = config.get("python", sys.executable)
    args = config.get("args", ["tests", "-q"])
    timeout = config.get("timeout", 1200)

    def check(context: GateContext) -> dict[str, Any]:
        try:
            proc = subprocess.run(
                [python, "-m", "pytest", *args],
                cwd=cwd,
                capture_output=True,
                text=True,
                timeout=timeout,
            )
            # Show the last ~8 lines as the orchestrator does
            tail = "\n".join(proc.stdout.strip().splitlines()[-8:]) if proc.stdout else ""
            return {"name": "pytest", "ok": proc.returncode == 0, "detail": tail}
        except subprocess.TimeoutExpired:
            return {
                "name": "pytest",
                "ok": False,
                "detail": f"timed out after {timeout}s",
            }
        except Exception as exc:
            return {"name": "pytest", "ok": False, "detail": f"error: {exc}"}

    return check


def _make_command_check(
    cwd: str, config: dict[str, Any]
) -> Callable[[GateContext], dict[str, Any]]:
    """Create a command check function."""
    argv = config["argv"]
    timeout = config.get("timeout", 600)

    def check(context: GateContext) -> dict[str, Any]:
        try:
            proc = subprocess.run(
                argv,
                cwd=cwd,
                capture_output=True,
                text=True,
                timeout=timeout,
            )
            detail = proc.stdout.strip() if proc.stdout else proc.stderr.strip()
            return {
                "name": "command",
                "ok": proc.returncode == 0,
                "detail": detail[-500:] if detail else f"exit {proc.returncode}",
            }
        except subprocess.TimeoutExpired:
            return {
                "name": "command",
                "ok": False,
                "detail": f"timed out after {timeout}s",
            }
        except Exception as exc:
            return {"name": "command", "ok": False, "detail": f"error: {exc}"}

    return check


def _make_probe_check(
    cwd: str, config: dict[str, Any]
) -> Callable[[GateContext], dict[str, Any]]:
    """Create a probe check function that starts a server and tests routes."""
    argv = config["argv"]
    port_env = config.get("port_env", "PORT")
    routes = config["routes"]
    ready_path = config.get("ready_path", "/")
    timeout = config.get("timeout", 20)

    def check(context: GateContext) -> dict[str, Any]:
        # Pick a free port
        port = _free_port()

        # Start the server process with the port in its environment
        env = {**os.environ, port_env: str(port)}
        server: Optional[subprocess.Popen] = None

        try:
            server = subprocess.Popen(
                argv,
                cwd=cwd,
                env=env,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
            )

            # Wait for the server to become ready
            base = f"http://127.0.0.1:{port}"
            deadline = time.time() + timeout
            ready = False

            while time.time() < deadline:
                if server.poll() is not None:
                    return {
                        "name": "probe",
                        "ok": False,
                        "detail": f"server died during startup (port {port})",
                    }
                try:
                    with urllib.request.urlopen(
                        base + ready_path, timeout=2
                    ) as resp:
                        if resp.status == 200:
                            ready = True
                            break
                except Exception:
                    time.sleep(0.5)

            if not ready:
                return {
                    "name": "probe",
                    "ok": False,
                    "detail": f"server did not become ready (port {port})",
                }

            # Test all routes
            results = {}
            for route in routes:
                try:
                    with urllib.request.urlopen(base + route, timeout=10) as resp:
                        results[route] = resp.status
                except urllib.error.HTTPError as exc:
                    results[route] = exc.code
                except Exception as exc:
                    results[route] = f"error: {type(exc).__name__}"

            ok = all(status == 200 for status in results.values())
            return {
                "name": "probe",
                "ok": ok,
                "detail": {"port": port, "routes": results},
            }

        except Exception as exc:
            return {"name": "probe", "ok": False, "detail": f"error: {exc}"}

        finally:
            # ALWAYS terminate the Popen we started, never by name
            if server is not None:
                server.terminate()
                try:
                    server.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    server.kill()
                    server.wait()

    return check


def _make_owns_check(
    cwd: str, owns: dict[str, tuple[str, ...]]
) -> Callable[[GateContext], dict[str, Any]]:
    """Create an owns check function that validates writes against agent partitions."""

    def check(context: GateContext) -> dict[str, Any]:
        try:
            # Get changed files from git
            proc = subprocess.run(
                ["git", "status", "--porcelain"],
                cwd=cwd,
                capture_output=True,
                text=True,
                timeout=10,
            )

            # Parse changed paths
            changed = []
            for line in proc.stdout.splitlines():
                if not line.strip():
                    continue
                # Format: "XY path" where XY are status codes
                path = line[3:].strip().strip('"').replace("\\", "/")
                changed.append(path)

            # Filter out .agentgraph/ paths
            paths = [p for p in changed if not p.startswith(".agentgraph/")]

            # Flatten all owned paths
            allowed = []
            for agent_owns in owns.values():
                for path in agent_owns:
                    allowed.append(path.replace("\\", "/"))

            # Find strays: paths not under any owned area
            strays = []
            for path in paths:
                is_owned = False
                for owned_path in allowed:
                    normalized = owned_path.rstrip("/")
                    # Path is owned if it equals the owned path or is under it
                    if path == normalized or path.startswith(normalized + "/"):
                        is_owned = True
                        break
                if not is_owned:
                    strays.append(path)

            return {
                "name": "owns",
                "ok": len(strays) == 0,
                "detail": {"changed": paths, "strays": strays},
            }

        except Exception as exc:
            return {"name": "owns", "ok": False, "detail": f"error: {exc}"}

    return check


def _free_port() -> int:
    """Find a free port on 127.0.0.1."""
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


__all__ = [
    "KNOWN_GATE_KEYS",
    "gate_from_spec",
    "validate_gate_spec",
]
