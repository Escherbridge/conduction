"""The Agent SDK worker pool — the only place concurrency exists.

Everything here runs strictly outside `Runtime`. The runtime is contractually
single-threaded (`runtime/queue.py`: "CONTRACT #10: no priority, no async"), so
a `Tool.fn` that blocked on an agent call would serialize every agent. This
module is the escape hatch: the host hands it requests, it runs them
concurrently under a semaphore, and the host injects only the results.

`Worker` is a protocol on purpose. `ClaudeAgentWorker` is the real executor;
`ScriptedWorker` is a deterministic stand-in so the whole loop — including
replay — is testable without an API key or a cent of spend.

See `agentgraph/AGENTS.md` for the identity-hash rationale.
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field
from decimal import Decimal
from typing import TYPE_CHECKING, Any, Callable, Optional, Protocol

from agentgraph.agentcache import AgentCache, hash_agent_call

if TYPE_CHECKING:
    from agentgraph.workerapi import WorkerAPI

#: Agent calls run for minutes; the framework's `Tool` default of 30s is a
#: tool's timeout, not an agent's (plan section 3.4).
DEFAULT_TIMEOUT_SECONDS = 600.0


@dataclass(frozen=True)
class AgentRequest:
    """One unit of agent work, and everything that identifies it.

    Every field that could change the answer is folded into `identity()`, so a
    behavior that gains a tool or switches model produces a different cache key
    instead of silently reusing a stale response.
    """

    worker: str
    prompt: str
    model: Optional[str] = None
    system_prompt: Optional[str] = None
    allowed_tools: tuple[str, ...] = ()
    disallowed_tools: tuple[str, ...] = ()
    mcp_server_names: tuple[str, ...] = ()
    cwd: Optional[str] = None
    max_turns: Optional[int] = None
    permission_mode: Optional[str] = None
    setting_sources: Optional[tuple[str, ...]] = None
    #: Skills to load into the worker (SDK `skills` option). None = SDK default.
    skills: Optional[tuple[str, ...]] = None
    timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS
    #: Caller-supplied marker for anything else that changes behavior (hook
    #: sets, for instance, which are callables and so cannot be hashed).
    config_fingerprint: str = ""
    #: Not hashed: routing/labelling metadata for the log.
    meta: dict[str, Any] = field(default_factory=dict, compare=False)

    def identity(self) -> dict[str, Any]:
        """The canonical dict this request is content-addressed by.

        Mirrors `_canonical_prompt_payload`: the tool set is folded in
        precisely so that a behavior gaining or losing a tool produces a
        different key.
        """
        identity: dict[str, Any] = {
            "prompt": self.prompt,
            "model": self.model,
            "system_prompt": self.system_prompt,
            "allowed_tools": sorted(self.allowed_tools),
            "disallowed_tools": sorted(self.disallowed_tools),
            "mcp_servers": sorted(self.mcp_server_names),
            "cwd": self.cwd,
            "max_turns": self.max_turns,
            "permission_mode": self.permission_mode,
            "setting_sources": (
                None if self.setting_sources is None else sorted(self.setting_sources)
            ),
            "config_fingerprint": self.config_fingerprint,
        }
        # Later-added identity fields enter the hash only when set: an absent
        # key and an absent field hash identically, which is what keeps every
        # recording made before the field existed replayable.
        if self.skills is not None:
            identity["skills"] = sorted(self.skills)
        return identity

    @property
    def args_hash(self) -> str:
        return hash_agent_call(self.identity())

    def to_payload(self) -> dict[str, Any]:
        """The `agent.requested` payload. `worker` and `meta` ride along for
        the log without entering the hash.
        """
        return {
            "worker": self.worker,
            "args_hash": self.args_hash,
            "identity": self.identity(),
            "timeout_seconds": self.timeout_seconds,
            "meta": dict(self.meta),
        }


@dataclass
class AgentResponse:
    """What a worker returns. Shaped to become an `agent.responded` payload."""

    output: Any = None
    error: Optional[dict[str, Any]] = None
    latency_seconds: float = 0.0
    cost_usd: Decimal = Decimal("0")
    session_id: Optional[str] = None
    num_turns: int = 0
    cache_hit: bool = False

    def to_payload(self) -> dict[str, Any]:
        """The `agent.responded` payload.

        `cache_hit` is deliberately absent. It is a fact about how *this*
        process obtained the answer, not about the agent call — and recording
        it would make a replayed log differ from its original on every single
        response, which is exactly the property replay exists to check. The
        framework draws the same line for `behavior.*` and `context.read`:
        runtime bookkeeping stays out of the log. It is reported on
        `HostResult` instead.
        """
        return {
            "output": self.output,
            "error": self.error,
            "latency_seconds": round(self.latency_seconds, 6),
            "cost_usd": str(self.cost_usd),
            "session_id": self.session_id,
            "num_turns": self.num_turns,
        }


class Worker(Protocol):
    """Runs one agent request to completion.

    The `api` handle is passed per call and must never be stashed on `self`:
    the worker object is shared across concurrent calls, so per-call state on
    the instance is a race. Workers never write to the graph directly — every
    write goes through `api`, which routes it to the host.
    """

    async def __call__(
        self, request: AgentRequest, api: "WorkerAPI"
    ) -> AgentResponse: ...


class ScriptedWorker:
    """A deterministic worker for tests, demos, and replay development.

    `responder` maps a request to an output. Nothing here is random or
    wall-clock dependent, so a run under a `FrozenClock` is byte-reproducible
    end to end. `delays` optionally makes workers finish out of dispatch order,
    which is exactly the race the reorder buffer exists to absorb.
    """

    def __init__(
        self,
        responder: Callable[[AgentRequest, "WorkerAPI"], Any],
        *,
        delays: Optional[dict[str, float]] = None,
        cost_usd: Decimal = Decimal("0.01"),
    ) -> None:
        self._responder = responder
        self._delays = delays or {}
        self._cost = cost_usd
        self.calls: list[AgentRequest] = []

    async def __call__(
        self, request: AgentRequest, api: "WorkerAPI"
    ) -> AgentResponse:
        self.calls.append(request)
        delay = self._delays.get(request.worker, 0.0)
        if delay:
            await asyncio.sleep(delay)
        try:
            output = self._responder(request, api)
        except Exception as exc:  # a scripted failure is a legitimate case
            return AgentResponse(
                error={"type": type(exc).__name__, "message": str(exc)},
                latency_seconds=delay,
                cost_usd=Decimal("0"),
            )
        return AgentResponse(
            output=output,
            latency_seconds=delay,
            cost_usd=self._cost,
            session_id=f"scripted-{request.worker}",
            num_turns=1,
        )


class ClaudeAgentWorker:
    """The real executor: one `claude_agent_sdk.query` per request.

    MCP servers and hooks come from `api`, freshly built per call, because
    both are worker-scoped: the in-process MCP server closes over the worker's
    identity so `graph_claim` knows who is claiming, and the `PreToolUse` hook
    closes over the same identity so it knows whose claim to check.
    """

    async def __call__(
        self, request: AgentRequest, api: "WorkerAPI"
    ) -> AgentResponse:
        from claude_agent_sdk import (
            AssistantMessage,
            ClaudeAgentOptions,
            ResultMessage,
            TextBlock,
            ThinkingBlock,
            ToolResultBlock,
            ToolUseBlock,
            UserMessage,
            query,
        )

        options_kwargs: dict[str, Any] = {
            "allowed_tools": list(request.allowed_tools),
            "disallowed_tools": list(request.disallowed_tools),
        }
        if request.model is not None:
            options_kwargs["model"] = request.model
        if request.system_prompt is not None:
            options_kwargs["system_prompt"] = request.system_prompt
        if request.cwd is not None:
            options_kwargs["cwd"] = request.cwd
        if request.max_turns is not None:
            options_kwargs["max_turns"] = request.max_turns
        if request.permission_mode is not None:
            options_kwargs["permission_mode"] = request.permission_mode
        if request.setting_sources is not None:
            options_kwargs["setting_sources"] = list(request.setting_sources)
        if request.skills is not None:
            options_kwargs["skills"] = list(request.skills)
        servers = api.mcp_servers()
        if servers:
            options_kwargs["mcp_servers"] = servers
        hooks = api.hooks()
        if hooks:
            options_kwargs["hooks"] = hooks

        options = ClaudeAgentOptions(**options_kwargs)
        started = time.monotonic()
        text_parts: list[str] = []
        result: Optional[Any] = None
        transcript = api.transcript
        if transcript is not None:
            transcript.begin(request.worker, request.prompt, request.model)
        try:
            async for message in query(prompt=request.prompt, options=options):
                if isinstance(message, AssistantMessage):
                    for block in message.content:
                        if isinstance(block, TextBlock):
                            text_parts.append(block.text)
                            if transcript is not None:
                                transcript.text(block.text)
                        elif isinstance(block, ThinkingBlock):
                            if transcript is not None:
                                transcript.thinking(block.thinking)
                        elif isinstance(block, ToolUseBlock):
                            if transcript is not None:
                                transcript.tool_use(block.name, block.input)
                elif isinstance(message, UserMessage):
                    if transcript is not None and isinstance(message.content, list):
                        for block in message.content:
                            if isinstance(block, ToolResultBlock):
                                transcript.tool_result(block.content)
                elif isinstance(message, ResultMessage):
                    result = message
        except Exception as exc:
            response = AgentResponse(
                error=_with_partial(
                    {"type": type(exc).__name__, "message": str(exc)}, text_parts
                ),
                latency_seconds=time.monotonic() - started,
            )
            if transcript is not None:
                transcript.result(response)
            return response

        latency = time.monotonic() - started
        if result is None:
            response = AgentResponse(
                error=_with_partial(
                    {
                        "type": "NoResultMessage",
                        "message": "the SDK stream ended without a ResultMessage",
                    },
                    text_parts,
                ),
                latency_seconds=latency,
            )
        elif result.is_error:
            response = AgentResponse(
                error=_with_partial(
                    {
                        "type": "AgentError",
                        "message": result.result or result.subtype,
                        "errors": list(result.errors or []),
                    },
                    text_parts,
                ),
                latency_seconds=latency,
                cost_usd=Decimal(str(result.total_cost_usd or 0)),
                session_id=result.session_id,
                num_turns=result.num_turns,
            )
        else:
            output = (
                result.structured_output
                if result.structured_output is not None
                else (result.result if result.result is not None else "".join(text_parts))
            )
            response = AgentResponse(
                output=output,
                latency_seconds=latency,
                cost_usd=Decimal(str(result.total_cost_usd or 0)),
                session_id=result.session_id,
                num_turns=result.num_turns,
            )
        if transcript is not None:
            transcript.result(response)
        return response


def _with_partial(error: dict, text_parts: list) -> dict:
    """Attach whatever final text the worker produced before it was cut off.

    A worker that exhausted its turn cap has been paid for; dropping its
    partial answer discards purchased work. The partial lands in the error
    payload -- recorded, replayed, and rendered -- so a cut-off completion is
    always inspectable afterwards.
    """
    partial = "".join(text_parts).strip()
    if partial:
        error["partial_output"] = partial
    return error


class CliWorker:
    """A worker backed by any agent CLI -- Copilot, Gemini, Codex, aider, or a
    plain script. The `Worker` protocol is the seam that makes AgentGraph
    CLI-agnostic: orchestration, log, cache, and replay never know which
    executor produced an answer.

    `command` is a template list; `{prompt}` is substituted, or the prompt is
    piped to stdin when no placeholder appears. Examples:

        CliWorker(["copilot", "-p", "{prompt}", "--allow-all-tools"])
        CliWorker(["gemini", "-p", "{prompt}"])
        CliWorker(["codex", "exec", "{prompt}"])

    What a CLI worker does NOT get: the in-process blackboard MCP server and
    the PreToolUse claim hook are Claude Agent SDK wiring, so an external CLI
    runs as a plain executor -- its findings arrive only in its final output,
    and claims are not enforced inside it. Give write work to SDK workers;
    give self-contained read/answer work to CLI workers. (Serving the
    blackboard over a real MCP transport is the known path to lifting this;
    it is not built.)

    Cost note: most CLIs do not report spend, so `cost_usd` stays 0 -- another
    reason `HostResult.total_cost_usd` is documented as a floor.
    """

    def __init__(
        self,
        command: list,
        *,
        cwd_from_request: bool = True,
        env: Optional[dict] = None,
    ) -> None:
        self._command = list(command)
        self._cwd_from_request = cwd_from_request
        self._env = env

    async def __call__(
        self, request: AgentRequest, api: "WorkerAPI"
    ) -> AgentResponse:
        import os

        argv = [part.replace("{prompt}", request.prompt) for part in self._command]
        uses_stdin = argv == self._command  # no placeholder consumed the prompt
        transcript = api.transcript
        if transcript is not None:
            transcript.begin(request.worker, request.prompt, argv[0])

        started = time.monotonic()
        try:
            process = await asyncio.create_subprocess_exec(
                *argv,
                stdin=asyncio.subprocess.PIPE if uses_stdin else None,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=request.cwd if self._cwd_from_request else None,
                env={**os.environ, **self._env} if self._env else None,
            )
            stdout, stderr = await process.communicate(
                request.prompt.encode("utf-8") if uses_stdin else None
            )
        except (OSError, ValueError) as exc:
            response = AgentResponse(
                error={"type": type(exc).__name__, "message": str(exc)},
                latency_seconds=time.monotonic() - started,
            )
            if transcript is not None:
                transcript.result(response)
            return response

        latency = time.monotonic() - started
        out = stdout.decode("utf-8", errors="replace").strip()
        err = stderr.decode("utf-8", errors="replace").strip()
        if transcript is not None and out:
            transcript.text(out)
        if process.returncode != 0:
            response = AgentResponse(
                error=_with_partial(
                    {
                        "type": "CliExit",
                        "message": f"{argv[0]} exited {process.returncode}: {err[:500]}",
                    },
                    [out],
                ),
                latency_seconds=latency,
            )
        else:
            response = AgentResponse(output=out, latency_seconds=latency, num_turns=1)
        if transcript is not None:
            transcript.result(response)
        return response


class Dispatcher:
    """Semaphore-bounded pool with a cache in front of it.

    Replay policy matches the tool contract exactly: when `replay` is on, every
    agent call is served from the recorded log or fails loud. There is no
    "fall back to a live call" path, because that is the failure mode the
    contract is written to prevent.

    `resume` is the deliberate exception, and it is a different situation: the
    recording is known-incomplete because the run was interrupted, so a miss
    means "this agent never finished", not "the config drifted". Work that
    completed is served free; work that did not runs live.
    """

    def __init__(
        self,
        worker: Worker,
        *,
        max_concurrency: int = 4,
        cache: Optional[AgentCache] = None,
        replay: bool = False,
        resume: bool = False,
    ) -> None:
        self._worker = worker
        self._semaphore = asyncio.Semaphore(max_concurrency)
        self.cache = cache if cache is not None else AgentCache()
        self.replay = replay
        #: Resume differs from replay in exactly one rule: a cache miss is an
        #: agent that never finished, so it runs live instead of failing.
        self.resume = resume
        #: How many times each identity hash has been *dispatched* this run.
        #: Assigned at request time, on the single-threaded host, so it is
        #: deterministic regardless of completion order.
        self._issued: dict[str, int] = {}

    def next_occurrence(self, args_hash: str) -> int:
        """Claim the next occurrence index for this identity hash."""
        n = self._issued.get(args_hash, 0)
        self._issued[args_hash] = n + 1
        return n

    async def run(
        self, request: AgentRequest, occurrence: int, api: "WorkerAPI"
    ) -> AgentResponse:
        args_hash = request.args_hash
        cached = self.cache.get(args_hash, occurrence)
        if cached is not None:
            return AgentResponse(
                output=cached.output,
                error=cached.error,
                latency_seconds=cached.latency_seconds,
                cost_usd=cached.cost_usd,
                session_id=cached.session_id,
                num_turns=cached.num_turns,
                cache_hit=True,
            )
        if self.replay and not self.resume:
            raise ReplayCacheMiss(
                f"no recorded response for agent call {request.worker!r} "
                f"(hash {args_hash[:12]}, occurrence {occurrence}). Replay serves "
                f"every agent call from the log or fails loud; a miss means the "
                f"behavior, prompt, model, or tool set changed since recording."
            )
        async with self._semaphore:
            try:
                return await asyncio.wait_for(
                    self._worker(request, api), timeout=request.timeout_seconds
                )
            except asyncio.TimeoutError:
                return AgentResponse(
                    error={
                        "type": "Timeout",
                        "message": (
                            f"agent call exceeded {request.timeout_seconds}s"
                        ),
                    },
                    latency_seconds=request.timeout_seconds,
                )


class ReplayCacheMiss(RuntimeError):
    """Replay needed a recorded agent response and the log did not have it."""


__all__ = [
    "DEFAULT_TIMEOUT_SECONDS",
    "AgentRequest",
    "AgentResponse",
    "ClaudeAgentWorker",
    "CliWorker",
    "Dispatcher",
    "ReplayCacheMiss",
    "ScriptedWorker",
    "Worker",
]
