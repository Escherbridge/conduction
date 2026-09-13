"""The handle one running worker gets onto the run.

Passed per call, never stored on the worker. That matters: workers are
concurrent and the worker *object* is shared across all of them, so anything
stashed on `self` is a race waiting to happen. Everything a worker needs to
know about itself — who it is, which request event to attribute writes to —
rides on this object instead.

Every method here is a graph write or read that happens on the host's event
loop, so the single-writer property holds without a lock.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Optional

from activegraph.core.event import Event

from agentgraph.dispatcher import AgentRequest
from agentgraph.events import FINDING_RECORDED
from agentgraph.transcript import TranscriptWriter

if TYPE_CHECKING:
    from agentgraph.host import Host


@dataclass(frozen=True)
class WorkerAPI:
    """One worker's scoped view of the run."""

    host: "Host"
    request: AgentRequest
    request_event: Event
    #: Sidecar transcript for this call, or None when transcripts are off.
    #: Workers write their stream here; it never touches the event log.
    transcript: Optional[TranscriptWriter] = None

    @property
    def worker(self) -> str:
        return self.request.worker

    # ---- writes ----

    def emit_finding(
        self, topic: str, summary: str, detail: Optional[str] = None
    ) -> Event:
        return self.host.worker_emit(
            self.worker,
            FINDING_RECORDED,
            {"topic": topic, "summary": summary, "detail": detail},
            self.request_event,
        )

    def claim(self, paths: list[str]) -> dict[str, Any]:
        return self.host.worker_claim(self.worker, list(paths), self.request_event)

    def release(self, paths: Optional[list[str]] = None) -> dict[str, Any]:
        return self.host.worker_release(
            self.worker, list(paths) if paths else None, self.request_event
        )

    # ---- reads ----

    @property
    def context(self) -> Any:
        return self.host.context

    def findings(self, **kwargs: Any) -> list[dict[str, Any]]:
        kwargs.setdefault("exclude_worker", self.worker)
        return self.host.context.findings(**kwargs)

    def summary(self) -> dict[str, Any]:
        return self.host.context.summary()

    # ---- SDK wiring, built fresh per call ----

    def mcp_servers(self) -> dict[str, Any]:
        """In-process MCP servers for this call.

        Opt-in: a request that does not name the server in `mcp_server_names`
        gets none and cannot reach the graph, which keeps the identity hash
        honest — a worker's tool set is exactly what was hashed.
        """
        from agentgraph.mcp_tools import SERVER_NAME, build_graph_server

        if SERVER_NAME not in self.request.mcp_server_names:
            return {}
        return {SERVER_NAME: build_graph_server(self.host, self.request, self.request_event)}

    def hooks(self) -> dict[str, Any]:
        """`PreToolUse` claim and destructive-command enforcement for this call.

        One matcher with no `matcher=` filter, so the hook sees every tool —
        writes are gated on the ledger, `Bash`/`PowerShell` on the command
        policy."""
        from claude_agent_sdk import HookMatcher

        return {
            "PreToolUse": [
                HookMatcher(hooks=[self.host.claim_hook_for(self.worker, self.request_event)])
            ]
        }


__all__ = ["WorkerAPI"]
