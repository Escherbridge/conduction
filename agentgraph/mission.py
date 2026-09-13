"""Mission — the declarative shim over Host, Runtime, and the behaviors.

Before this module, every workflow restated the same ~500 lines of machinery:
a dispatch behavior, report objects, join counting, a synthesis trigger, a
finding index, run/resume/replay plumbing, and a system prompt that hauled
every reference an agent might need into every call. The shim owns that once.

A mission file declares agents and briefs; everything heavy moves out of the
prompt and onto the board:

  * **References are findings, not prompt text.** `references={"checklist":
    ...}` seeds each document as a `finding` with topic `ref/<name>`; an agent
    that needs it pulls it with `graph_query(view='findings',
    topic='ref/checklist')`. Agents self-retrieve context instead of paying
    for it in every request — and the identity hash stops churning when a
    reference is reworded, because the prompt no longer contains it.

  * **The system prompt is the coordination contract only** (~12 lines):
    board first, publish as you go, claim before writing, budget the turns.

Facts and references are re-seeded on resume and replay, so they must be
deterministic for byte-identical replay — compute them from inputs, not from
wall-clock or randomness.
"""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterable, Optional

from activegraph import Clock, FrozenClock, Graph, IDGen, Runtime, behavior
from activegraph.core.event import Event

from agentgraph.agentcache import AgentCache
from agentgraph.dispatcher import (
    AgentRequest,
    AgentResponse,
    ClaudeAgentWorker,
    Worker,
    denied_tools,
)
from agentgraph.events import AGENT_RESPONDED, FINDING_RECORDED, MISSION_COMPLETED, emit
from agentgraph.host import Host, HostResult, request_agent
from agentgraph.log import JSONLLog, read_events
from agentgraph.mcp_tools import GRAPH_TOOL_NAMES, SERVER_NAME
from agentgraph.replay import ReplayPlan

MISSION_STARTED = "mission.started"

#: The gate runs as a pseudo-agent under the worker name `host`, so its finding
#: is attributed to the host rather than to any agent (`worker_emit` stamps the
#: request's worker onto the finding).
GATE_WORKER = "host"
GATE_TOPIC = "gate"
GATE_PROMPT = "host-side verification gate"
#: A gate may build and test; it is not an LLM call and must not be cut short
#: at the agent default.
GATE_TIMEOUT_SECONDS = 1800.0

READ_TOOLS = ("Read", "Grep", "Glob")
EDIT_TOOLS = READ_TOOLS + ("Edit", "Write")

SYSTEM_PROMPT = (
    "You are {worker}, one of {n} agents on one mission, coordinating through "
    "a shared graph.\n"
    "1. graph_query(view='findings') FIRST — seeded facts and the other "
    "agents' findings are already there. Build on them; never redo them.\n"
    "{refs_rule}"
    "2. graph_emit each finding the moment you confirm it. Published work "
    "survives if you are cut off; unpublished work is lost.\n"
    "3. graph_claim any file before writing it — unclaimed writes are "
    "refused. If a claim is refused, another agent owns the file: report, "
    "don't edit.\n"
    "4. As work lands, graph_emit topic='result' whose detail is JSON "
    '{{"files_changed": [...], "verification": "<how you proved it>" or null, '
    '"done": true|false}}, and re-emit it whenever it changes. Running out of '
    "turns then costs turns, not the work.\n"
    "You have {max_turns} turns; every tool call spends one. Verify before "
    "reporting and cite path:line. A clean area is a valid result. End with a "
    "3-line summary."
)

REFS_RULE = (
    "   Reference material is on the board too: "
    "graph_query(view='findings', topic='ref/<name>'). Yours: {names}.\n"
)

SYNTHESIS_PROMPT = (
    "Every agent has finished. Read all findings with "
    "graph_query(view='findings', include_own=true), then: {brief}\n"
    "Judge the findings — rank, note convergence, name the gaps. Do not "
    "restate them as a list."
)


@dataclass(frozen=True)
class AgentSpec:
    """One agent: a name, a brief, and what it may touch. The shim supplies
    everything else."""

    name: str
    brief: str
    tools: tuple[str, ...] = READ_TOOLS
    refs: tuple[str, ...] = ()
    #: Skills loaded into this worker's session — how a subagent's capacity is
    #: extended beyond its tools. Prefer an explicit list over
    #: setting_sources=("user",), which drags every plugin and hook along.
    skills: tuple[str, ...] = ()
    setting_sources: Optional[tuple[str, ...]] = None
    model: Optional[str] = None
    max_turns: Optional[int] = None
    meta: dict[str, Any] = field(default_factory=dict)
    #: Paths this agent may claim, relative to `claim_root` or absolute. Rides
    #: in `meta` (and so in the request event, not the identity hash) because it
    #: is host-side routing policy: the claim ledger reads it, the model never
    #: sees it, and editing a partition must not invalidate a recorded answer.
    owns: tuple[str, ...] = ()


@dataclass(frozen=True)
class AgentReport:
    """One agent's outcome as the board recorded it."""

    worker: str
    ok: bool
    output: Any = None
    error: Optional[dict[str, Any]] = None
    cost_usd: Optional[str] = None
    partial_output: Optional[str] = None


@dataclass(frozen=True)
class GateContext:
    """Everything a host-side gate is allowed to look at."""

    cwd: str
    claim_root: str
    reports: dict[str, AgentReport]
    findings: list[dict[str, Any]]


@dataclass(frozen=True)
class GateResult:
    """A gate's verdict plus the per-check evidence behind it."""

    passed: bool
    checks: list[dict[str, Any]] = field(default_factory=list)


@dataclass
class MissionResult:
    """What a mission produced, already projected out of the graph."""

    host: HostResult
    graph: Graph
    findings: list[dict[str, Any]]
    outputs: dict[str, Any]
    synthesis: Optional[str]
    log_path: Path
    #: True when any agent failed, never reported, or the gate refused.
    failed: bool = False
    #: The gate verdict, rebuilt from its finding (None when no gate ran).
    gate: Optional[GateResult] = None
    #: Per-agent outcomes, keyed by worker name.
    reports: dict[str, AgentReport] = field(default_factory=dict)


class Mission:
    """Declare agents; run, interrupt, resume, and replay them.

    `facts` seeds deterministic host-computed knowledge; `references` seeds
    retrievable documents. Both land on the board before any agent starts.
    """

    def __init__(
        self,
        name: str,
        agents: Iterable[AgentSpec],
        *,
        synthesis: Optional[str] = None,
        model: str = "claude-sonnet-4-5-20250929",
        max_turns: int = 20,
        cwd: Optional[str] = None,
        claim_root: Optional[str] = None,
        facts: Iterable[tuple[str, str, Any]] = (),
        references: Optional[dict[str, str]] = None,
        max_concurrency: int = 4,
        transcript_dir: Optional[str] = None,
        clock: Optional[Clock] = None,
        gate: Optional[Callable[[GateContext], GateResult]] = None,
    ) -> None:
        self.name = name
        self.agents = list(agents)
        self.synthesis = synthesis
        self.model = model
        self.max_turns = max_turns
        self.cwd = cwd
        self.claim_root = claim_root
        self.facts = list(facts)
        self.references = dict(references or {})
        self.max_concurrency = max_concurrency
        self.transcript_dir = transcript_dir
        self.clock = clock or FrozenClock("2026-08-21T00:00:00Z")
        #: Deterministic host code run after every agent report lands and before
        #: synthesis is dispatched. See `_GateRunner` for why it is dispatched
        #: as a pseudo-agent rather than called from a behavior.
        self.gate = gate

    # ---- public entry points ----

    def run(
        self,
        log_path: str | Path,
        *,
        worker: Optional[Worker] = None,
        interrupt_after: Optional[int] = None,
        stop_when: Optional[Callable[[], bool]] = None,
    ) -> MissionResult:
        return self._execute(
            Path(log_path),
            worker=worker,
            interrupt_after=interrupt_after,
            stop_when=stop_when,
        )

    def resume(
        self,
        recorded_log: str | Path,
        log_path: str | Path,
        *,
        worker: Optional[Worker] = None,
        stop_when: Optional[Callable[[], bool]] = None,
    ) -> MissionResult:
        """Continue an interrupted run: completed calls are served from the
        recorded log for nothing, missing ones run live."""
        recorded = read_events(recorded_log)
        return self._execute(
            Path(log_path),
            worker=worker,
            cache=AgentCache.from_events(recorded),
            plan=ReplayPlan.from_events(recorded),
            resume=True,
            stop_when=stop_when,
        )

    def replay(self, recorded_log: str | Path, log_path: str | Path) -> MissionResult:
        """Re-execute against the recording only. No worker ever runs."""

        async def must_not_run(request: AgentRequest, api: Any) -> Any:
            raise AssertionError("replay must never invoke a live worker")

        recorded = read_events(recorded_log)
        return self._execute(
            Path(log_path),
            worker=must_not_run,
            cache=AgentCache.from_events(recorded),
            plan=ReplayPlan.from_events(recorded),
            replay=True,
        )

    # ---- assembly ----

    def _system_prompt(self, spec: AgentSpec) -> str:
        refs_rule = (
            REFS_RULE.format(names=", ".join(spec.refs)) if spec.refs else ""
        )
        return SYSTEM_PROMPT.format(
            worker=spec.name,
            n=len(self.agents),
            max_turns=spec.max_turns or self.max_turns,
            refs_rule=refs_rule,
        )

    def _request_kwargs(self, tools: tuple[str, ...]) -> dict[str, Any]:
        allowed = tuple(tools) + GRAPH_TOOL_NAMES
        kwargs: dict[str, Any] = {
            "mcp_server_names": (SERVER_NAME,),
            "allowed_tools": allowed,
            # `allowed_tools` only auto-approves; every unlisted built-in stays
            # reachable under bypassPermissions, so the complement is what makes
            # `AgentSpec.tools` a boundary. The mode stays bypass on purpose: a
            # headless worker cannot answer a permission prompt, so switching
            # modes hangs it instead of restricting it.
            "disallowed_tools": denied_tools(allowed),
            "permission_mode": "bypassPermissions",
        }
        if self.cwd is not None:
            kwargs["cwd"] = self.cwd
        return kwargs

    def _behaviors(self) -> list:
        mission = self
        agent_names = {spec.name for spec in self.agents}

        def dispatch_synthesis(graph: Graph) -> None:
            if mission.synthesis is None:
                return
            request_agent(
                graph,
                worker="synthesizer",
                prompt=SYNTHESIS_PROMPT.format(brief=mission.synthesis),
                system_prompt="You judge and prioritize; you do not re-report.",
                model=mission.model,
                max_turns=mission.max_turns,
                timeout_seconds=900,
                meta={"wave": 2},
                **mission._request_kwargs(READ_TOOLS),
            )

        @behavior(name=f"{self.name}:dispatch", on=[MISSION_STARTED])
        def dispatch(event: Event, graph: Graph, ctx: Any) -> None:
            for spec in mission.agents:
                request_agent(
                    graph,
                    worker=spec.name,
                    prompt=spec.brief,
                    system_prompt=mission._system_prompt(spec),
                    model=spec.model or mission.model,
                    max_turns=spec.max_turns or mission.max_turns,
                    timeout_seconds=1200,
                    skills=spec.skills or None,
                    setting_sources=spec.setting_sources,
                    meta={**spec.meta, "wave": 1, "owns": list(spec.owns)},
                    **mission._request_kwargs(spec.tools),
                )

        @behavior(name=f"{self.name}:record", on=[AGENT_RESPONDED])
        def record(event: Event, graph: Graph, ctx: Any) -> None:
            worker = event.payload.get("worker")
            if worker in agent_names:
                error = event.payload.get("error")
                graph.add_object(
                    "agent_report",
                    {
                        "worker": worker,
                        "ok": error is None,
                        "output": event.payload.get("output"),
                        "error": error,
                        "cost_usd": event.payload.get("cost_usd"),
                        "partial_output": (error or {}).get("partial_output"),
                    },
                )
            elif worker == "synthesizer":
                graph.add_object(
                    "synthesis",
                    {
                        "output": event.payload.get("output"),
                        "error": event.payload.get("error"),
                    },
                )

        @behavior(name=f"{self.name}:join", on=[AGENT_RESPONDED])
        def join(event: Event, graph: Graph, ctx: Any) -> None:
            # Wave 2 dispatches itself when the board says wave 1 is complete.
            # `record` runs first (registration order), so exactly one response
            # completes the count and only that invocation dispatches.
            if event.payload.get("worker") not in agent_names:
                return
            if len(ctx.view.objects(type="agent_report")) != len(mission.agents):
                return
            if mission.gate is not None:
                # The gate is dispatched, not called: host code that runs
                # subprocesses must not execute inside a behavior, because
                # behaviors re-run on replay. As an agent call it is cached,
                # served from the log on replay, and its finding is replayed as
                # a recorded side effect like any other worker write.
                request_agent(
                    graph,
                    worker=GATE_WORKER,
                    prompt=GATE_PROMPT,
                    timeout_seconds=GATE_TIMEOUT_SECONDS,
                    meta={"wave": 1, "kind": "gate"},
                )
                return
            dispatch_synthesis(graph)

        @behavior(name=f"{self.name}:gated", on=[AGENT_RESPONDED])
        def gated(event: Event, graph: Graph, ctx: Any) -> None:
            # Synthesis waits for the gate's verdict, pass or fail: a failed
            # wave still gets judged, it just does not get called completed.
            if event.payload.get("worker") != GATE_WORKER:
                return
            dispatch_synthesis(graph)

        @behavior(name=f"{self.name}:index", on=[FINDING_RECORDED])
        def index(event: Event, graph: Graph, ctx: Any) -> None:
            graph.add_object(
                "finding",
                {
                    "worker": event.payload.get("worker"),
                    "topic": event.payload.get("topic"),
                    "summary": event.payload.get("summary"),
                },
            )

        return [dispatch, record, join, gated, index]

    def _seed(self, graph: Graph) -> None:
        seed = emit(graph, MISSION_STARTED, {"mission": self.name}, actor="host")
        for topic, summary, detail in self.facts:
            emit(
                graph,
                FINDING_RECORDED,
                {"worker": "host", "topic": topic, "summary": summary,
                 "detail": str(detail)[:4000]},
                actor="host",
                caused_by=seed.id,
            )
        for name, text in self.references.items():
            first_line = text.strip().splitlines()[0][:120] if text.strip() else name
            emit(
                graph,
                FINDING_RECORDED,
                {"worker": "host", "topic": f"ref/{name}", "summary": first_line,
                 "detail": text},
                actor="host",
                caused_by=seed.id,
            )

    def _execute(
        self,
        log_path: Path,
        *,
        worker: Optional[Worker],
        interrupt_after: Optional[int] = None,
        stop_when: Optional[Callable[[], bool]] = None,
        cache: Optional[AgentCache] = None,
        plan: Optional[ReplayPlan] = None,
        resume: bool = False,
        replay: bool = False,
    ) -> MissionResult:
        log_path.unlink(missing_ok=True)
        graph = Graph(ids=IDGen(), clock=self.clock, run_id=f"MISSION-{self.name}")
        log = JSONLLog(log_path).attach(graph)
        runtime = Runtime(graph, behaviors=self._behaviors())
        agent_names = {spec.name for spec in self.agents}

        executor: Worker = worker if worker is not None else ClaudeAgentWorker()
        if self.gate is not None:
            executor = _GateRunner(self, executor)
        host = Host(
            runtime,
            executor,
            max_concurrency=self.max_concurrency,
            cache=cache,
            plan=plan,
            resume=resume,
            replay=replay,
            claim_root=self.claim_root,
            transcript_dir=None if replay else self.transcript_dir,
        )
        self._seed(graph)

        stops: list[Callable[[Graph], bool]] = []
        if interrupt_after is not None:
            stops.append(
                lambda g: (
                    sum(
                        1
                        for e in g.events
                        if e.type == AGENT_RESPONDED
                        and e.payload.get("worker") in agent_names
                    )
                    >= interrupt_after
                )
            )
        if stop_when is not None:
            stops.append(lambda g: bool(stop_when()))
        until = (lambda g: any(stop(g) for stop in stops)) if stops else None

        result = asyncio.run(host.run(until=until))
        host.close()

        reports = _reports_from_graph(graph)
        gate_result = _gate_from_graph(graph)
        # An agent that never reported is a failure too: "completed" must not be
        # survivable by dying, which is what made an interrupted wave look done.
        agents_failed = sum(
            1
            for spec in self.agents
            if spec.name not in reports or not reports[spec.name].ok
        )
        gate_passed = None if gate_result is None else gate_result.passed
        failed = agents_failed > 0 or gate_passed is False
        emit(
            graph,
            MISSION_COMPLETED,
            {
                "agents_total": len(self.agents),
                "agents_failed": agents_failed,
                "gate_passed": gate_passed,
                "status": "failed" if failed else "completed",
            },
            actor="host",
        )
        log.close()

        outputs = {
            o.data["worker"]: o.data.get("output")
            for o in graph.objects(type="agent_report")
        }
        synthesis_objs = graph.objects(type="synthesis")
        return MissionResult(
            host=result,
            graph=graph,
            findings=[o.data for o in graph.objects(type="finding")],
            outputs=outputs,
            synthesis=(synthesis_objs[0].data.get("output") if synthesis_objs else None),
            log_path=log_path,
            failed=failed,
            gate=gate_result,
            reports=reports,
        )


class _GateRunner:
    """Routes the gate pseudo-agent to host code; everything else to the real
    worker.

    The gate must not run inside a behavior — behaviors re-execute on replay,
    and a gate may build, test, and touch the filesystem. Running it as an agent
    call instead buys the whole replay story for free: the call is cached, so a
    replay serves the verdict from the log and never invokes this runner, and
    the gate's finding is a normal worker write that `ReplayPlan` re-emits in
    its recorded window. The gate is called synchronously — the loop is
    quiescent by then (every agent has reported), so blocking it costs nothing
    and keeps the verdict's position in the log independent of timing.
    """

    def __init__(self, mission: "Mission", inner: Worker) -> None:
        self._mission = mission
        self._inner = inner

    async def __call__(self, request: AgentRequest, api: Any) -> AgentResponse:
        if request.worker != GATE_WORKER or self._mission.gate is None:
            return await self._inner(request, api)
        if api.host.replay and not api.host.resume:
            raise AssertionError(
                "replay reached the gate: the recorded verdict must be served "
                "from the log, never recomputed"
            )
        context = GateContext(
            cwd=self._mission.cwd or "",
            claim_root=self._mission.claim_root or self._mission.cwd or "",
            reports=_reports_from_graph(api.host.graph),
            findings=api.host.context.findings(limit=10**9),
        )
        try:
            verdict = self._mission.gate(context)
        except Exception as exc:  # a gate that crashes has not passed
            verdict = GateResult(
                passed=False,
                checks=[
                    {
                        "check": "gate",
                        "passed": False,
                        "error": f"{type(exc).__name__}: {exc}",
                    }
                ],
            )
        summary = "passed" if verdict.passed else "failed"
        api.emit_finding(
            GATE_TOPIC, summary, json.dumps(verdict.checks, default=str, sort_keys=True)
        )
        # Not an error response: the call itself succeeded. Mission failure is
        # read off the verdict, so a failed gate does not double-count as a
        # failed agent.
        return AgentResponse(output=summary, num_turns=0)


def _reports_from_graph(graph: Graph) -> dict[str, AgentReport]:
    """Project the `agent_report` objects back into typed reports."""
    reports: dict[str, AgentReport] = {}
    for obj in graph.objects(type="agent_report"):
        data = obj.data
        error = data.get("error")
        reports[data["worker"]] = AgentReport(
            worker=data["worker"],
            ok=bool(data.get("ok", error is None)),
            output=data.get("output"),
            error=error,
            cost_usd=data.get("cost_usd"),
            partial_output=data.get("partial_output"),
        )
    return reports


def _gate_from_graph(graph: Graph) -> Optional[GateResult]:
    """Rebuild the verdict from the gate finding — the only source that exists
    on replay, where the gate itself never runs."""
    for event in reversed(graph.events):
        payload = event.payload
        if (
            event.type != FINDING_RECORDED
            or payload.get("topic") != GATE_TOPIC
            or payload.get("worker") != GATE_WORKER
        ):
            continue
        try:
            checks = json.loads(payload.get("detail") or "[]")
        except (TypeError, ValueError):
            checks = []
        return GateResult(
            passed=payload.get("summary") == "passed",
            checks=list(checks) if isinstance(checks, list) else [checks],
        )
    return None


__all__ = [
    "EDIT_TOOLS",
    "MISSION_COMPLETED",
    "MISSION_STARTED",
    "READ_TOOLS",
    "AgentReport",
    "AgentSpec",
    "GateContext",
    "GateResult",
    "Mission",
    "MissionResult",
]
