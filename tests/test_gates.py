"""One test per build-phase gate from the plan, section 4.

These are gates, not unit tests: each asserts the property that was supposed to
be *unproven* before the phase, so a regression that breaks the architecture
fails here rather than somewhere subtle downstream.

Everything runs on `ScriptedWorker`. Nothing here touches the API or costs
money, and every run is deterministic under a `FrozenClock`.
"""

from __future__ import annotations

import asyncio
from decimal import Decimal
from pathlib import Path

import pytest

from activegraph import Event, FrozenClock, Graph, IDGen, Runtime, behavior

from agentgraph import (
    AGENT_REQUESTED,
    AGENT_RESPONDED,
    AgentCache,
    ClaimLedger,
    Host,
    JSONLLog,
    ScriptedWorker,
    read_events,
    ReplayPlan,
    request_agent,
)
from agentgraph.claims import ClaimViolation, make_claim_hook
from agentgraph.events import CLAIM_GRANTED, CLAIM_REJECTED, FINDING_RECORDED
from agentgraph.mcp_tools import SERVER_NAME

SEED = "mission.started"


def fresh_graph(run_id: str = "RUN-TEST") -> Graph:
    """A graph with every nondeterministic input pinned."""
    return Graph(ids=IDGen(), clock=FrozenClock("2026-08-21T00:00:00Z"), run_id=run_id)


def two_worker_behaviors() -> list:
    """A seed behavior that fans out to two agents, plus a join behavior."""

    @behavior(name="fan_out_two", on=[SEED])
    def fan_out_two(event: Event, graph: Graph, ctx) -> None:
        for name in ("scout-a", "scout-b"):
            request_agent(
                graph,
                worker=name,
                prompt=f"survey sector {name}",
                caused_by=event.id,
                mcp_server_names=(SERVER_NAME,),
            )

    @behavior(name="join_outputs", on=[AGENT_RESPONDED])
    def join_outputs(event: Event, graph: Graph, ctx) -> None:
        if event.payload.get("error"):
            return
        graph.add_object(
            "report",
            {"worker": event.payload["worker"], "output": event.payload["output"]},
        )

    return [fan_out_two, join_outputs]


# --------------------------------------------------------------------------
# Phase 1 — injection
# --------------------------------------------------------------------------


def test_phase1_host_emit_enqueues_into_the_runtime_fifo() -> None:
    """The gate the whole design rests on: a host-side emit is real work.

    `Runtime.__init__` subscribes `_on_event` to the graph, and `_on_event`
    pushes into the FIFO — so an event injected from outside the loop is
    indistinguishable from one a behavior emitted.
    """
    seen: list[str] = []

    @behavior(name="observe", on=["agent.responded"])
    def observe(event: Event, graph: Graph, ctx) -> None:
        seen.append(event.payload["worker"])

    graph = fresh_graph()
    rt = Runtime(graph, behaviors=[observe])
    rt.run_quantum()

    for worker in ("w1", "w2", "w3"):
        graph.emit(
            Event(
                id=graph.ids.event(),
                type="agent.responded",
                payload={"worker": worker},
                actor="host",
                timestamp=graph.clock.now(),
            )
        )

    assert len(rt._queue) == 3, "host emit did not reach the runtime queue"
    result = rt.run_quantum()
    assert result.queue_events_processed == 3
    assert seen == ["w1", "w2", "w3"], "FIFO order was not preserved"


# --------------------------------------------------------------------------
# Phase 2 — the host loop
# --------------------------------------------------------------------------


def test_phase2_two_agents_coordinate_through_the_graph() -> None:
    """Gate: two agents run concurrently and both land back on the graph."""
    order: list[str] = []

    def responder(request, api):
        order.append(request.worker)
        return f"{request.worker} reporting"

    # scout-a is slower, so completion order inverts dispatch order — proving
    # the host is not secretly serializing them.
    worker = ScriptedWorker(responder, delays={"scout-a": 0.05, "scout-b": 0.01})

    graph = fresh_graph()
    rt = Runtime(graph, behaviors=two_worker_behaviors())
    host = Host(rt, worker, max_concurrency=4)
    graph.emit(
        Event(id=graph.ids.event(), type=SEED, payload={}, timestamp=graph.clock.now())
    )

    result = asyncio.run(host.run())
    host.close()

    assert result.agents_dispatched == 2
    assert result.agents_completed == 2
    assert result.idle, result.stopped_reason
    assert result.total_cost_usd == Decimal("0.02")

    reports = {o.data["worker"] for o in graph.objects(type="report")}
    assert reports == {"scout-a", "scout-b"}

    # Both were in flight at once: b finished before a, though a was dispatched
    # first. If the host serialized them, a would always finish first.
    responded = [e.payload["worker"] for e in graph.events if e.type == AGENT_RESPONDED]
    assert responded == ["scout-b", "scout-a"], responded


def test_phase2_agent_failure_is_recorded_not_raised() -> None:
    """A worker blowing up is a fact on the graph, not a crashed host."""

    def responder(request, api):
        if request.worker == "scout-b":
            raise RuntimeError("sector unreachable")
        return "ok"

    graph = fresh_graph()
    rt = Runtime(graph, behaviors=two_worker_behaviors())
    host = Host(rt, ScriptedWorker(responder))
    graph.emit(
        Event(id=graph.ids.event(), type=SEED, payload={}, timestamp=graph.clock.now())
    )

    result = asyncio.run(host.run())
    host.close()

    assert result.agents_completed == 1
    assert result.agents_failed == 1
    errors = [
        e.payload["error"]
        for e in graph.events
        if e.type == AGENT_RESPONDED and e.payload.get("error")
    ]
    assert errors[0]["message"] == "sector unreachable"


# --------------------------------------------------------------------------
# Phase 3 — the log
# --------------------------------------------------------------------------


def test_phase3_log_is_readable_while_the_run_is_still_going(tmp_path: Path) -> None:
    """Gate: `tail -f` sees events mid-run, not at the end.

    Asserted by reading the file from inside a worker: if the writer buffered,
    the worker would see an empty or truncated file.
    """
    path = tmp_path / "run.jsonl"
    log = JSONLLog(path)
    observed: dict[str, int] = {}

    def responder(request, api):
        # Read the log off disk, exactly as an operator tailing it would.
        with path.open("r", encoding="utf-8") as fh:
            observed[request.worker] = sum(1 for line in fh if line.strip())
        return "ok"

    graph = fresh_graph()
    log.attach(graph)
    rt = Runtime(graph, behaviors=two_worker_behaviors())
    host = Host(rt, ScriptedWorker(responder, delays={"scout-b": 0.02}))
    graph.emit(
        Event(id=graph.ids.event(), type=SEED, payload={}, timestamp=graph.clock.now())
    )

    asyncio.run(host.run())
    host.close()
    log.close()

    assert observed["scout-a"] > 0, "the log was empty while the run was in flight"
    assert observed["scout-b"] >= observed["scout-a"]
    assert log.written == len(graph.events)

    # And it round-trips: the file is a faithful copy of the in-memory log.
    replayed = read_events(path)
    assert [e.id for e in replayed] == [e.id for e in graph.events]
    assert [e.type for e in replayed] == [e.type for e in graph.events]


# --------------------------------------------------------------------------
# Phase 4 — the blackboard
# --------------------------------------------------------------------------


def test_phase4_a_worker_reads_another_workers_finding() -> None:
    """Gate: mid-run, one worker sees what another already published.

    This is the property that makes the log a blackboard rather than a
    transcript. `scout-b` is delayed, so `scout-a` has published by the time it
    looks.
    """

    def responder(request, api):
        if request.worker == "scout-a":
            api.emit_finding("terrain", "sector A is impassable", detail="ravine")
            return "a done"
        seen = api.findings()
        assert seen, "scout-b could not see scout-a's finding"
        return f"b saw: {seen[0]['summary']}"

    graph = fresh_graph()
    rt = Runtime(graph, behaviors=two_worker_behaviors())
    host = Host(rt, ScriptedWorker(responder, delays={"scout-b": 0.05}))
    graph.emit(
        Event(id=graph.ids.event(), type=SEED, payload={}, timestamp=graph.clock.now())
    )

    asyncio.run(host.run())
    host.close()

    outputs = {
        o.data["worker"]: o.data["output"] for o in graph.objects(type="report")
    }
    assert outputs["scout-b"] == "b saw: sector A is impassable"

    findings = [e for e in graph.events if e.type == FINDING_RECORDED]
    assert len(findings) == 1
    # The finding is causally attributed to the worker's own request, which is
    # what makes it harvestable as a replay side effect.
    request_ids = {e.id for e in graph.events if e.type == AGENT_REQUESTED}
    assert findings[0].caused_by in request_ids


# --------------------------------------------------------------------------
# Phase 5 — claims
# --------------------------------------------------------------------------


def test_phase5_conflicting_claim_is_refused(tmp_path: Path) -> None:
    """Gate: the second worker to ask for a file does not get it."""
    target = str(tmp_path / "shared.py")
    results: dict[str, dict] = {}

    def responder(request, api):
        results[request.worker] = api.claim([target])
        return "ok"

    graph = fresh_graph()
    rt = Runtime(graph, behaviors=two_worker_behaviors())
    # Serialize the two claims so the outcome is decided, not raced.
    host = Host(rt, ScriptedWorker(responder, delays={"scout-b": 0.05}))
    graph.emit(
        Event(id=graph.ids.event(), type=SEED, payload={}, timestamp=graph.clock.now())
    )

    asyncio.run(host.run())
    host.close()

    assert results["scout-a"]["granted"] is True
    assert results["scout-b"]["granted"] is False
    assert results["scout-b"]["conflicts"][0]["owner"] == "scout-a"

    assert any(e.type == CLAIM_GRANTED for e in graph.events)
    assert any(e.type == CLAIM_REJECTED for e in graph.events)
    assert host.ledger.owner_of(target) == "scout-a"


def test_phase5_pretooluse_hook_rejects_an_unclaimed_write(tmp_path: Path) -> None:
    """Gate: a conflicting write is rejected.

    The hook is the only real enforcement — a worker will not voluntarily
    consult the ledger — so this asserts the deny decision itself, in the shape
    the SDK consumes.
    """
    target = str(tmp_path / "owned.py")
    ledger = ClaimLedger()
    ledger.grant("scout-a", [target])
    violations: list[ClaimViolation] = []

    hook = make_claim_hook("scout-b", ledger, on_violation=violations.append)
    decision = asyncio.run(
        hook(
            {
                "hook_event_name": "PreToolUse",
                "tool_name": "Write",
                "tool_input": {"file_path": target, "content": "x"},
            },
            None,
            None,
        )
    )

    specific = decision["hookSpecificOutput"]
    assert specific["permissionDecision"] == "deny"
    assert "claimed by scout-a" in specific["permissionDecisionReason"]
    assert violations and violations[0].owner == "scout-a"

    # The owner is allowed through, and a read is never gated at all.
    owner_hook = make_claim_hook("scout-a", ledger)
    assert asyncio.run(
        owner_hook(
            {"tool_name": "Write", "tool_input": {"file_path": target}}, None, None
        )
    ) == {}
    assert asyncio.run(
        hook({"tool_name": "Read", "tool_input": {"file_path": target}}, None, None)
    ) == {}


def test_phase5_claim_paths_compare_case_and_separator_insensitively(
    tmp_path: Path,
) -> None:
    """A claim and a write of the same file must compare equal on Windows."""
    ledger = ClaimLedger()
    ledger.grant("w", [str(tmp_path / "Sub" / "File.py")])
    assert ledger.holds("w", str(tmp_path / "sub" / "file.py"))
    assert ledger.holds("w", str(tmp_path).replace("\\", "/") + "/Sub/File.py")


# --------------------------------------------------------------------------
# Phase 6 — replay
# --------------------------------------------------------------------------


def _record_run(path: Path, *, responder, delays) -> Graph:
    graph = fresh_graph()
    log = JSONLLog(path).attach(graph)
    rt = Runtime(graph, behaviors=two_worker_behaviors())
    host = Host(rt, ScriptedWorker(responder, delays=delays))
    graph.emit(
        Event(id=graph.ids.event(), type=SEED, payload={}, timestamp=graph.clock.now())
    )
    asyncio.run(host.run())
    host.close()
    log.close()
    return graph


def _replay_run(recorded_path: Path, replay_path: Path):
    """Re-execute the behaviors against the recorded log. No worker runs."""
    recorded = read_events(recorded_path)

    def must_not_run(request, api):
        raise AssertionError(
            f"replay invoked a live worker for {request.worker!r}; every agent "
            f"call must be served from the recorded log"
        )

    graph = fresh_graph()
    log = JSONLLog(replay_path).attach(graph)
    rt = Runtime(graph, behaviors=two_worker_behaviors())
    host = Host(
        rt,
        ScriptedWorker(must_not_run),
        cache=AgentCache.from_events(recorded),
        replay=True,
        plan=ReplayPlan.from_events(recorded),
    )
    graph.emit(
        Event(id=graph.ids.event(), type=SEED, payload={}, timestamp=graph.clock.now())
    )
    result = asyncio.run(host.run())
    host.close()
    log.close()
    return graph, result


def test_phase6_replay_reproduces_the_log_byte_for_byte(tmp_path: Path) -> None:
    """Gate: byte-identical event log on replay.

    The recorded run finishes out of dispatch order on purpose. If the reorder
    buffer were absent, replay would inject in a different order and the logs
    would diverge.
    """

    def responder(request, api):
        if request.worker == "scout-a":
            api.emit_finding("terrain", "sector A is impassable")
        return f"{request.worker} reporting"

    recorded_path = tmp_path / "recorded.jsonl"
    replay_path = tmp_path / "replayed.jsonl"

    _record_run(recorded_path, responder=responder, delays={"scout-a": 0.05})
    graph, result = _replay_run(recorded_path, replay_path)

    assert result.cache_hits == 2, "replay did not serve every call from the log"
    assert result.agents_completed == 2

    original = recorded_path.read_bytes()
    replayed = replay_path.read_bytes()
    assert replayed == original, "replayed log diverged from the recording"

    # And the worker's mid-run write survived, even though no worker ran.
    assert sum(1 for e in graph.events if e.type == FINDING_RECORDED) == 1


def test_phase6_replay_fails_loud_when_the_request_changed(tmp_path: Path) -> None:
    """A changed prompt, model, or tool set must miss rather than reuse.

    This is the `_canonical_prompt_payload` property, ported: identity folds in
    everything that changes the answer, so a reconfigured agent cannot silently
    inherit a stale response.
    """
    from agentgraph.dispatcher import AgentRequest, ReplayCacheMiss

    recorded_path = tmp_path / "recorded.jsonl"
    _record_run(
        recorded_path, responder=lambda request, api: "ok", delays={}
    )
    cache = AgentCache.from_events(read_events(recorded_path))

    original = AgentRequest(
        worker="scout-a", prompt="survey sector scout-a", mcp_server_names=(SERVER_NAME,)
    )
    assert cache.has(original.args_hash, 0), "the recorded call did not round-trip"

    from dataclasses import replace as dc_replace

    for changed in (
        dc_replace(original, prompt="survey sector scout-a "),
        dc_replace(original, model="claude-opus-5"),
        dc_replace(original, allowed_tools=("Read",)),
        dc_replace(original, system_prompt="be terse"),
        dc_replace(original, mcp_server_names=()),
    ):
        assert not cache.has(changed.args_hash, 0), (
            f"a changed request reused a recorded response: {changed}"
        )

    graph = fresh_graph()
    rt = Runtime(graph, behaviors=[])
    host = Host(rt, ScriptedWorker(lambda r, a: "x"), cache=AgentCache(), replay=True)
    request_agent(graph, worker="scout-a", prompt="never recorded")
    with pytest.raises(ReplayCacheMiss):
        asyncio.run(host.run())
    host.close()


def test_phase6_identical_prompts_stay_distinct_calls(tmp_path: Path) -> None:
    """Two identical agent calls are two units of work, not one cache entry.

    `ToolCache` collapses them, which is right for a tool. For an agent it
    would silently halve the recorded cost and lose a response.
    """

    @behavior(name="fan_out_same", on=[SEED])
    def fan_out_same(event: Event, graph: Graph, ctx) -> None:
        for name in ("twin-a", "twin-b"):
            request_agent(graph, worker=name, prompt="identical prompt", caused_by=event.id)

    calls = {"n": 0}

    def responder(request, api):
        calls["n"] += 1
        return f"answer {calls['n']}"

    graph = fresh_graph()
    rt = Runtime(graph, behaviors=[fan_out_same])
    host = Host(rt, ScriptedWorker(responder))
    graph.emit(
        Event(id=graph.ids.event(), type=SEED, payload={}, timestamp=graph.clock.now())
    )
    asyncio.run(host.run())
    host.close()

    # `worker` is not part of the identity, so both requests hash identically.
    hashes = {
        e.payload["args_hash"] for e in graph.events if e.type == AGENT_REQUESTED
    }
    assert len(hashes) == 1, "the two calls should be content-identical"

    occurrences = sorted(
        e.payload["occurrence"] for e in graph.events if e.type == AGENT_REQUESTED
    )
    assert occurrences == [0, 1], "occurrence indices did not disambiguate the calls"
    assert calls["n"] == 2, "one of the two identical calls was skipped"

    cache = AgentCache.from_events(graph.events)
    assert len(cache) == 2
    assert cache.get(list(hashes)[0], 0).output != cache.get(list(hashes)[0], 1).output


def test_phase6_pre_seeded_facts_are_not_replayed_twice(tmp_path: Path) -> None:
    """Facts seeded onto the board before `run()` must not enter the plan.

    A workflow that puts deterministic host-computed facts on the graph before
    dispatching (an inventory, a checksum, a diff) re-seeds them itself on
    replay. If the plan also carried them they would land twice and shift every
    window after them — which presents as a `ReplayOrderStall` at the very end,
    a long way from the cause.
    """
    from agentgraph.events import emit

    def seed(graph: Graph, seed_event: Event) -> None:
        emit(
            graph,
            FINDING_RECORDED,
            {"worker": "host", "topic": "precomputed", "summary": "a mechanical fact"},
            actor="host",
            caused_by=seed_event.id,
        )

    def go(path: Path, *, recorded=None):
        graph = fresh_graph()
        log = JSONLLog(path).attach(graph)
        rt = Runtime(graph, behaviors=two_worker_behaviors())
        kwargs = {}
        if recorded is not None:
            kwargs = {
                "cache": AgentCache.from_events(recorded),
                "plan": ReplayPlan.from_events(recorded),
                "replay": True,
            }
        host = Host(rt, ScriptedWorker(lambda r, a: f"{r.worker} ok"), **kwargs)
        seed_event = emit(graph, SEED, {}, actor="host")
        seed(graph, seed_event)
        result = asyncio.run(host.run())
        host.close()
        log.close()
        return graph, result

    recorded_path, replay_path = tmp_path / "rec.jsonl", tmp_path / "rep.jsonl"
    go(recorded_path)
    recorded = read_events(recorded_path)

    # The seeded finding precedes every request, so the plan must skip it.
    assert ReplayPlan.from_events(recorded).groups
    assert not any(
        p.payload.get("topic") == "precomputed"
        for group in ReplayPlan.from_events(recorded).groups
        for p in group
    ), "a pre-run seeded fact leaked into the replay plan"

    _, result = go(replay_path, recorded=recorded)
    assert result.cache_hits == 2
    assert replay_path.read_bytes() == recorded_path.read_bytes()
    assert sum(1 for e in read_events(replay_path) if e.type == FINDING_RECORDED) == 1
