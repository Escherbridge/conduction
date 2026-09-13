"""Record/replay cache for agent calls — a port of `activegraph.tools.cache`.

The plan's section 3 claim in code form: an agent call is just a
non-deterministic tool call, so the framework's content-addressed cache already
fits. Three things differ from `ToolCache` and only three:

  1. The harvested types are `agent.requested` / `agent.responded`.
  2. Entries are keyed by `(hash, occurrence)` rather than hash alone, so two
     identical agent calls stay two units of work with two costs.
  3. The identity hash folds model, system prompt, and tool set into the key,
     mirroring `_canonical_prompt_payload`, so a reconfigured agent misses
     rather than silently reusing a stale answer (plan section 3.4).

See `agentgraph/AGENTS.md` for why the occurrence index exists.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Any, Iterable, Optional

from activegraph.core.event import Event
from activegraph.tools.cache import canonicalize_args, hash_tool_call

from agentgraph.events import AGENT_REQUESTED, AGENT_RESPONDED

#: The synthetic tool name agent calls are hashed under, so an agent hash can
#: never collide with a real tool's hash in a shared log.
AGENT_TOOL_NAME = "spawn_agent"


def hash_agent_call(identity: dict[str, Any]) -> str:
    """Content-address one agent call.

    `identity` must already carry everything that changes the answer — prompt,
    model, system prompt, tool set. `AgentRequest.identity()` builds it.
    """
    return hash_tool_call(tool_name=AGENT_TOOL_NAME, args=identity)


@dataclass
class CachedAgentResponse:
    """A recorded agent response, mirroring `CachedToolResponse` field for
    field plus the session and turn metadata an agent call has.
    """

    output: Any
    error: Optional[dict[str, Any]] = None
    latency_seconds: float = 0.0
    cost_usd: Decimal = Decimal("0")
    cache_hit: bool = False
    requesting_event_id: Optional[str] = None
    session_id: Optional[str] = None
    num_turns: int = 0

    def copy(self, *, cache_hit: bool) -> "CachedAgentResponse":
        return CachedAgentResponse(
            output=self.output,
            error=dict(self.error) if self.error else None,
            latency_seconds=self.latency_seconds,
            cost_usd=self.cost_usd,
            cache_hit=cache_hit,
            requesting_event_id=self.requesting_event_id,
            session_id=self.session_id,
            num_turns=self.num_turns,
        )


class AgentCache:
    """Content-addressed store of agent responses.

    Keyed by `(identity_hash, nth-occurrence)`. The occurrence index is what
    keeps two identical agent calls in one run distinct — `ToolCache` collapses
    them, which is right for a tool and wrong for an agent, where the same
    prompt issued twice is two units of work with two costs.
    """

    def __init__(self) -> None:
        self._by_key: dict[tuple[str, int], CachedAgentResponse] = {}
        self._counts: dict[str, int] = {}

    # ---- read ----

    def get(self, args_hash: str, occurrence: int) -> Optional[CachedAgentResponse]:
        entry = self._by_key.get((args_hash, occurrence))
        return None if entry is None else entry.copy(cache_hit=True)

    def has(self, args_hash: str, occurrence: int) -> bool:
        return (args_hash, occurrence) in self._by_key

    def occurrences(self, args_hash: str) -> int:
        return self._counts.get(args_hash, 0)

    def __len__(self) -> int:
        return len(self._by_key)

    # ---- write ----

    def record(
        self,
        args_hash: str,
        response: CachedAgentResponse,
        *,
        occurrence: Optional[int] = None,
        requesting_event_id: Optional[str] = None,
    ) -> int:
        """Store one response and return the occurrence index it landed at."""
        n = self._counts.get(args_hash, 0) if occurrence is None else occurrence
        clean = response.copy(cache_hit=False)
        if requesting_event_id is not None:
            clean.requesting_event_id = requesting_event_id
        self._by_key[(args_hash, n)] = clean
        self._counts[args_hash] = max(self._counts.get(args_hash, 0), n + 1)
        return n

    # ---- bulk-load from a recorded event log ----

    @classmethod
    def from_events(cls, events: Iterable[Event]) -> "AgentCache":
        """Harvest every completed agent call out of a recorded log.

        Direct analogue of `ToolCache.from_events`: walk `agent.responded`,
        follow `caused_by` back to its `agent.requested`, read `args_hash` off
        the request. Worker side effects are *not* harvested here — they are
        part of the recorded interleaving, which `ReplayPlan` owns.
        """
        cache = cls()
        events_list = list(events)
        by_id: dict[str, Event] = {e.id: e for e in events_list}

        for e in events_list:
            if e.type != AGENT_RESPONDED:
                continue
            req_id = e.caused_by
            if req_id is None:
                continue
            req = by_id.get(req_id)
            if req is None or req.type != AGENT_REQUESTED:
                continue
            args_hash = req.payload.get("args_hash")
            if not args_hash:
                continue
            cache.record(
                args_hash,
                CachedAgentResponse(
                    output=e.payload.get("output"),
                    error=e.payload.get("error"),
                    latency_seconds=float(e.payload.get("latency_seconds", 0.0) or 0.0),
                    cost_usd=_decimal(e.payload.get("cost_usd", "0")),
                    session_id=e.payload.get("session_id"),
                    num_turns=int(e.payload.get("num_turns", 0) or 0),
                ),
                occurrence=int(req.payload.get("occurrence", 0) or 0),
                requesting_event_id=req_id,
            )
        return cache


def _decimal(v: Any) -> Decimal:
    if isinstance(v, Decimal):
        return v
    return Decimal(str(v))


__all__ = [
    "AGENT_TOOL_NAME",
    "AgentCache",
    "CachedAgentResponse",
    "canonicalize_args",
    "hash_agent_call",
]
