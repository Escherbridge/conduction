"""Regression tests for the Mission hardening contract, offline.

Follows test_mission.py's style: everything runs on `ScriptedWorker` under
Mission's default `FrozenClock`, no API key, no network, byte-identical
replay asserted the same way. Each test below targets one piece of the
contract that two other agents are landing in parallel:

  (i)   `AgentSpec(..., owns=(...))` rides in request meta, not identity.
  (ii)  a failed agent call surfaces as `MissionResult.failed` and an
        `ok=False` report.
  (iii) a `gate` callable on `Mission` records a `gate` finding and can fail
        the whole result.
  (iv)  replay never re-invokes the gate.
  (v)   `Mission.run(..., stop_when=...)` stops a run early.
  (vi)  `_request_kwargs` locks a read-only spec out of shell tools and
        never leaks an `mcp__` name into `disallowed_tools`.

None of `owns`, `gate`, `stop_when`, `MissionResult.failed`, or the
`disallowed_tools` shell-lockout exist in today's `mission.py` — so most of
these fail loudly (TypeError / AttributeError / KeyError) rather than
silently passing, which is deliberate: they pin the contract the other two
agents are implementing.
"""

from __future__ import annotations

from pathlib import Path

from agentgraph import AgentSpec, Mission, ScriptedWorker
from agentgraph.mission import READ_TOOLS


def test_owns_lands_in_request_meta_without_changing_identity(tmp_path: Path) -> None:
    def responder(request, api):
        return f"{request.worker} ok"

    with_owns = Mission(
        "owns-with", [AgentSpec("alpha", "look at area A", owns=("src/",))], max_turns=5
    )
    without_owns = Mission(
        "owns-without", [AgentSpec("alpha", "look at area A")], max_turns=5
    )

    result_with = with_owns.run(tmp_path / "with.jsonl", worker=ScriptedWorker(responder))
    result_without = without_owns.run(
        tmp_path / "without.jsonl", worker=ScriptedWorker(responder)
    )

    requested_with = next(
        e for e in result_with.graph.events
        if e.type == "agent.requested" and e.payload.get("worker") == "alpha"
    )
    requested_without = next(
        e for e in result_without.graph.events
        if e.type == "agent.requested" and e.payload.get("worker") == "alpha"
    )

    assert list(requested_with.payload["meta"].get("owns") or []) == ["src/"]
    assert requested_with.payload["args_hash"] == requested_without.payload["args_hash"], (
        "owns must not enter the identity hash — it is routing metadata, not "
        "something that changes the answer"
    )


def test_agent_failure_marks_result_failed_and_report_not_ok(tmp_path: Path) -> None:
    def responder(request, api):
        if request.worker == "alpha":
            raise RuntimeError("alpha blew up")
        return "beta ok"

    mission = Mission(
        "fail-test",
        [AgentSpec("alpha", "do a"), AgentSpec("beta", "do b")],
        max_turns=5,
    )
    result = mission.run(tmp_path / "run.jsonl", worker=ScriptedWorker(responder))

    assert result.failed is True

    alpha_report = next(
        o.data for o in result.graph.objects(type="agent_report")
        if o.data["worker"] == "alpha"
    )
    assert alpha_report["ok"] is False

    completed = [e for e in result.graph.events if e.type == "mission.completed"]
    assert completed, "no mission.completed event was recorded"
    assert completed[0].payload.get("status") == "failed"


def test_failing_gate_records_a_gate_finding_and_fails_the_result(tmp_path: Path) -> None:
    from agentgraph.mission import GateResult

    def gate(ctx):
        return GateResult(passed=False, checks=[{"name": "x", "ok": False}])

    mission = Mission("gate-fail", [AgentSpec("alpha", "do a")], gate=gate, max_turns=5)
    result = mission.run(
        tmp_path / "run.jsonl", worker=ScriptedWorker(lambda r, a: f"{r.worker} ok")
    )

    gate_findings = [f for f in result.findings if f.get("topic") == "gate"]
    assert gate_findings and gate_findings[0]["summary"] == "failed"
    assert result.failed is True


def test_passing_gate_records_a_gate_finding_and_does_not_fail(tmp_path: Path) -> None:
    from agentgraph.mission import GateResult

    def gate(ctx):
        return GateResult(passed=True, checks=[{"name": "x", "ok": True}])

    mission = Mission("gate-pass", [AgentSpec("alpha", "do a")], gate=gate, max_turns=5)
    result = mission.run(
        tmp_path / "run.jsonl", worker=ScriptedWorker(lambda r, a: f"{r.worker} ok")
    )

    gate_findings = [f for f in result.findings if f.get("topic") == "gate"]
    assert gate_findings and gate_findings[0]["summary"] == "passed"
    assert result.failed is False


def test_gate_is_not_invoked_during_replay(tmp_path: Path) -> None:
    from agentgraph.mission import GateResult

    calls = {"n": 0}

    def gate(ctx):
        calls["n"] += 1
        return GateResult(passed=True, checks=[{"name": "x", "ok": True}])

    mission = Mission("gate-replay", [AgentSpec("alpha", "do a")], gate=gate, max_turns=5)
    mission.run(tmp_path / "a.jsonl", worker=ScriptedWorker(lambda r, a: f"{r.worker} ok"))
    assert calls["n"] == 1, "gate should have run exactly once on the live pass"

    replayed = mission.replay(tmp_path / "a.jsonl", tmp_path / "b.jsonl")

    assert (tmp_path / "b.jsonl").read_bytes() == (tmp_path / "a.jsonl").read_bytes()
    assert calls["n"] == 1, "replay re-invoked the gate instead of replaying its finding"


def test_stop_when_stops_the_run_before_every_agent_responds(tmp_path: Path) -> None:
    mission = Mission(
        "stop-when",
        [AgentSpec("alpha", "do a"), AgentSpec("beta", "do b")],
        max_turns=5,
    )
    result = mission.run(
        tmp_path / "run.jsonl",
        worker=ScriptedWorker(lambda r, a: f"{r.worker} ok"),
        stop_when=lambda: True,
    )

    assert result.host.stopped_reason, "stop_when should set a stopped_reason"
    responded = {
        e.payload["worker"] for e in result.graph.events if e.type == "agent.responded"
    }
    assert responded != {"alpha", "beta"}, "stop_when did not actually stop the run early"


def test_read_tools_request_kwargs_lock_out_shell_and_hide_mcp_names() -> None:
    mission = Mission("kwargs-test", [])
    kwargs = mission._request_kwargs(READ_TOOLS)

    assert kwargs["permission_mode"] == "bypassPermissions"
    assert "disallowed_tools" in kwargs
    disallowed = kwargs["disallowed_tools"]
    assert "Bash" in disallowed
    assert "PowerShell" in disallowed
    assert not any(name.startswith("mcp__") for name in disallowed), (
        "disallowed_tools must never name an mcp__ tool — those are opted in "
        "via allowed_tools/mcp_server_names, not blocked here"
    )
