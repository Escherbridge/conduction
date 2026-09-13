"""The event vocabulary AgentGraph adds on top of ActiveGraph's own.

Every type here is deliberately shaped like the framework's `tool.requested` /
`tool.responded` pair so the existing harvest, causal-walk, and trace machinery
reads our events without modification. See `agentgraph/AGENTS.md` for why.
"""

from __future__ import annotations

from typing import Any, Optional

from activegraph import Event

# --- agent dispatch ---
AGENT_REQUESTED = "agent.requested"
AGENT_RESPONDED = "agent.responded"

# --- worker-side blackboard writes ---
FINDING_RECORDED = "finding.recorded"

# --- file-claim ledger ---
CLAIM_GRANTED = "claim.granted"
CLAIM_REJECTED = "claim.rejected"
CLAIM_RELEASED = "claim.released"
CLAIM_VIOLATED = "claim.violated"

# --- command policy ---
#: A refused shell command. Payload: worker, tool_name, command, pattern.
COMMAND_VIOLATED = "command.violated"

# --- mission lifecycle ---
#: One mission run finished. Payload: agents_total, agents_failed, gate_passed,
#: status. Emitted by mission code, never by a worker.
MISSION_COMPLETED = "mission.completed"

#: Types a worker is allowed to put on the graph through `graph_emit`.
WORKER_EMITTABLE = frozenset({FINDING_RECORDED})

#: Every type this module defines, for registries and log validators.
AGENTGRAPH_EVENT_TYPES = frozenset(
    {
        AGENT_REQUESTED,
        AGENT_RESPONDED,
        FINDING_RECORDED,
        CLAIM_GRANTED,
        CLAIM_REJECTED,
        CLAIM_RELEASED,
        CLAIM_VIOLATED,
        COMMAND_VIOLATED,
        MISSION_COMPLETED,
    }
)


def emit(
    graph: Any,
    type_: str,
    payload: dict[str, Any],
    *,
    actor: Optional[str] = None,
    caused_by: Optional[str] = None,
    frame_id: Optional[str] = None,
) -> Event:
    """Emit one event, from either side of the runtime boundary.

    Behaviors do not receive a `Graph` — they receive a `BehaviorGraph`, whose
    two-argument `emit` stamps actor, `caused_by`, and `frame_id` from the
    invocation itself (CONTRACT #5). That is strictly better than passing them
    by hand: causality is recorded by the runtime that knows it, not by the
    caller that has to remember. So inside a behavior the keyword arguments
    here are ignored, and outside one — on the host, injecting a result — the
    full `Event` is built explicitly because there is no invocation to stamp
    from.

    Either way ids come from `graph.ids` and timestamps from `graph.clock`, so
    a run under a `FrozenClock` and a fresh `IDGen` is byte-reproducible.
    """
    if not hasattr(graph, "ids"):  # BehaviorGraph: the wrapper stamps for us
        return graph.emit(type_, payload)
    return graph.emit(
        Event(
            id=graph.ids.event(),
            type=type_,
            payload=payload,
            actor=actor,
            frame_id=frame_id,
            caused_by=caused_by,
            timestamp=graph.clock.now(),
        )
    )


__all__ = [
    "AGENTGRAPH_EVENT_TYPES",
    "AGENT_REQUESTED",
    "AGENT_RESPONDED",
    "CLAIM_GRANTED",
    "CLAIM_REJECTED",
    "CLAIM_RELEASED",
    "CLAIM_VIOLATED",
    "COMMAND_VIOLATED",
    "FINDING_RECORDED",
    "MISSION_COMPLETED",
    "WORKER_EMITTABLE",
    "emit",
]
