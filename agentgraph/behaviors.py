"""Reference behaviors — the reactive layer that drives agent work.

Nothing here is required by the framework; these exist to show the shape. The
point is what a behavior *does not* do: it never awaits, never sees a worker,
and never learns that concurrency exists. It reads the graph, decides, and
emits. The host does the rest.

Note `on=` takes a **list**. Passing a bare string silently splats it into
characters and the behavior never fires — a mistake that costs an afternoon
the first time.
"""

from __future__ import annotations

from typing import Any, Optional, Sequence

from activegraph import Event, Graph, behavior

from agentgraph.events import AGENT_RESPONDED, FINDING_RECORDED
from agentgraph.host import request_agent
from agentgraph.mcp_tools import GRAPH_TOOL_NAMES, SERVER_NAME

#: A worker that should participate in the blackboard needs the server named
#: in its request (so the tools are wired) and the tools allowed (so it may
#: call them). Both are folded into the identity hash.
BLACKBOARD_TOOLS = GRAPH_TOOL_NAMES
BLACKBOARD_SERVERS = (SERVER_NAME,)


def fan_out(
    graph: Graph,
    source: Event,
    specs: Sequence[dict[str, Any]],
    *,
    model: Optional[str] = None,
    blackboard: bool = True,
    **shared: Any,
) -> list[Event]:
    """Emit one `agent.requested` per spec, all caused by `source`.

    All of them land in the same queue drain, so the host dispatches the whole
    wave concurrently. Fan-out width is a property of the behavior, not of the
    host.
    """
    events = []
    for spec in specs:
        kwargs: dict[str, Any] = dict(shared)
        kwargs.update(spec)
        worker = kwargs.pop("worker")
        prompt = kwargs.pop("prompt")
        if blackboard:
            kwargs.setdefault("mcp_server_names", BLACKBOARD_SERVERS)
            kwargs.setdefault(
                "allowed_tools",
                tuple(kwargs.get("allowed_tools", ())) + BLACKBOARD_TOOLS,
            )
        if model is not None:
            kwargs.setdefault("model", model)
        events.append(
            request_agent(
                graph,
                worker=worker,
                prompt=prompt,
                caused_by=source.id,
                **kwargs,
            )
        )
    return events


@behavior(name="record_agent_output", on=[AGENT_RESPONDED])
def record_agent_output(event: Event, graph: Graph, ctx: Any) -> None:
    """Turn a successful agent response into a graph object.

    This is the join: the response arrives as an event like any other, so
    everything downstream of it is ordinary reactive graph work with no
    awareness that an API call happened.
    """
    if event.payload.get("error"):
        return
    output = event.payload.get("output")
    if output is None:
        return
    graph.add_object(
        "agent_output",
        {
            "worker": event.payload.get("worker"),
            "output": output,
            "cost_usd": event.payload.get("cost_usd"),
            "response_event": event.id,
        },
    )


@behavior(name="record_finding_object", on=[FINDING_RECORDED])
def record_finding_object(event: Event, graph: Graph, ctx: Any) -> None:
    """Project a worker's mid-run finding into a queryable object."""
    graph.add_object(
        "finding",
        {
            "worker": event.payload.get("worker"),
            "topic": event.payload.get("topic"),
            "summary": event.payload.get("summary"),
        },
    )


__all__ = [
    "BLACKBOARD_SERVERS",
    "BLACKBOARD_TOOLS",
    "fan_out",
    "record_agent_output",
    "record_finding_object",
]
