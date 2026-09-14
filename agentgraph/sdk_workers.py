"""Multi-SDK worker support for AgentGraph missions.

Thin wrappers around CliWorker for Copilot, Codex, and Gemini agent CLIs.
Each implements the Worker protocol so missions can dispatch to any supported
SDK without touching dispatcher.py's core logic.

LIMITATION (same as CliWorker): no blackboard/MCP access, no claim enforcement.
These workers run as plain executors -- their findings arrive only in final
output, and claims are not enforced inside them. The known upgrade path is a
real MCP stdio/HTTP transport once the target SDK supports it.
"""

from __future__ import annotations

import shutil
from typing import TYPE_CHECKING, Any

from agentgraph.dispatcher import (
    AgentRequest,
    AgentResponse,
    ClaudeAgentWorker,
    CliWorker,
    Worker,
)

if TYPE_CHECKING:
    from agentgraph.workerapi import WorkerAPI


class RoutingWorker:
    """Routes agent requests to different Worker implementations by agent name.

    Implements the Worker protocol, delegating each request to a worker selected
    by `request.worker`. Falls back to a default worker for agents not in the
    routing table.

    Example:
        >>> routing = RoutingWorker(
        ...     workers_by_agent={"alpha": copilot_worker, "beta": codex_worker},
        ...     default=claude_worker,
        ... )
        >>> # Requests from "alpha" go to copilot_worker, "beta" to codex_worker,
        >>> # everything else to claude_worker.
    """

    def __init__(self, workers_by_agent: dict[str, Worker], default: Worker) -> None:
        """Create a routing worker.

        Args:
            workers_by_agent: Map from agent name to the worker that should handle it.
            default: Worker to use when request.worker is not in workers_by_agent.
        """
        self._workers_by_agent = dict(workers_by_agent)
        self._default = default

    async def __call__(
        self, request: AgentRequest, api: "WorkerAPI"
    ) -> AgentResponse:
        """Delegate to the worker for this agent, or the default."""
        worker = self._workers_by_agent.get(request.worker, self._default)
        return await worker(request, api)


class CopilotAgentWorker(CliWorker):
    """Worker backed by the Copilot CLI.

    Command: `copilot -p "{prompt}"`

    LIMITATION: no blackboard/MCP access, no claim enforcement — same
    limitation as CliWorker; upgrade path is a real MCP stdio/HTTP transport
    once the Copilot SDK supports it.
    """

    def __init__(self, **kwargs: Any) -> None:
        super().__init__(["copilot", "-p", "{prompt}"], **kwargs)


class CodexAgentWorker(CliWorker):
    """Worker backed by the Codex CLI.

    Command: `codex exec "{prompt}"`

    LIMITATION: no blackboard/MCP access, no claim enforcement — same
    limitation as CliWorker; upgrade path is a real MCP stdio/HTTP transport
    once the Codex SDK supports it.
    """

    def __init__(self, **kwargs: Any) -> None:
        super().__init__(["codex", "exec", "{prompt}"], **kwargs)


class GeminiAgentWorker(CliWorker):
    """Worker backed by the Gemini CLI.

    Command: `gemini -p "{prompt}"`

    LIMITATION: no blackboard/MCP access, no claim enforcement — same
    limitation as CliWorker; upgrade path is a real MCP stdio/HTTP transport
    once the Gemini SDK supports it.
    """

    def __init__(self, **kwargs: Any) -> None:
        super().__init__(["gemini", "-p", "{prompt}"], **kwargs)


#: Registry mapping SDK name strings to their worker classes.
#: This is the extension point future SDKs get added through.
WORKER_REGISTRY: dict[str, type] = {
    "claude": ClaudeAgentWorker,
    "copilot": CopilotAgentWorker,
    "codex": CodexAgentWorker,
    "gemini": GeminiAgentWorker,
}


def make_worker(sdk: str, **kwargs: Any) -> Worker:
    """Factory for creating SDK workers by name.

    Args:
        sdk: SDK name ("claude", "copilot", "codex", "gemini")
        **kwargs: Passed through to the worker constructor (e.g. cwd_from_request, env)

    Returns:
        An initialized worker for the given SDK.

    Raises:
        KeyError: If the SDK name is not in WORKER_REGISTRY.

    Example:
        >>> worker = make_worker("copilot", cwd_from_request=True)
        >>> # worker is a CopilotAgentWorker ready to accept AgentRequests
    """
    worker_class = WORKER_REGISTRY[sdk]
    return worker_class(**kwargs)


def available_sdks() -> dict[str, bool]:
    """Check which agent SDKs are available on this system.

    Returns:
        Dict mapping SDK name to availability. Keys are:
        - "claude": claude_agent_sdk is importable
        - "copilot": copilot CLI is on PATH
        - "codex": codex CLI is on PATH
        - "gemini": gemini CLI is on PATH

    Example:
        >>> sdks = available_sdks()
        >>> if sdks["claude"]:
        ...     worker = make_worker("claude")
    """
    import importlib.util

    return {
        "claude": importlib.util.find_spec("claude_agent_sdk") is not None,
        "copilot": shutil.which("copilot") is not None,
        "codex": shutil.which("codex") is not None,
        "gemini": shutil.which("gemini") is not None,
    }


def resolve_workers(
    agent_sdks: dict[str, str], *, default_sdk: str = "claude"
) -> RoutingWorker:
    """Build a RoutingWorker from per-agent SDK assignments.

    Args:
        agent_sdks: Map from agent name to SDK name ("claude", "copilot", etc.)
        default_sdk: SDK to use for agents not in agent_sdks (default: "claude")

    Returns:
        A RoutingWorker that routes each agent to its assigned SDK.

    Raises:
        ValueError: If any SDK name (in agent_sdks or default_sdk) is not in
            WORKER_REGISTRY. The error message lists valid SDK names.

    Example:
        >>> routing = resolve_workers(
        ...     {"alpha": "copilot", "beta": "codex"},
        ...     default_sdk="claude"
        ... )
        >>> # alpha uses copilot, beta uses codex, others use claude
    """
    # Collect all SDK names that need workers
    sdk_names = set(agent_sdks.values())
    sdk_names.add(default_sdk)

    # Validate all SDK names
    unknown = sdk_names - set(WORKER_REGISTRY.keys())
    if unknown:
        valid = ", ".join(sorted(WORKER_REGISTRY.keys()))
        raise ValueError(
            f"Unknown SDK(s): {', '.join(sorted(unknown))}. "
            f"Valid SDK names: {valid}"
        )

    # Build one worker per unique SDK
    workers_by_sdk: dict[str, Worker] = {}
    for sdk in sdk_names:
        workers_by_sdk[sdk] = make_worker(sdk)

    # Map agents to their workers
    workers_by_agent: dict[str, Worker] = {
        agent: workers_by_sdk[sdk] for agent, sdk in agent_sdks.items()
    }

    return RoutingWorker(
        workers_by_agent=workers_by_agent, default=workers_by_sdk[default_sdk]
    )


#: Tool name mappings for each SDK.
#:
#: Claude Code tool names (mission.py's READ_TOOLS/EDIT_TOOLS, claims.py's
#: WRITING_TOOLS) are:
#:   READ_TOOLS = ("Read", "Grep", "Glob")
#:   EDIT_TOOLS = ("Read", "Grep", "Glob", "Edit", "Write")
#:   WRITING_TOOLS = {"Write", "Edit", "NotebookEdit", "MultiEdit"}
#:
#: This map documents the equivalent tool names for each SDK where known.
#: Empty dicts indicate that the tool names are currently unknown for that SDK —
#: fill them in once the actual tool names are determined from each CLI's
#: --help output or public docs. Do not guess.
TOOL_NAME_MAP: dict[str, dict[str, str]] = {
    "claude": {
        # Claude Code tool names (the source of truth)
        "read": "Read",
        "grep": "Grep",
        "glob": "Glob",
        "edit": "Edit",
        "write": "Write",
        "notebook_edit": "NotebookEdit",
        "multi_edit": "MultiEdit",
    },
    "copilot": {
        # Unknown — fill in once determined from Copilot CLI docs/--help.
        # Example placeholders (DO NOT USE until verified):
        # "read": "ReadFile",
        # "write": "WriteFile",
        # ...
    },
    "codex": {
        # Unknown — fill in once determined from Codex CLI docs/--help.
    },
    "gemini": {
        # Unknown — fill in once determined from Gemini CLI docs/--help.
    },
}


__all__ = [
    "RoutingWorker",
    "CopilotAgentWorker",
    "CodexAgentWorker",
    "GeminiAgentWorker",
    "WORKER_REGISTRY",
    "make_worker",
    "available_sdks",
    "resolve_workers",
    "TOOL_NAME_MAP",
]
