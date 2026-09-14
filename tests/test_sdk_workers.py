"""Tests for multi-SDK worker support: RoutingWorker, available_sdks, resolve_workers."""

from __future__ import annotations

import pytest

from agentgraph import ScriptedWorker
from agentgraph.dispatcher import AgentRequest, AgentResponse
from agentgraph.sdk_workers import (
    RoutingWorker,
    WORKER_REGISTRY,
    available_sdks,
    make_worker,
    resolve_workers,
)


def test_routing_worker_delegates_by_agent_name() -> None:
    """RoutingWorker routes requests to the correct worker by request.worker."""

    def alpha_responder(request, api):
        return f"alpha handled: {request.prompt}"

    def beta_responder(request, api):
        return f"beta handled: {request.prompt}"

    alpha_worker = ScriptedWorker(alpha_responder)
    beta_worker = ScriptedWorker(beta_responder)

    routing = RoutingWorker(
        workers_by_agent={"alpha": alpha_worker, "beta": beta_worker},
        default=ScriptedWorker(lambda req, api: "default"),
    )

    # Create a minimal WorkerAPI mock
    class MockAPI:
        pass

    api = MockAPI()

    # Test alpha routing
    alpha_request = AgentRequest(worker="alpha", prompt="task for alpha")
    import asyncio

    alpha_response = asyncio.run(routing(alpha_request, api))
    assert isinstance(alpha_response, AgentResponse)
    assert alpha_response.output == "alpha handled: task for alpha"

    # Test beta routing
    beta_request = AgentRequest(worker="beta", prompt="task for beta")
    beta_response = asyncio.run(routing(beta_request, api))
    assert beta_response.output == "beta handled: task for beta"


def test_routing_worker_falls_back_to_default() -> None:
    """RoutingWorker uses the default worker for unknown agent names."""

    def default_responder(request, api):
        return f"default handled {request.worker}: {request.prompt}"

    default_worker = ScriptedWorker(default_responder)
    routing = RoutingWorker(
        workers_by_agent={"alpha": ScriptedWorker(lambda req, api: "alpha")},
        default=default_worker,
    )

    class MockAPI:
        pass

    api = MockAPI()

    # Request from an agent not in workers_by_agent
    gamma_request = AgentRequest(worker="gamma", prompt="unknown agent task")
    import asyncio

    response = asyncio.run(routing(gamma_request, api))
    assert response.output == "default handled gamma: unknown agent task"


def test_resolve_workers_creates_routing_worker() -> None:
    """resolve_workers builds a RoutingWorker from agent->SDK mappings."""
    # This test uses only "claude" which always exists in WORKER_REGISTRY,
    # avoiding dependency on external CLIs being installed
    routing = resolve_workers(
        agent_sdks={"alpha": "claude", "beta": "claude"}, default_sdk="claude"
    )

    assert isinstance(routing, RoutingWorker)
    # The routing worker should have workers for alpha and beta
    assert "alpha" in routing._workers_by_agent
    assert "beta" in routing._workers_by_agent


def test_resolve_workers_raises_on_unknown_sdk() -> None:
    """resolve_workers raises ValueError listing valid SDKs for unknown names."""
    with pytest.raises(ValueError) as exc_info:
        resolve_workers({"alpha": "nope"}, default_sdk="claude")

    error_msg = str(exc_info.value)
    assert "nope" in error_msg
    assert "Valid SDK names:" in error_msg
    # Should list all registered SDKs
    for sdk in WORKER_REGISTRY.keys():
        assert sdk in error_msg


def test_resolve_workers_validates_default_sdk() -> None:
    """resolve_workers validates the default_sdk parameter too."""
    with pytest.raises(ValueError) as exc_info:
        resolve_workers({"alpha": "claude"}, default_sdk="invalid")

    error_msg = str(exc_info.value)
    assert "invalid" in error_msg


def test_available_sdks_returns_all_keys() -> None:
    """available_sdks returns a bool for each known SDK."""
    sdks = available_sdks()

    # All four SDKs must be present in the result
    assert set(sdks.keys()) == {"claude", "copilot", "codex", "gemini"}

    # All values must be bools
    for sdk, available in sdks.items():
        assert isinstance(available, bool), f"{sdk} availability is not a bool"


def test_available_sdks_does_not_import_claude_sdk_eagerly() -> None:
    """available_sdks uses importlib.util.find_spec, not an actual import.

    This test verifies that calling available_sdks() does not leave
    claude_agent_sdk in sys.modules if it wasn't already there.
    """
    import sys

    # Remove claude_agent_sdk from sys.modules if present
    sdk_was_loaded = "claude_agent_sdk" in sys.modules
    if sdk_was_loaded:
        # Can't meaningfully test this when the SDK is already loaded
        pytest.skip("claude_agent_sdk already imported in this test run")

    _ = available_sdks()

    # SDK should still not be in sys.modules
    assert (
        "claude_agent_sdk" not in sys.modules
    ), "available_sdks() eagerly imported claude_agent_sdk"


def test_make_worker_supports_claude() -> None:
    """make_worker can create a ClaudeAgentWorker."""
    from agentgraph.dispatcher import ClaudeAgentWorker

    worker = make_worker("claude")
    assert isinstance(worker, ClaudeAgentWorker)


def test_worker_registry_includes_claude() -> None:
    """WORKER_REGISTRY includes 'claude' mapped to ClaudeAgentWorker."""
    from agentgraph.dispatcher import ClaudeAgentWorker

    assert "claude" in WORKER_REGISTRY
    assert WORKER_REGISTRY["claude"] is ClaudeAgentWorker


def test_routing_worker_with_mixed_sdks() -> None:
    """Integration test: resolve_workers with different SDKs per agent.

    Uses only SDK names that are guaranteed to exist in WORKER_REGISTRY
    to avoid external CLI dependencies in tests.
    """
    from agentgraph.dispatcher import ClaudeAgentWorker

    routing = resolve_workers(
        agent_sdks={"alpha": "claude", "beta": "codex"},
        default_sdk="claude",
    )

    # Assert the routing table only: a real ClaudeAgentWorker must never be invoked here.
    assert isinstance(routing._workers_by_agent["alpha"], ClaudeAgentWorker)
    assert type(routing._workers_by_agent["beta"]).__name__ == "CodexAgentWorker"
    assert isinstance(routing._default, ClaudeAgentWorker)


def test_routing_worker_reuses_workers_for_same_sdk() -> None:
    """resolve_workers creates one worker instance per unique SDK, not per agent."""
    routing = resolve_workers(
        agent_sdks={"alpha": "claude", "beta": "claude", "gamma": "claude"},
        default_sdk="claude",
    )

    # All three agents should share the same worker instance
    alpha_worker = routing._workers_by_agent["alpha"]
    beta_worker = routing._workers_by_agent["beta"]
    gamma_worker = routing._workers_by_agent["gamma"]

    assert alpha_worker is beta_worker
    assert beta_worker is gamma_worker
    assert alpha_worker is routing._default
