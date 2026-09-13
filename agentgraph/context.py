"""The read API workers query — the blackboard side of the log.

Plan section 2.1: the unlock is not that the log is observable, it is that the
log is *readable by the workers themselves, mid-run*. A worker that can see
what another worker already found stops duplicating it.

`GraphContext` is deliberately a thin, read-only projection over live graph
state. It never writes; every write goes through the host so the single-writer
property holds.
"""

from __future__ import annotations

from typing import Any, Optional

from activegraph import Graph

from agentgraph.claims import ClaimLedger
from agentgraph.events import AGENT_REQUESTED, AGENT_RESPONDED, FINDING_RECORDED


class GraphContext:
    """Read-only views over the run, shaped for an agent to consume.

    Everything returns plain JSON-able structures: these values cross into MCP
    tool results and end up in a model's context window, so they are kept small
    and flat on purpose.
    """

    def __init__(self, graph: Graph, ledger: ClaimLedger) -> None:
        self._graph = graph
        self._ledger = ledger

    # ---- findings: what other workers have put on the board ----

    def findings(
        self,
        *,
        topic: Optional[str] = None,
        exclude_worker: Optional[str] = None,
        limit: int = 50,
    ) -> list[dict[str, Any]]:
        """Findings recorded so far, newest last.

        `exclude_worker` is the useful default for a worker asking "what does
        everyone *else* know" — its own findings are not news to it.
        """
        out: list[dict[str, Any]] = []
        for event in self._graph.events:
            if event.type != FINDING_RECORDED:
                continue
            payload = event.payload
            if topic is not None and payload.get("topic") != topic:
                continue
            if exclude_worker is not None and payload.get("worker") == exclude_worker:
                continue
            out.append(
                {
                    "event_id": event.id,
                    "worker": payload.get("worker"),
                    "topic": payload.get("topic"),
                    "summary": payload.get("summary"),
                    "detail": payload.get("detail"),
                    "timestamp": event.timestamp,
                }
            )
        return out[-limit:]

    # ---- claims ----

    def claims(self) -> dict[str, str]:
        return self._ledger.snapshot()

    def claimed_by(self, worker: str) -> list[str]:
        return self._ledger.owned_by(worker)

    def owner_of(self, path: str) -> Optional[str]:
        return self._ledger.owner_of(path)

    # ---- agent economics and status ----

    def agents(self) -> list[dict[str, Any]]:
        """Every agent call this run, with cost and outcome once known.

        `caused_by` links a response to its request, so this is a plain walk
        rather than any bookkeeping the host has to maintain separately.
        """
        requests: dict[str, dict[str, Any]] = {}
        for event in self._graph.events:
            if event.type == AGENT_REQUESTED:
                requests[event.id] = {
                    "request_id": event.id,
                    "worker": event.payload.get("worker"),
                    "args_hash": event.payload.get("args_hash"),
                    "occurrence": event.payload.get("occurrence", 0),
                    "status": "running",
                    "cost_usd": None,
                    "latency_seconds": None,
                    "error": None,
                }
            elif event.type == AGENT_RESPONDED and event.caused_by in requests:
                entry = requests[event.caused_by]
                payload = event.payload
                entry["status"] = "failed" if payload.get("error") else "completed"
                entry["cost_usd"] = payload.get("cost_usd")
                entry["latency_seconds"] = payload.get("latency_seconds")
                entry["error"] = payload.get("error")
                entry["response_id"] = event.id
        return list(requests.values())

    def total_cost_usd(self) -> str:
        from decimal import Decimal

        total = Decimal("0")
        for entry in self.agents():
            if entry["cost_usd"] is not None:
                total += Decimal(str(entry["cost_usd"]))
        return str(total)

    # ---- objects, for behaviors that build real graph structure ----

    def objects(self, type_: Optional[str] = None) -> list[dict[str, Any]]:
        objs = (
            self._graph.objects(type=type_)
            if type_ is not None
            else self._graph.all_objects()
        )
        return [o.to_dict() for o in objs]

    def summary(self) -> dict[str, Any]:
        """The one-call orientation packet handed to a worker on request."""
        agents = self.agents()
        return {
            "run_id": self._graph.run_id,
            "events": len(self._graph.events),
            "agents_running": sum(1 for a in agents if a["status"] == "running"),
            "agents_completed": sum(1 for a in agents if a["status"] == "completed"),
            "agents_failed": sum(1 for a in agents if a["status"] == "failed"),
            "total_cost_usd": self.total_cost_usd(),
            "findings": len(self.findings(limit=10**9)),
            "claims": self.claims(),
        }


__all__ = ["GraphContext"]
