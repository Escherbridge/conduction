"""The replay plan — the recorded interleaving, read back off the log.

Plan section 3.3 called the remaining piece "completion ordering", and for two
agents that is all it is. At width it is not: several workers publish findings
while the host sits in one `asyncio.wait`, so their writes share a single
quantum window. A replay that gives each completion its own window produces the
same events in the same order but *grouped* differently — and the runtime emits
one `runtime.idle` marker per window, so the logs diverge on marker events
alone.

The fix is to stop modelling the interleaving and start reading it. Every event
in the log was emitted either by the runtime, inside `run_quantum`, or by the
host and its workers, between quanta. The second kind is a closed set of types.
So a maximal run of consecutive injected events in the recording *is* one
window, and replaying window by window reproduces the interleaving by
construction rather than by inference.

That also makes the match key better. Instead of `(identity_hash, occurrence)`,
a recorded response names its request directly through `caused_by`, and request
ids regenerate deterministically — so replay matches on the request id and
detects divergence at the first event that differs rather than at the end.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Iterable, Optional

from activegraph.core.event import Event

from agentgraph.events import (
    AGENT_REQUESTED,
    AGENT_RESPONDED,
    CLAIM_GRANTED,
    CLAIM_REJECTED,
    CLAIM_RELEASED,
    CLAIM_VIOLATED,
    COMMAND_VIOLATED,
    FINDING_RECORDED,
)

#: Event types that enter the graph from outside `run_quantum` — the host
#: injecting a result, or a worker writing through `WorkerAPI`. Everything else
#: in a log was emitted by the runtime or by a behavior during a quantum, and
#: so re-occurs on its own when the same events are drained.
#
# The rule for adding a type here: emitted between quanta by a hook or by the
# host → injected, so it must be served from the recording or every later
# window shifts and `ReplayOrderStall` fires. Produced by behavior or mission
# code → not injected, because replay re-runs that code and would emit it twice.
# `command.violated` is the first kind (a `PreToolUse` hook, exactly like
# `claim.violated`); `mission.completed` is the second.
INJECTED_TYPES = frozenset(
    {
        AGENT_RESPONDED,
        FINDING_RECORDED,
        CLAIM_GRANTED,
        CLAIM_REJECTED,
        CLAIM_RELEASED,
        CLAIM_VIOLATED,
        COMMAND_VIOLATED,
    }
)


@dataclass(frozen=True)
class PlannedEvent:
    """One recorded injection, ready to be re-emitted."""

    type: str
    payload: dict[str, Any]
    actor: Optional[str]
    caused_by: Optional[str]
    recorded_id: str

    @property
    def is_response(self) -> bool:
        return self.type == AGENT_RESPONDED

    @classmethod
    def from_event(cls, event: Event) -> "PlannedEvent":
        return cls(
            type=event.type,
            payload=dict(event.payload),
            actor=event.actor,
            caused_by=event.caused_by,
            recorded_id=event.id,
        )


@dataclass
class ReplayPlan:
    """The recorded injections, grouped into the quantum windows they shared.

    `groups[k]` is everything the host and its workers put on the graph between
    quantum k and quantum k+1, in recorded order.
    """

    groups: list[list[PlannedEvent]]

    @classmethod
    def from_events(cls, events: Iterable[Event]) -> "ReplayPlan":
        """Group a recorded log into the injection windows it contains.

        Only injections made *during* the host loop belong in the plan.
        Anything of an injected type that appears before the first
        `agent.requested` was seeded onto the graph by the caller before
        `run()` started — a workflow that puts deterministic facts on the
        board up front, for instance. The caller re-seeds those itself on
        replay, so including them here would emit them twice and shift every
        window after them.

        The cut is safe because no host injection can precede the first
        request: a response is caused by a request, and a worker's side effect
        is caused by its own request.
        """
        events = list(events)

        # An interrupted run can leave a request with no response: the agent
        # published findings, then the run stopped before it returned. That
        # agent has no cache entry, so a resume re-runs it and it publishes
        # those findings again. Replaying the recorded copies too would double
        # them, so they are dropped here — the live re-run is the source of
        # truth for a call that never completed.
        answered = {
            e.caused_by
            for e in events
            if e.type == AGENT_RESPONDED and e.caused_by is not None
        }
        requests = {e.id for e in events if e.type == AGENT_REQUESTED}
        orphaned = requests - answered

        groups: list[list[PlannedEvent]] = []
        current: list[PlannedEvent] = []
        started = False
        for event in events:
            if not started:
                if event.type == AGENT_REQUESTED:
                    started = True
                continue
            if event.type in INJECTED_TYPES:
                if event.caused_by in orphaned:
                    continue
                current.append(PlannedEvent.from_event(event))
            elif current:
                groups.append(current)
                current = []
        if current:
            groups.append(current)
        return cls(groups=groups)

    def __len__(self) -> int:
        return len(self.groups)

    @property
    def responses(self) -> list[PlannedEvent]:
        return [p for group in self.groups for p in group if p.is_response]

    def is_empty(self) -> bool:
        return not self.groups


__all__ = ["INJECTED_TYPES", "PlannedEvent", "ReplayPlan"]
