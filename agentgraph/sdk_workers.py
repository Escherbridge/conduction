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

from typing import TYPE_CHECKING, Any

from agentgraph.dispatcher import AgentRequest, AgentResponse, CliWorker

if TYPE_CHECKING:
    from agentgraph.workerapi import WorkerAPI


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
    "copilot": CopilotAgentWorker,
    "codex": CodexAgentWorker,
    "gemini": GeminiAgentWorker,
}


def make_worker(sdk: str, **kwargs: Any) -> CliWorker:
    """Factory for creating SDK workers by name.

    Args:
        sdk: SDK name ("copilot", "codex", "gemini")
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
    "CopilotAgentWorker",
    "CodexAgentWorker",
    "GeminiAgentWorker",
    "WORKER_REGISTRY",
    "make_worker",
    "TOOL_NAME_MAP",
]
