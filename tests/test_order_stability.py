"""Order stability under concurrency — the plan's open decision 3.

Running concurrency beside a runtime whose contract says single-threaded is
legal but untested upstream, and the seams are ours. These are the regression
tests for those seams: whatever the workers do to completion order, the event
log must stay a total order that replays exactly.

The wider fan-out here (eight workers, adversarial delays) is the point. The
two-agent gate in `test_gates.py` can pass by accident; this cannot.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest
from activegraph import Event, FrozenClock, Graph, IDGen, Runtime, behavior

from agentgraph import (
    AGENT_REQUESTED,
    AGENT_RESPONDED,
    AgentCache,
    Host,
    JSONLLog,
    ReplayPlan,
    ScriptedWorker,
    read_events,
    request_agent,
)
from agentgraph.events import FINDING_RECORDED

SEED = "mission.started"
WORKERS = [f"scout-{i}" for i in range(8)]

#: Deliberately adversarial: dispatch order is 0..7, completion order is
#: nothing like it, and two workers tie.
DELAYS = {
    "scout-0": 0.06,
    "scout-1": 0.01,
    "scout-2": 0.05,
    "scout-3": 0.02,
    "scout-4": 0.02,
    "scout-5": 0.04,
    "scout-6": 0.00,
    "scout-7": 0.03,
}


def fresh_graph() -> Graph:
    return Graph(ids=IDGen(), clock=FrozenClock("2026-08-21T00:00:00Z"), run_id="RUN-8")


def wide_behaviors() -> list:
    @behavior(name="fan_out_eight", on=[SEED])
    def fan_out_eight(event: Event, graph: Graph, ctx) -> None:
        for name in WORKERS:
            request_agent(graph, worker=name, prompt=f"survey {name}")

    @behavior(name="collect", on=[AGENT_RESPONDED])
    def collect(event: Event, graph: Graph, ctx) -> None:
        if not event.payload.get("error"):
            graph.add_object("report", {"worker": event.payload["worker"]})

    @behavior(name="index_finding", on=[FINDING_RECORDED])
    def index_finding(event: Event, graph: Graph, ctx) -> None:
        graph.add_object("finding", {"worker": event.payload["worker"]})

    return [fan_out_eight, collect, index_finding]


def responder(request, api):
    """Every worker publishes mid-run, so side effects interleave too."""
    api.emit_finding("survey", f"{request.worker} saw something")
    return f"{request.worker} done"


def record(path: Path, *, delays: dict[str, float]) -> Graph:
    graph = fresh_graph()
    log = JSONLLog(path).attach(graph)
    rt = Runtime(graph, behaviors=wide_behaviors())
    host = Host(rt, ScriptedWorker(responder, delays=delays), max_concurrency=8)
    graph.emit(Event(id=graph.ids.event(), type=SEED, payload={}, timestamp=graph.clock.now()))
    asyncio.run(host.run())
    host.close()
    log.close()
    return graph


def replay(recorded_path: Path, replay_path: Path):
    recorded = read_events(recorded_path)

    def must_not_run(request, api):
        raise AssertionError("replay invoked a live worker")

    graph = fresh_graph()
    log = JSONLLog(replay_path).attach(graph)
    rt = Runtime(graph, behaviors=wide_behaviors())
    host = Host(
        rt,
        ScriptedWorker(must_not_run),
        max_concurrency=8,
        cache=AgentCache.from_events(recorded),
        replay=True,
        plan=ReplayPlan.from_events(recorded),
    )
    graph.emit(Event(id=graph.ids.event(), type=SEED, payload={}, timestamp=graph.clock.now()))
    result = asyncio.run(host.run())
    host.close()
    log.close()
    return graph, result


def test_eight_concurrent_agents_replay_byte_identically(tmp_path: Path) -> None:
    """The load-bearing test: wide fan-out, racy completions, exact replay."""
    recorded_path = tmp_path / "recorded.jsonl"
    replay_path = tmp_path / "replayed.jsonl"

    graph = record(recorded_path, delays=DELAYS)

    completion_order = [e.payload["worker"] for e in graph.events if e.type == AGENT_RESPONDED]
    dispatch_order = [e.payload["worker"] for e in graph.events if e.type == AGENT_REQUESTED]
    assert dispatch_order == WORKERS
    assert completion_order != dispatch_order, (
        "the delays did not actually reorder completions, so this test proved "
        "nothing — retune DELAYS"
    )

    _, result = replay(recorded_path, replay_path)
    assert result.cache_hits == len(WORKERS)
    assert replay_path.read_bytes() == recorded_path.read_bytes()


def test_event_log_is_a_total_order_with_intact_causality(tmp_path: Path) -> None:
    """Every event has a unique id, and no effect precedes its cause.

    Injecting from outside the loop is the thing most likely to break this, so
    it is asserted directly rather than inferred from replay passing.
    """
    graph = record(tmp_path / "run.jsonl", delays=DELAYS)
    events = graph.events

    ids = [e.id for e in events]
    assert len(ids) == len(set(ids)), "duplicate event ids"

    position = {e.id: i for i, e in enumerate(events)}
    for i, event in enumerate(events):
        if event.caused_by is not None:
            assert event.caused_by in position, f"{event.id} cites an unknown cause"
            assert position[event.caused_by] < i, (
                f"{event.id} ({event.type}) precedes its cause {event.caused_by}"
            )

    # Every dispatched agent got exactly one response, and every response is
    # attributed to a real request.
    requests = {e.id for e in events if e.type == AGENT_REQUESTED}
    responses = [e for e in events if e.type == AGENT_RESPONDED]
    assert len(responses) == len(requests) == len(WORKERS)
    assert {r.caused_by for r in responses} == requests


def test_completion_order_survives_a_different_race(tmp_path: Path) -> None:
    """Replay follows the *recorded* order, not whatever this machine does.

    The recording is made with one set of delays and replayed with the workers
    gone entirely — so if the reorder buffer were absent, the replay would
    inject in task-creation order and diverge. That is precisely the failure
    the buffer exists to prevent.
    """
    recorded_path = tmp_path / "recorded.jsonl"
    record(recorded_path, delays=DELAYS)
    recorded = read_events(recorded_path)

    recorded_workers = [e.payload["worker"] for e in recorded if e.type == AGENT_RESPONDED]
    assert recorded_workers != WORKERS, "recording did not reorder"

    graph, _ = replay(recorded_path, tmp_path / "replayed.jsonl")
    replayed_workers = [e.payload["worker"] for e in graph.events if e.type == AGENT_RESPONDED]
    assert replayed_workers == recorded_workers


def test_replay_rejects_a_log_whose_calls_do_not_correspond(tmp_path: Path) -> None:
    """A truncated recording must stall loudly, not half-replay in silence."""
    from agentgraph.host import ReplayOrderStall

    recorded_path = tmp_path / "recorded.jsonl"
    record(recorded_path, delays=DELAYS)
    recorded = read_events(recorded_path)

    # Drop the last recorded window while leaving the cache intact: every call
    # can still be served, but the last one has nowhere to go.
    full = ReplayPlan.from_events(recorded)
    truncated = ReplayPlan(groups=full.groups[:-1])
    assert len(truncated.responses) < len(full.responses)

    graph = fresh_graph()
    rt = Runtime(graph, behaviors=wide_behaviors())
    host = Host(
        rt,
        ScriptedWorker(lambda r, a: "x"),
        max_concurrency=8,
        cache=AgentCache.from_events(recorded),
        replay=True,
        plan=truncated,
    )
    graph.emit(Event(id=graph.ids.event(), type=SEED, payload={}, timestamp=graph.clock.now()))
    with pytest.raises(ReplayOrderStall):
        asyncio.run(host.run())
    host.close()


def test_concurrency_limit_is_respected(tmp_path: Path) -> None:
    """The semaphore actually bounds in-flight work.

    Worth pinning: the bound is the only thing standing between a wide fan-out
    and eight simultaneous API sessions.
    """
    peak = {"now": 0, "max": 0}

    def counting_responder(request, api):
        peak["now"] += 1
        peak["max"] = max(peak["max"], peak["now"])
        return "ok"

    class Counting(ScriptedWorker):
        async def __call__(self, request, api):
            peak["now"] += 1
            peak["max"] = max(peak["max"], peak["now"])
            try:
                await asyncio.sleep(0.02)
                return await super().__call__(request, api)
            finally:
                peak["now"] -= 1

    graph = fresh_graph()
    rt = Runtime(graph, behaviors=wide_behaviors())
    host = Host(rt, Counting(lambda r, a: "ok"), max_concurrency=3)
    graph.emit(Event(id=graph.ids.event(), type=SEED, payload={}, timestamp=graph.clock.now()))
    asyncio.run(host.run())
    host.close()

    assert peak["max"] <= 3, f"{peak['max']} workers ran at once, limit was 3"
    assert peak["max"] > 1, "nothing ran concurrently, so the bound proved nothing"


# --------------------------------------------------------------------------
# Deep queues: the condition that broke a real 9-agent run
# --------------------------------------------------------------------------


def chatty_behaviors(findings_per_worker: int) -> list:
    """A workload whose queue depth exceeds one quantum's event cap.

    Each worker publishes several findings, and every finding fans out into
    further graph writes, so settling the runtime takes multiple quanta. That
    is the regime where injecting after a *bounded* quantum rather than a
    settled one makes the log depend on arrival timing.
    """

    @behavior(name="fan_out_eight", on=[SEED])
    def fan_out_eight(event: Event, graph: Graph, ctx) -> None:
        for name in WORKERS:
            request_agent(graph, worker=name, prompt=f"survey {name}")

    @behavior(name="collect", on=[AGENT_RESPONDED])
    def collect(event: Event, graph: Graph, ctx) -> None:
        graph.add_object("report", {"worker": event.payload["worker"]})

    @behavior(name="index_finding", on=[FINDING_RECORDED])
    def index_finding(event: Event, graph: Graph, ctx) -> None:
        # Two objects per finding, so the queue outruns a 25-event quantum.
        graph.add_object("finding", {"worker": event.payload["worker"]})
        graph.add_object("finding_index", {"topic": event.payload.get("topic")})

    return [fan_out_eight, collect, index_finding]


def _chatty_responder(findings_per_worker: int):
    def responder(request, api):
        for i in range(findings_per_worker):
            api.emit_finding("survey", f"{request.worker} finding {i}")
        return f"{request.worker} done"

    return responder


def _chatty_run(path: Path, *, delays, findings_per_worker=4, recorded=None):
    graph = fresh_graph()
    log = JSONLLog(path).attach(graph)
    rt = Runtime(graph, behaviors=chatty_behaviors(findings_per_worker))
    worker = (
        ScriptedWorker(lambda r, a: (_ for _ in ()).throw(AssertionError("live call")))
        if recorded is not None
        else ScriptedWorker(_chatty_responder(findings_per_worker), delays=delays)
    )
    kwargs = {}
    if recorded is not None:
        kwargs = {
            "cache": AgentCache.from_events(recorded),
            "plan": ReplayPlan.from_events(recorded),
            "replay": True,
        }
    host = Host(rt, worker, max_concurrency=8, **kwargs)
    graph.emit(Event(id=graph.ids.event(), type=SEED, payload={}, timestamp=graph.clock.now()))
    result = asyncio.run(host.run())
    host.close()
    log.close()
    return graph, result


def test_deep_queues_replay_byte_identically(tmp_path: Path) -> None:
    """Deep-queue smoke test — NOT a regression test, despite appearances.

    Verified by reverting the fix: this passes either way. Scripted workers
    resolve too predictably to reproduce the arrival-timing asymmetry that a
    live run creates, so it proves the deep-queue path works, not that the
    quiescence fix is load-bearing.

    `test_every_injection_happens_with_the_runtime_settled` is the real
    regression test — it fails without the fix. The end-to-end proof is a live
    9-agent audit replaying byte-identically.
    """
    recorded_path, replay_path = tmp_path / "rec.jsonl", tmp_path / "rep.jsonl"
    graph, _ = _chatty_run(recorded_path, delays=DELAYS)

    # The workload must actually be deep enough to span multiple quanta,
    # otherwise this passes without exercising anything.
    assert len(graph.events) > 200, len(graph.events)

    recorded = read_events(recorded_path)
    _, result = _chatty_run(replay_path, delays={}, recorded=recorded)
    assert result.cache_hits == len(WORKERS)
    assert replay_path.read_bytes() == recorded_path.read_bytes()


def test_every_injection_happens_with_the_runtime_settled(tmp_path: Path) -> None:
    """The invariant the fix rests on, asserted directly.

    Between the runtime's last event and each injection there must be a
    `runtime.idle` marker: the graph is quiescent whenever the host writes.
    """
    graph, _ = _chatty_run(tmp_path / "run.jsonl", delays=DELAYS)
    from agentgraph.replay import INJECTED_TYPES

    events = graph.events
    for i, event in enumerate(events):
        if event.type not in INJECTED_TYPES:
            continue
        if i and events[i - 1].type in INJECTED_TYPES:
            continue  # mid-group; only the first of a run opens a window
        preceding = [e.type for e in events[:i] if e.type not in INJECTED_TYPES]
        assert preceding and preceding[-1] == "runtime.idle", (
            f"injection {event.id} ({event.type}) landed while the runtime was "
            f"still busy; last runtime event was {preceding[-1] if preceding else None}"
        )


# --------------------------------------------------------------------------
# Interrupt and resume
# --------------------------------------------------------------------------


def test_an_interrupted_run_resumes_without_repeating_paid_work(
    tmp_path: Path,
) -> None:
    """Stop a run partway, then continue it from the log alone.

    Resume is replay-then-continue: the log already holds every completed
    agent, so those are served from cache for nothing, and only the agents that
    never finished actually run. That is the payoff of treating the log as the
    state rather than as a transcript — there is no separate checkpoint format,
    and nothing to keep in sync with it.
    """
    calls: list[str] = []

    def responder(request, api):
        calls.append(request.worker)
        api.emit_finding("survey", f"{request.worker} reporting")
        return f"{request.worker} done"

    log_path = tmp_path / "interrupted.jsonl"
    graph = fresh_graph()
    log = JSONLLog(log_path).attach(graph)
    rt = Runtime(graph, behaviors=wide_behaviors())
    host = Host(rt, ScriptedWorker(responder, delays=DELAYS), max_concurrency=8)
    graph.emit(Event(id=graph.ids.event(), type=SEED, payload={}, timestamp=graph.clock.now()))
    # Interrupt: stop as soon as three agents have landed.
    interrupted = asyncio.run(
        host.run(until=lambda g: sum(1 for e in g.events if e.type == AGENT_RESPONDED) >= 3)
    )
    host.close()
    log.close()

    assert interrupted.stopped_reason == "until"
    finished_first = [e.payload["worker"] for e in graph.events if e.type == AGENT_RESPONDED]
    # At least the three that tripped `until`, plus any that had already
    # landed: finalization records everything paid for, never fewer.
    assert 3 <= len(finished_first) < len(WORKERS), finished_first
    assert set(finished_first) <= set(calls), "a response with no call behind it"
    first_pass_calls = list(calls)

    # --- resume from the log, nothing else ---
    calls.clear()
    recorded = read_events(log_path)
    resume_path = tmp_path / "resumed.jsonl"
    graph2 = fresh_graph()
    log2 = JSONLLog(resume_path).attach(graph2)
    rt2 = Runtime(graph2, behaviors=wide_behaviors())
    host2 = Host(
        rt2,
        ScriptedWorker(responder, delays={}),
        max_concurrency=8,
        cache=AgentCache.from_events(recorded),
        plan=ReplayPlan.from_events(recorded),
        resume=True,
    )
    graph2.emit(Event(id=graph2.ids.event(), type=SEED, payload={}, timestamp=graph2.clock.now()))
    result = asyncio.run(host2.run())
    host2.close()
    log2.close()

    # Everything finishes, and the three that already had lands for free.
    assert result.agents_dispatched == len(WORKERS)
    assert result.cache_hits == len(finished_first), result
    assert result.resumed_live == len(WORKERS) - len(finished_first), result

    # The decisive assertion: no completed agent was paid for twice.
    assert not set(calls) & set(finished_first), (
        f"resume re-ran already-completed work: {set(calls) & set(finished_first)}"
    )
    assert sorted(calls) == sorted(w for w in WORKERS if w not in finished_first)

    # Resume preserves *work*, not *layout*. The agents that never finished run
    # live and publish as they go, so their writes interleave with the replayed
    # ones and every id after the first live write shifts. Only a pure replay
    # reproduces a log byte for byte; demanding that here would forbid resume
    # from making progress at all. What must hold is that nothing recorded is
    # lost: every response in the interrupted log reappears, attributed to the
    # same worker, carrying the same output.
    recorded_responses = {
        e.payload["worker"]: e.payload["output"] for e in recorded if e.type == AGENT_RESPONDED
    }
    resumed_responses = {
        e.payload["worker"]: e.payload["output"] for e in graph2.events if e.type == AGENT_RESPONDED
    }
    for worker, output in recorded_responses.items():
        assert resumed_responses[worker] == output, f"{worker}'s recorded answer changed on resume"

    # The finished graph is complete: every worker reported exactly once.
    responded = [e.payload["worker"] for e in graph2.events if e.type == AGENT_RESPONDED]
    assert sorted(responded) == sorted(WORKERS)
    assert len(first_pass_calls) >= 3
