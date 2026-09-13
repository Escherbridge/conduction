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
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Optional

from activegraph import Clock, FrozenClock, Graph, IDGen, Runtime, behavior
from activegraph.core.event import Event

from agentgraph.agentcache import AgentCache
from agentgraph.dispatcher import AgentRequest, ClaudeAgentWorker, Worker
from agentgraph.events import AGENT_RESPONDED, FINDING_RECORDED, emit
from agentgraph.host import Host, HostResult, request_agent
from agentgraph.log import JSONLLog, read_events
from agentgraph.mcp_tools import GRAPH_TOOL_NAMES, SERVER_NAME
from agentgraph.replay import ReplayPlan

MISSION_STARTED = "mission.started"

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


@dataclass
class MissionResult:
    """What a mission produced, already projected out of the graph."""

    host: HostResult
    graph: Graph
    findings: list[dict[str, Any]]
    outputs: dict[str, Any]
    synthesis: Optional[str]
    log_path: Path


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

    # ---- public entry points ----

    def run(
        self,
        log_path: str | Path,
        *,
        worker: Optional[Worker] = None,
        interrupt_after: Optional[int] = None,
    ) -> MissionResult:
        return self._execute(Path(log_path), worker=worker, interrupt_after=interrupt_after)

    def resume(
        self, recorded_log: str | Path, log_path: str | Path, *, worker: Optional[Worker] = None
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
        kwargs: dict[str, Any] = {
            "mcp_server_names": (SERVER_NAME,),
            "allowed_tools": tuple(tools) + GRAPH_TOOL_NAMES,
            "permission_mode": "bypassPermissions",
        }
        if self.cwd is not None:
            kwargs["cwd"] = self.cwd
        return kwargs

    def _behaviors(self) -> list:
        mission = self
        agent_names = {spec.name for spec in self.agents}

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
                    meta={**spec.meta, "wave": 1},
                    **mission._request_kwargs(spec.tools),
                )

        @behavior(name=f"{self.name}:record", on=[AGENT_RESPONDED])
        def record(event: Event, graph: Graph, ctx: Any) -> None:
            worker = event.payload.get("worker")
            if worker in agent_names:
                graph.add_object(
                    "agent_report",
                    {
                        "worker": worker,
                        "output": event.payload.get("output"),
                        "error": event.payload.get("error"),
                        "cost_usd": event.payload.get("cost_usd"),
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
            if mission.synthesis is None:
                return
            if event.payload.get("worker") not in agent_names:
                return
            if len(ctx.view.objects(type="agent_report")) != len(mission.agents):
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

        return [dispatch, record, join, index]

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

        host = Host(
            runtime,
            worker if worker is not None else ClaudeAgentWorker(),
            max_concurrency=self.max_concurrency,
            cache=cache,
            plan=plan,
            resume=resume,
            replay=replay,
            claim_root=self.claim_root,
            transcript_dir=None if replay else self.transcript_dir,
        )
        self._seed(graph)

        until = None
        if interrupt_after is not None:
            until = lambda g: (  # noqa: E731 — a one-condition stop rule
                sum(
                    1
                    for e in g.events
                    if e.type == AGENT_RESPONDED
                    and e.payload.get("worker") in agent_names
                )
                >= interrupt_after
            )

        result = asyncio.run(host.run(until=until))
        host.close()
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
        )


__all__ = [
    "EDIT_TOOLS",
    "MISSION_STARTED",
    "READ_TOOLS",
    "AgentSpec",
    "Mission",
    "MissionResult",
]
