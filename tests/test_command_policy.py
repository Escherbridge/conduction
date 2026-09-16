"""Command-policy hardening for the `PreToolUse` claim hook.

`agentgraph.claims.make_claim_hook` currently only gates file-writing tools
against the claim ledger (see `claims.py`'s `WRITING_TOOLS`). The hardening
under test here extends the same hook with a *command* policy: shell/
PowerShell invocations that would kill the host process tree, wipe the
filesystem, or blow away uncommitted work must be denied outright, whatever
the current claim state is.

Everything in this file exercises the still-to-land contract:

    make_claim_hook(worker, ledger, on_violation=..., on_command_violation=...)

`on_command_violation` is called with an object carrying `.worker`,
`.command`, and `.pattern` — mirroring the existing `ClaimViolation` shape
but for a command match rather than a file claim. Until that kwarg and the
underlying command-scanning exist, every test here fails loudly (a
`TypeError` on the `make_claim_hook` call) rather than silently passing.
"""

from __future__ import annotations

import asyncio

import pytest

from agentgraph.claims import ClaimLedger, make_claim_hook

WORKER = "worker-x"

#: (tool_name, command) pairs that must be denied.
DANGEROUS = [
    ("PowerShell", "Get-Process -Name python | Stop-Process -Force"),
    ("PowerShell", "Stop-Process -Name python -Force"),
    ("PowerShell", "taskkill /IM python.exe /F"),
    ("Bash", "pkill python"),
    ("Bash", "rm -rf /"),
    ("Bash", "git reset --hard"),
]

#: (tool_name, command) pairs that must be allowed through untouched.
SAFE = [
    ("PowerShell", "Stop-Process -Id 1234 -Force"),
    ("PowerShell", "taskkill /PID 1234 /F"),
    ("Bash", "kill 1234"),
    ("Bash", "rm -rf ./build"),
    ("Bash", "git status"),
]


def build_hook(*, on_command_violation=None, on_violation=None):
    ledger = ClaimLedger()
    return make_claim_hook(
        WORKER,
        ledger,
        on_violation=on_violation,
        on_command_violation=on_command_violation,
    )


@pytest.mark.parametrize("tool_name,command", DANGEROUS)
def test_dangerous_command_is_denied(tool_name: str, command: str) -> None:
    violations: list = []
    hook = build_hook(on_command_violation=violations.append)

    decision = asyncio.run(
        hook(
            {
                "hook_event_name": "PreToolUse",
                "tool_name": tool_name,
                "tool_input": {"command": command},
            },
            None,
            None,
        )
    )

    specific = decision["hookSpecificOutput"]
    assert specific["permissionDecision"] == "deny"
    assert violations, f"no command violation recorded for {command!r}"
    violation = violations[0]
    assert violation.worker == WORKER
    assert violation.command == command
    assert violation.pattern, "violation must carry the pattern that matched"


@pytest.mark.parametrize("tool_name,command", DANGEROUS)
def test_dangerous_command_match_is_case_insensitive(tool_name: str, command: str) -> None:
    violations: list = []
    hook = build_hook(on_command_violation=violations.append)

    decision = asyncio.run(
        hook(
            {
                "hook_event_name": "PreToolUse",
                "tool_name": tool_name,
                "tool_input": {"command": command.upper()},
            },
            None,
            None,
        )
    )

    specific = decision["hookSpecificOutput"]
    assert specific["permissionDecision"] == "deny"
    assert violations, f"case-insensitive match failed for {command.upper()!r}"


@pytest.mark.parametrize("tool_name,command", SAFE)
def test_safe_command_is_not_gated(tool_name: str, command: str) -> None:
    hook = build_hook()

    decision = asyncio.run(
        hook(
            {
                "hook_event_name": "PreToolUse",
                "tool_name": tool_name,
                "tool_input": {"command": command},
            },
            None,
            None,
        )
    )

    assert decision == {}


def test_read_tool_call_is_never_gated_by_command_policy() -> None:
    hook = build_hook()

    decision = asyncio.run(
        hook(
            {
                "hook_event_name": "PreToolUse",
                "tool_name": "Read",
                "tool_input": {"file_path": "some/file.py"},
            },
            None,
            None,
        )
    )

    assert decision == {}
