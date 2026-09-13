"""AgentGraph — Agent SDK workers under ActiveGraph-style orchestration.

ActiveGraph owns event-sourced orchestration and is contractually
single-threaded; the Agent SDK owns the executor and is inherently concurrent.
AgentGraph is the seam: behaviors emit `agent.requested`, the host runs the
work *outside* the runtime loop, and only the result is injected back as an
event. The runtime's total event order is preserved because the loop still
processes events one at a time — it just never blocks on one.

See `agentgraph/AGENTS.md` for the rationale behind each module.
"""

from agentgraph.agentcache import AgentCache, CachedAgentResponse, hash_agent_call
from agentgraph.claims import ClaimLedger, ClaimViolation, make_claim_hook
from agentgraph.context import GraphContext
from agentgraph.dispatcher import (
    AgentRequest,
    AgentResponse,
    ClaudeAgentWorker,
    CliWorker,
    Dispatcher,
    ScriptedWorker,
    Worker,
)
from agentgraph.events import (
    AGENT_REQUESTED,
    AGENT_RESPONDED,
    CLAIM_GRANTED,
    CLAIM_REJECTED,
    CLAIM_RELEASED,
    CLAIM_VIOLATED,
    FINDING_RECORDED,
    WORKER_EMITTABLE,
    emit,
)
from agentgraph.host import Host, HostResult, request_agent
from agentgraph.log import JSONLLog, read_events
from agentgraph.mission import AgentSpec, Mission, MissionResult
from agentgraph.narrate import narrate, narrate_path
from agentgraph.replay import ReplayPlan
from agentgraph.transcript import TranscriptWriter

__all__ = [
    "AGENT_REQUESTED",
    "AGENT_RESPONDED",
    "AgentCache",
    "AgentRequest",
    "AgentResponse",
    "AgentSpec",
    "CLAIM_GRANTED",
    "CLAIM_REJECTED",
    "CLAIM_RELEASED",
    "CLAIM_VIOLATED",
    "CachedAgentResponse",
    "ClaimLedger",
    "CliWorker",
    "ClaimViolation",
    "ClaudeAgentWorker",
    "Dispatcher",
    "FINDING_RECORDED",
    "GraphContext",
    "Host",
    "HostResult",
    "JSONLLog",
    "Mission",
    "MissionResult",
    "ReplayPlan",
    "TranscriptWriter",
    "ScriptedWorker",
    "WORKER_EMITTABLE",
    "Worker",
    "emit",
    "hash_agent_call",
    "make_claim_hook",
    "narrate",
    "narrate_path",
    "read_events",
    "request_agent",
]
