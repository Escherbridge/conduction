"""The in-process MCP server every worker gets.

Plan section 2.1: this is the actual unlock. A worker holding `graph_query` can
read what other workers have already found and claimed *while it is still
running*, instead of discovering the overlap at the join. That is the
difference between a fan-out and a blackboard.

The server is built per-worker because every tool here needs to know who is
calling — `graph_claim` must know whose claim to record, and `graph_emit` must
attribute the finding. Closing over the identity is simpler and safer than
trusting a model-supplied `worker` argument.

These tools run on the host's event loop, so they are graph writes on the same
single thread as `run_quantum` — never concurrent with it, because
`run_quantum` is synchronous and yields nothing.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any

from activegraph.core.event import Event

from agentgraph.dispatcher import AgentRequest
from agentgraph.events import FINDING_RECORDED

if TYPE_CHECKING:
    from agentgraph.host import Host

#: The MCP server name workers see. Tool ids become
#: `mcp__agentgraph__graph_query` and so on.
SERVER_NAME = "agentgraph"

GRAPH_TOOL_NAMES = (
    "mcp__agentgraph__graph_query",
    "mcp__agentgraph__graph_emit",
    "mcp__agentgraph__graph_claim",
    "mcp__agentgraph__graph_release",
)


def _text(payload: Any) -> dict[str, Any]:
    return {
        "content": [
            {"type": "text", "text": json.dumps(payload, indent=2, default=str)}
        ]
    }


def build_graph_server(
    host: "Host", request: AgentRequest, request_event: Event
) -> Any:
    """One in-process MCP server scoped to one running worker."""
    from claude_agent_sdk import create_sdk_mcp_server, tool

    worker = request.worker

    @tool(
        "graph_query",
        "Read the shared run graph: what other workers have found, which files "
        "are claimed, what every agent in this run has cost. Call this before "
        "starting work so you do not repeat someone else's.",
        {
            "type": "object",
            "properties": {
                "view": {
                    "type": "string",
                    "enum": ["summary", "findings", "claims", "agents", "objects"],
                    "description": "Which projection to read.",
                },
                "topic": {
                    "type": "string",
                    "description": "For view=findings: filter to one topic.",
                },
                "include_own": {
                    "type": "boolean",
                    "description": (
                        "For view=findings: include your own findings. "
                        "Defaults to false — you already know them."
                    ),
                },
                "object_type": {
                    "type": "string",
                    "description": "For view=objects: filter to one object type.",
                },
            },
            "required": ["view"],
        },
    )
    async def graph_query(args: dict[str, Any]) -> dict[str, Any]:
        view = args.get("view", "summary")
        if view == "summary":
            return _text(host.context.summary())
        if view == "findings":
            return _text(
                host.context.findings(
                    topic=args.get("topic"),
                    exclude_worker=None if args.get("include_own") else worker,
                )
            )
        if view == "claims":
            return _text(
                {"claims": host.context.claims(), "yours": host.context.claimed_by(worker)}
            )
        if view == "agents":
            return _text(host.context.agents())
        if view == "objects":
            return _text(host.context.objects(args.get("object_type")))
        return _text({"error": f"unknown view {view!r}"})

    @tool(
        "graph_emit",
        "Publish a finding to the shared run graph so other workers can use it "
        "immediately. Publish as soon as you know something useful — do not "
        "save it for your final answer.",
        {
            "type": "object",
            "properties": {
                "topic": {
                    "type": "string",
                    "description": "Short slug grouping related findings.",
                },
                "summary": {
                    "type": "string",
                    "description": "One line another agent can act on.",
                },
                "detail": {
                    "type": "string",
                    "description": "Optional supporting detail.",
                },
            },
            "required": ["topic", "summary"],
        },
    )
    async def graph_emit(args: dict[str, Any]) -> dict[str, Any]:
        event = host.worker_emit(
            worker,
            FINDING_RECORDED,
            {
                "topic": args["topic"],
                "summary": args["summary"],
                "detail": args.get("detail"),
            },
            request_event,
        )
        return _text({"recorded": event.id})

    @tool(
        "graph_claim",
        "Claim exclusive write access to files before editing them. Writes to "
        "unclaimed or another worker's files are refused by the harness, so "
        "claim first. If a claim is refused, another worker owns the file — "
        "report what you would have changed instead of writing it.",
        {
            "type": "object",
            "properties": {
                "paths": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "File paths to claim.",
                },
                "reason": {
                    "type": "string",
                    "description": "Why you need them; recorded in the log.",
                },
            },
            "required": ["paths"],
        },
    )
    async def graph_claim(args: dict[str, Any]) -> dict[str, Any]:
        return _text(host.worker_claim(worker, list(args["paths"]), request_event))

    @tool(
        "graph_release",
        "Release your claims on files you are finished with, so other workers "
        "can take them. Release as soon as you are done.",
        {
            "type": "object",
            "properties": {
                "paths": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Paths to release; omit to release all yours.",
                }
            },
        },
    )
    async def graph_release(args: dict[str, Any]) -> dict[str, Any]:
        paths = args.get("paths")
        return _text(
            host.worker_release(worker, list(paths) if paths else None, request_event)
        )

    return create_sdk_mcp_server(
        name=SERVER_NAME,
        version="1.0.0",
        tools=[graph_query, graph_emit, graph_claim, graph_release],
    )


__all__ = ["GRAPH_TOOL_NAMES", "SERVER_NAME", "build_graph_server"]
