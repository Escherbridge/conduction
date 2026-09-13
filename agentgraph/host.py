"""The host loop: run_quantum, dispatch, inject.

This is the whole architecture in one file. The runtime drains a bounded
quantum synchronously; anything it queued as `agent.requested` is picked up by
the host, run concurrently *outside* the loop, and injected back as
`agent.responded`. The runtime's contract is satisfied rather than violated —
it still processes one event at a time, it just never blocks on the work.

Three properties make this work, and none of them is obvious:

  * **asyncio, not threads.** The graph has exactly one writer: this loop.
    `run_quantum()` is synchronous and so cannot interleave with an injection,
    and worker MCP callbacks only run at `await` points. Concurrency without a
    second writer means the single-threaded contract holds unmodified.

  * **Ordering is decided at request time, not completion time.** The
    occurrence index of each agent call is assigned when the request is made —
    on this single-threaded loop — so it is stable no matter what order workers
    finish in.

  * **The runtime is drained to quiescence before every injection.** This is
    the invariant that makes the log independent of worker timing, and it is
    load-bearing. A quantum is bounded (25 events by default), so a busy graph
    needs several to settle. If the host injected after a *bounded* quantum
    rather than after a settled one, the choice between "run another quantum"
    and "inject now" would depend on whether a completion happened to have
    arrived yet — which is exactly what differs between a live run and an
    instant replay. Draining first removes the choice: the graph is quiescent
    at every injection point, so `runtime.idle` markers land in the same places
    both times.

    Observed the hard way: without this, a 9-agent live run replayed with one
    extra `runtime.idle` in a 51-event stretch and nothing else wrong.
"""

from __future__ import annotations

import asyncio
from collections import deque
from pathlib import Path
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any, Callable, Optional

from activegraph import Event, Graph, Runtime

from agentgraph.agentcache import AgentCache
from agentgraph.claims import (
    ClaimLedger,
    ClaimViolation,
    CommandViolation,
    claim_rejection_reason,
    make_claim_hook,
)
from agentgraph.context import GraphContext
from agentgraph.dispatcher import (
    AgentRequest,
    AgentResponse,
    ClaudeAgentWorker,
    Dispatcher,
    Worker,
)
from agentgraph.replay import ReplayPlan
from agentgraph.events import (
    AGENT_REQUESTED,
    AGENT_RESPONDED,
    CLAIM_GRANTED,
    CLAIM_REJECTED,
    CLAIM_RELEASED,
    CLAIM_VIOLATED,
    COMMAND_VIOLATED,
    WORKER_EMITTABLE,
    emit,
)
from agentgraph.transcript import TranscriptWriter
from agentgraph.workerapi import WorkerAPI


def request_agent(
    graph: Graph,
    *,
    worker: str,
    prompt: str,
    caused_by: Optional[str] = None,
    actor: Optional[str] = None,
    **request_kwargs: Any,
) -> Event:
    """Emit an `agent.requested` from inside a behavior.

    Behaviors call this; the host picks the event up on the next drain. The
    behavior never awaits, never blocks, and never learns that concurrency
    exists — which is exactly the separation the design depends on.

    `caused_by` and `actor` are only used when emitting against a raw `Graph`
    (a seed event from a script, say). Inside a behavior the runtime stamps
    both from the invocation, and these are ignored.
    """
    request = AgentRequest(worker=worker, prompt=prompt, **request_kwargs)
    return emit(
        graph,
        AGENT_REQUESTED,
        request.to_payload(),
        actor=actor or worker,
        caused_by=caused_by,
    )


@dataclass
class HostResult:
    """What one `run()` did. Everything here is derived from the log."""

    quanta: int = 0
    events_processed: int = 0
    agents_dispatched: int = 0
    agents_completed: int = 0
    agents_failed: int = 0
    cache_hits: int = 0
    #: Resume only: calls that had no recording and so ran live.
    resumed_live: int = 0
    total_cost_usd: Decimal = Decimal("0")
    idle: bool = False
    stopped_reason: str = ""
    violations: list[ClaimViolation] = field(default_factory=list)
    #: Shell commands refused by the destructive-command policy.
    command_violations: list[CommandViolation] = field(default_factory=list)


@dataclass
class _Completion:
    """One finished agent call waiting for its turn to be injected."""

    request: AgentRequest
    occurrence: int
    response: AgentResponse
    request_event: Event


class Host:
    """Drives a `Runtime` and an agent pool against one graph.

    Live and replay are the same code path with one flag. In replay the
    dispatcher serves from the recorded log or fails loud, and completions are
    released through a reorder buffer in recorded order rather than as they
    finish — the entire delta between permissive and strict replay (plan
    section 3.3).
    """

    def __init__(
        self,
        runtime: Runtime,
        worker: Optional[Worker] = None,
        *,
        max_concurrency: int = 4,
        cache: Optional[AgentCache] = None,
        replay: bool = False,
        resume: bool = False,
        plan: Optional[ReplayPlan] = None,
        claim_root: Optional[str] = None,
        transcript_dir: Optional[str] = None,
    ) -> None:
        self.runtime = runtime
        self.graph = runtime.graph
        self.dispatcher = Dispatcher(
            worker if worker is not None else ClaudeAgentWorker(),
            max_concurrency=max_concurrency,
            cache=cache,
            replay=replay or resume,
            resume=resume,
        )
        self.replay = replay or resume
        self.resume = resume
        self._transcript_dir = Path(transcript_dir) if transcript_dir else None
        self.ledger = ClaimLedger(root=claim_root)
        self.context = GraphContext(self.graph, self.ledger)

        #: Pending `agent.requested` events, collected by a graph listener so
        #: the host sees them the instant a behavior emits one.
        self._pending: list[Event] = []
        #: Finished calls awaiting injection, in arrival (race) order.
        self._arrived: deque[_Completion] = deque()
        #: Cleared while finalizing, so a dying run records what it already
        #: bought without starting anything new.
        self._spawning = True
        self._result = HostResult()

        # --- replay ordering (plan section 3.3, generalized in replay.py) ---
        self._plan = plan
        self._group_at = 0

        self.graph.add_listener(self._collect)

    # ---- collection ----

    def _collect(self, event: Event) -> None:
        if event.type == AGENT_REQUESTED and event.actor != "host":
            self._pending.append(event)

    def close(self) -> None:
        self.graph._remove_listener(self._collect)  # noqa: SLF001

    def __enter__(self) -> "Host":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    # ---- the loop ----

    async def run(
        self,
        *,
        max_quanta: int = 10_000,
        max_queue_events: int = 25,
        max_seconds: float = 0.25,
        until: Optional[Callable[[Graph], bool]] = None,
    ) -> HostResult:
        """Interleave runtime quanta with agent dispatch until nothing is left.

        Terminates when the runtime is idle with nothing pending, nothing in
        flight, and nothing waiting to be injected — the only state in which no
        further event can appear without an external write.
        """
        self._result = HostResult()
        tasks: set[asyncio.Task[Any]] = set()

        try:
            for _ in range(max_quanta):
                # Settle the runtime completely. A quantum is event-bounded, so
                # a busy graph takes several; injecting after a bounded quantum
                # would make the loop's shape depend on arrival timing.
                while True:
                    self._result.quanta += 1
                    quantum = self.runtime.run_quantum(
                        max_queue_events=max_queue_events, max_seconds=max_seconds
                    )
                    self._result.events_processed += quantum.queue_events_processed
                    for event in self._drain_pending():
                        if self._spawning:
                            tasks.add(self._spawn(event))
                    if quantum.idle or quantum.budget_exhausted:
                        break

                if until is not None and until(self.graph):
                    self._result.stopped_reason = "until"
                    break

                if self._inject_one():
                    continue

                tasks = {t for t in tasks if not t.done()}
                if not tasks:
                    if not self._pending and not self._arrived:
                        self._result.idle = True
                        self._result.stopped_reason = "idle"
                        break
                    continue

                done, still_running = await asyncio.wait(
                    tasks, return_when=asyncio.FIRST_COMPLETED
                )
                tasks = set(still_running)
                for task in done:
                    task.result()  # re-raise worker/host bugs, never swallow
            else:
                self._result.stopped_reason = "max_quanta"
        finally:
            for task in tasks:
                if not task.done():
                    task.cancel()
            if tasks:
                await asyncio.gather(*tasks, return_exceptions=True)
            self._flush_arrived(
                max_queue_events=max_queue_events, max_seconds=max_seconds
            )

        if self._arrived and self._result.stopped_reason == "idle":
            raise ReplayOrderStall(
                f"{len(self._arrived)} completion(s) never reached their recorded "
                f"position. The recorded log and this run's agent calls do not "
                f"correspond."
            )
        if (
            self._plan is not None
            and not self.resume
            and self._group_at < len(self._plan.groups)
        ):
            remaining = sum(
                len(g) for g in self._plan.groups[self._group_at :]
            )
            raise ReplayOrderStall(
                f"replay ended with {remaining} recorded injection(s) unreplayed "
                f"across {len(self._plan.groups) - self._group_at} window(s); "
                f"this run stopped making the calls the recording contains."
            )
        return self._result

    def _flush_arrived(self, *, max_queue_events: int, max_seconds: float) -> None:
        """Record every completion that already landed, then settle.

        Called on every exit, including an interrupted one. An agent that
        finished has been paid for whether or not the loop was still running to
        collect it, so leaving its response out of the log would make a resume
        buy the same answer twice. New requests raised by the behaviors these
        injections wake are deliberately *not* dispatched — the run is ending —
        but their `agent.requested` events stay in the log, which is exactly
        what tells a resume to run them.
        """
        self._spawning = False
        try:
            while self._inject_one():
                while True:
                    quantum = self.runtime.run_quantum(
                        max_queue_events=max_queue_events, max_seconds=max_seconds
                    )
                    self._drain_pending()
                    if quantum.idle or quantum.budget_exhausted:
                        break
        finally:
            self._spawning = True

    def _drain_pending(self) -> list[Event]:
        events, self._pending = self._pending, []
        return events

    def _spawn(self, event: Event) -> asyncio.Task[Any]:
        request = self._request_from(event)
        occurrence = self.dispatcher.next_occurrence(request.args_hash)
        # Stamp the occurrence onto the request event so a replay of this log
        # can pair request to response without re-deriving dispatch order.
        event.payload["occurrence"] = occurrence
        self._result.agents_dispatched += 1
        return asyncio.create_task(
            self._run_one(request, occurrence, event), name=f"agent:{request.worker}"
        )

    async def _run_one(
        self, request: AgentRequest, occurrence: int, request_event: Event
    ) -> None:
        transcript = None
        if self._transcript_dir is not None:
            suffix = f"-{occurrence}" if occurrence else ""
            transcript = TranscriptWriter(
                self._transcript_dir / f"{request.worker}{suffix}.md"
            )
        api = WorkerAPI(
            host=self,
            request=request,
            request_event=request_event,
            transcript=transcript,
        )
        response = await self.dispatcher.run(request, occurrence, api)
        self._arrived.append(_Completion(request, occurrence, response, request_event))

    # ---- injection ----

    def _inject_one(self) -> bool:
        """Put at most one window of injections on the graph.

        Live runs take arrivals in race order, one per iteration — that race
        *is* the recording. Replay instead follows `ReplayPlan`: it re-emits a
        whole recorded window at once, so the quantum boundaries land exactly
        where they did live.
        """
        if self._plan is None:
            if not self._arrived:
                return False
            self._inject(self._arrived.popleft())
            return True
        return self._inject_planned_group()

    def _inject_planned_group(self) -> bool:
        assert self._plan is not None
        if self._group_at >= len(self._plan.groups):
            # The recorded prefix is exhausted. Under replay that is an error;
            # under resume it is the whole point — everything past here is new
            # work, injected in live arrival order because no recording of it
            # exists to follow.
            if not self._arrived:
                return False
            if not self.resume:
                raise ReplayOrderStall(
                    f"{len(self._arrived)} agent call(s) completed after the "
                    f"recorded log ended; this run made calls the recording "
                    f"does not contain."
                )
            self._inject(self._arrived.popleft())
            return True

        group = self._plan.groups[self._group_at]

        # A window can only be replayed once every response it contains has a
        # completion in hand — otherwise the graph would show a result before
        # the call that produced it finished.
        needed = {p.caused_by for p in group if p.is_response}
        available = {c.request_event.id for c in self._arrived}
        if not needed.issubset(available):
            return False

        self._group_at += 1
        for planned in group:
            if planned.is_response:
                self._inject(self._take_arrived(planned.caused_by))
            else:
                self._replay_side_effect(planned)
        return True

    def _take_arrived(self, request_event_id: Optional[str]) -> "_Completion":
        for i, completion in enumerate(self._arrived):
            if completion.request_event.id == request_event_id:
                del self._arrived[i]
                return completion
        raise ReplayOrderStall(
            f"the recorded log expects a response for request "
            f"{request_event_id!r}, which this run never made"
        )

    def _inject(self, completion: "_Completion") -> None:
        request, response = completion.request, completion.response
        if self.resume and not response.cache_hit:
            self._result.resumed_live += 1
        payload = response.to_payload()
        payload["worker"] = request.worker
        emit(
            self.graph,
            AGENT_RESPONDED,
            payload,
            actor="host",
            caused_by=completion.request_event.id,
        )
        if response.error:
            self._result.agents_failed += 1
        else:
            self._result.agents_completed += 1
        if response.cache_hit:
            self._result.cache_hits += 1
        self._result.total_cost_usd += response.cost_usd

    def _replay_side_effect(self, planned: Any) -> None:
        """Re-emit one recorded worker write.

        On replay the worker never executes, so its mid-run `graph_emit` and
        `graph_claim` writes would simply vanish. The ledger is stepped forward
        alongside them, because a later `graph_query` in the same replayed run
        must see the same claims the live run saw.
        """
        payload = dict(planned.payload)
        if planned.type == CLAIM_GRANTED:
            self.ledger.grant(payload["worker"], payload.get("paths", []))
        elif planned.type == CLAIM_RELEASED:
            self.ledger.release(payload["worker"], payload.get("paths"))
        emit(
            self.graph,
            planned.type,
            payload,
            actor=planned.actor,
            caused_by=planned.caused_by,
        )

    # ---- worker-facing writes, reached through `WorkerAPI` ----

    def worker_emit(
        self,
        worker: str,
        type_: str,
        payload: dict[str, Any],
        request_event: Event,
    ) -> Event:
        """A worker's write onto the blackboard.

        Caused by the worker's own `agent.requested`, which is what makes the
        write harvestable as a side effect on replay and walkable by
        `causal_chain()` afterwards.
        """
        if type_ not in WORKER_EMITTABLE:
            raise ValueError(
                f"workers may emit {sorted(WORKER_EMITTABLE)}, not {type_!r}"
            )
        body = dict(payload)
        body["worker"] = worker
        return emit(self.graph, type_, body, actor=worker, caused_by=request_event.id)

    @staticmethod
    def _declared_owns(request_event: Event) -> list[str]:
        """The `owns` partition the requesting agent was dispatched with."""
        meta = request_event.payload.get("meta") or {}
        owns = meta.get("owns") if isinstance(meta, dict) else None
        if isinstance(owns, str):
            owns = [owns]
        return [o for o in (owns or []) if isinstance(o, str) and o.strip()]

    def _invalid_claims(
        self, paths: list[str], request_event: Event
    ) -> list[dict[str, str]]:
        """`(path, reason)` for every path that may not be claimed at all."""
        owns = self._declared_owns(request_event)
        out: list[dict[str, str]] = []
        for path in paths:
            reason = claim_rejection_reason(
                path, root=self.ledger.root, owns=owns or None
            )
            if reason is not None:
                out.append({"path": path, "reason": reason})
        return out

    def worker_claim(
        self, worker: str, paths: list[str], request_event: Event
    ) -> dict[str, Any]:
        """Adjudicate a claim. Runs on the host loop, so it is race-free."""
        invalid = self._invalid_claims(paths, request_event)
        if invalid:
            emit(
                self.graph,
                CLAIM_REJECTED,
                {
                    "worker": worker,
                    "paths": list(paths),
                    "conflicts": [],
                    "invalid": invalid,
                },
                actor=worker,
                caused_by=request_event.id,
            )
            return {"granted": False, "conflicts": [], "invalid": invalid}
        conflicts = self.ledger.conflicts(worker, paths)
        if conflicts:
            detail = [{"path": p, "owner": o} for p, o in conflicts]
            emit(
                self.graph,
                CLAIM_REJECTED,
                {"worker": worker, "paths": list(paths), "conflicts": detail},
                actor=worker,
                caused_by=request_event.id,
            )
            return {"granted": False, "conflicts": detail}
        granted = self.ledger.grant(worker, paths)
        emit(
            self.graph,
            CLAIM_GRANTED,
            {"worker": worker, "paths": granted},
            actor=worker,
            caused_by=request_event.id,
        )
        return {"granted": True, "paths": granted}

    def worker_release(
        self, worker: str, paths: Optional[list[str]], request_event: Event
    ) -> dict[str, Any]:
        released = self.ledger.release(worker, paths)
        emit(
            self.graph,
            CLAIM_RELEASED,
            {"worker": worker, "paths": released},
            actor=worker,
            caused_by=request_event.id,
        )
        return {"released": released}

    def record_violation(self, violation: ClaimViolation, request_event: Event) -> None:
        """A refused write is a fact, so it goes on the graph."""
        self._result.violations.append(violation)
        emit(
            self.graph,
            CLAIM_VIOLATED,
            {
                "worker": violation.worker,
                "tool_name": violation.tool_name,
                "path": violation.path,
                "owner": violation.owner,
            },
            actor="host",
            caused_by=request_event.id,
        )

    def record_command_violation(
        self, violation: CommandViolation, request_event: Event
    ) -> None:
        """A refused command is a fact, so it goes on the graph."""
        self._result.command_violations.append(violation)
        emit(
            self.graph,
            COMMAND_VIOLATED,
            {
                "worker": violation.worker,
                "tool_name": violation.tool_name,
                "command": violation.command,
                "pattern": violation.pattern,
            },
            actor="host",
            caused_by=request_event.id,
        )

    def claim_hook_for(self, worker: str, request_event: Event) -> Any:
        """The `PreToolUse` hook for one worker, wired to record violations."""
        return make_claim_hook(
            worker,
            self.ledger,
            on_violation=lambda v: self.record_violation(v, request_event),
            on_command_violation=lambda v: self.record_command_violation(
                v, request_event
            ),
        )

    # ---- helpers ----

    @staticmethod
    def _request_from(event: Event) -> AgentRequest:
        """Rebuild the `AgentRequest` an `agent.requested` event describes.

        The event is the source of truth, not any object the behavior kept —
        which is what lets a replayed log reconstruct requests it never saw
        built.
        """
        identity = event.payload["identity"]
        return AgentRequest(
            worker=event.payload["worker"],
            prompt=identity["prompt"],
            model=identity["model"],
            system_prompt=identity["system_prompt"],
            allowed_tools=tuple(identity["allowed_tools"]),
            disallowed_tools=tuple(identity["disallowed_tools"]),
            mcp_server_names=tuple(identity["mcp_servers"]),
            cwd=identity["cwd"],
            max_turns=identity["max_turns"],
            permission_mode=identity["permission_mode"],
            setting_sources=(
                None
                if identity["setting_sources"] is None
                else tuple(identity["setting_sources"])
            ),
            timeout_seconds=float(event.payload.get("timeout_seconds", 600.0)),
            config_fingerprint=identity["config_fingerprint"],
            # Absent in logs recorded before the field existed; .get keeps them
            # rebuilding to the same hash they were recorded under.
            skills=(
                None
                if identity.get("skills") is None
                else tuple(identity["skills"])
            ),
            meta=dict(event.payload.get("meta") or {}),
        )


class ReplayOrderStall(RuntimeError):
    """Replay held completions that never reached their recorded position."""


__all__ = ["Host", "HostResult", "ReplayOrderStall", "request_agent"]
