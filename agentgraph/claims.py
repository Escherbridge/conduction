"""The file-claim ledger and its enforcement hook.

Plan section 2.2: claims are events, the owner map is a projection, and the
`PreToolUse` hook is the enforcement. The hook is not belt-and-braces — it is
the only real enforcement, because a worker will not voluntarily consult a
ledger before writing.

This replaces a static `partitions` block stamped with `computed_at_commit`
with something correct by construction: a claim either was granted before the
write or it was not, and the log says which.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Optional

from activegraph.core.event import Event

from agentgraph.events import CLAIM_GRANTED, CLAIM_RELEASED

#: Tools that write to disk and so require a claim.
WRITING_TOOLS = frozenset({"Write", "Edit", "NotebookEdit", "MultiEdit"})

#: Tools that hand a raw command line to a shell, and so bypass every claim.
COMMAND_TOOLS = frozenset({"Bash", "PowerShell"})

#: MSYS/Git-Bash drive form. `Path("/c/x").resolve()` silently invents
#: `C:/c/x` on Windows, so a claim on it is granted and every later Edit on the
#: real path is denied — it must be rejected by shape, not by resolution.
MSYS_DRIVE_PATH = re.compile(r"^/([A-Za-z])/")


def normalize_path(path: str, *, root: Optional[str] = None) -> str:
    """Canonical form for claim comparison.

    Resolved, case-folded (this is developed against Windows, where two casings
    are one file), and forward-slashed so a claim and a write of the same file
    always compare equal.
    """
    p = Path(path)
    if root is not None and not p.is_absolute():
        p = Path(root) / p
    try:
        resolved = p.resolve()
    except (OSError, ValueError):
        resolved = p
    return str(resolved).replace("\\", "/").casefold()


def _resolved(path: str, root: Optional[str] = None) -> Path:
    """Absolute `Path` for `path`, without the case-folding `normalize_path` does."""
    p = Path(path)
    if root is not None and not p.is_absolute():
        p = Path(root) / p
    try:
        return p.resolve()
    except (OSError, ValueError):
        return p


def is_under(path: str, root: str, *, allow_equal: bool = True) -> bool:
    """True when `path` normalizes to a location inside `root`."""
    key = normalize_path(path, root=root)
    root_key = normalize_path(root).rstrip("/")
    if not root_key:
        return True
    return key.startswith(root_key + "/") or (allow_equal and key == root_key)


def claim_rejection_reason(
    path: str,
    *,
    root: Optional[str] = None,
    owns: Optional[Iterable[str]] = None,
) -> Optional[str]:
    """Why this path may not be claimed, or None if it may."""
    if not isinstance(path, str) or not path.strip():
        return "empty path"
    if MSYS_DRIVE_PATH.match(path):
        drive = MSYS_DRIVE_PATH.match(path).group(1).upper()  # type: ignore[union-attr]
        return (
            f"{path} is an MSYS-style path; Windows resolves it to a phantom "
            f"location under the current drive. Use the Windows form "
            f"{drive}:/{path[3:]} instead."
        )
    if root is not None and not is_under(path, root, allow_equal=False):
        return f"{path} is outside the claim root {root}"
    parent = _resolved(path, root).parent
    if not parent.is_dir():
        return (
            f"{path} has no existing parent directory ({parent}); claim a path "
            f"in a directory that exists"
        )
    owned = [o for o in (owns or ()) if isinstance(o, str) and o.strip()]
    if owned and not any(
        is_under(path, str(_resolved(o, root)), allow_equal=True) for o in owned
    ):
        return (
            f"{path} is not under this agent's declared owns "
            f"({', '.join(sorted(owned))}); claim only what you were partitioned"
        )
    return None


@dataclass(frozen=True)
class ClaimViolation:
    """A rejected write, with enough detail for the worker to re-plan."""

    worker: str
    tool_name: str
    path: str
    owner: Optional[str]

    def reason(self) -> str:
        if self.owner is None:
            return (
                f"{self.tool_name} on {self.path} refused: {self.worker} holds no "
                f"claim on it. Call graph_claim first; if the claim is refused, "
                f"another worker owns the file and you must not write it."
            )
        return (
            f"{self.tool_name} on {self.path} refused: the file is claimed by "
            f"{self.owner}, not {self.worker}. Do not write it — report what you "
            f"would have changed instead."
        )


@dataclass(frozen=True)
class CommandRule:
    """One named shell-command pattern the runtime refuses to run."""

    name: str
    pattern: re.Pattern[str]
    advice: str
    #: When set, the match is only a violation if one of its delete targets is
    #: outside the claim root — `rm -rf build/` inside your own tree is fine.
    targets_must_be_under_root: bool = False


def _rule(
    name: str, source: str, advice: str, *, targets: bool = False
) -> CommandRule:
    return CommandRule(name, re.compile(source, re.IGNORECASE), advice, targets)


#: Verb plus its argument list, for deletes whose danger depends on the target.
_DELETE_SOURCE = (
    r"(?<![\w.-])(?P<verb>rm|ri|rd|rmdir|del|erase|remove-item)(?![\w-])"
    r"(?P<args>[^;|&\r\n]*)"
)
_DELETE_FORCE_FLAG = re.compile(
    r"(?:^|\s)(?:-{1,2}(?:r|f|rf|fr|force|recurse|recursive)\b|/s\b|/q\b)",
    re.IGNORECASE,
)
_ARGUMENT = re.compile(r"\"[^\"]*\"|'[^']*'|\S+")
_CMD_SWITCH = re.compile(r"/[a-z]{1,2}$", re.IGNORECASE)
_CMD_VERBS = frozenset({"del", "erase", "rd", "rmdir"})
_DANGEROUS_TARGETS = frozenset({"/", ".", "..", "*", "./*", "/*", ".*", "-"})
_DANGEROUS_EXPANSIONS = (
    "$home",
    "%userprofile%",
    "$env:userprofile",
    "%systemroot%",
    "$env:systemroot",
    "%windir%",
    "$pwd",
)

#: Commands that end other agents, the orchestrator, or the repository. A prose
#: warning in a brief is acknowledged and then violated 800 lines later; this is
#: the mechanical version of the same sentence.
DESTRUCTIVE_COMMAND_RULES: tuple[CommandRule, ...] = (
    _rule(
        "stop-process-without-pid",
        # The whole statement must be free of -Id, because the pid can be
        # selected upstream of the pipe: `Get-Process -Id 900 | Stop-Process`.
        r"(?:^|(?<=[;\r\n]))(?:(?!-id\b)[^;\r\n])*?"
        r"(?<![\w.-])(?:stop-process|spps)(?![\w-])"
        r"(?:(?!-id\b)[^;\r\n])*(?=[;\r\n]|$)",
        "Stop-Process without -Id kills every process of that name, including "
        "the orchestrator and its sibling agents.",
    ),
    _rule(
        "kill-by-name",
        r"(?<![\w.-])kill(?![\w-])[^;\r\n]*?(?<![\w-])-n(?:ame)?\b",
        "Killing by name matches sibling agents that share the executable.",
    ),
    _rule(
        "get-process-piped-into-kill",
        r"(?<![\w.-])(?:get-process|gps)(?![\w-])(?![^;\r\n]*-id\b)[^;\r\n]*\|"
        r"[^;\r\n]*(?:\.kill\(|stop-process|spps|kill(?![\w-]))",
        "A Get-Process pipeline selects by name, so it sweeps up every sibling "
        "agent as well as the target.",
    ),
    _rule(
        "taskkill-by-image-or-force",
        r"(?<![\w.-])taskkill(?![\w-])(?![^;\r\n]*/pid\b)[^;\r\n]*/(?:im|f)\b",
        "taskkill /IM and /F without /PID kill by image name, not by process.",
    ),
    _rule(
        "pkill-or-killall",
        r"(?<![\w.-])(?:pkill|killall)(?![\w-])",
        "pkill and killall match by name and cannot spare siblings.",
    ),
    _rule(
        "kill-every-process",
        r"(?<![\w.-])kill(?![\w-])(?:\s+-\w+)*\s+-1(?![\w])",
        "`kill -1` signals every process the user owns.",
    ),
    _rule(
        "git-reset-hard",
        r"(?<![\w.-])git\s+reset(?![\w-])[^;\r\n]*--hard\b",
        "git reset --hard discards work another agent has not committed yet.",
    ),
    _rule(
        "git-clean-force",
        r"(?<![\w.-])git\s+clean(?![\w-])[^;\r\n]*(?:--force\b|(?<![\w-])-[a-z]*f)",
        "git clean -f deletes untracked files belonging to other agents.",
    ),
    _rule(
        "git-push-force",
        r"(?<![\w.-])git\s+push(?![\w-])[^;\r\n]*"
        r"(?:--force(?!-with-lease)\b|(?<![\w-])-f(?![\w]))",
        "A force push rewrites history that other agents have already fetched.",
    ),
    _rule(
        "format-volume",
        r"(?<![\w.-])(?:format|format-volume)(?![\w-])[^;\r\n]*"
        r"(?:\s[a-z]:|-driveletter\b|/fs:)",
        "Formatting a volume is never part of an agent task.",
    ),
    _rule(
        "del-recursive",
        r"(?<![\w.-])(?:del|erase)(?![\w-])[^;\r\n]*/s\b",
        "del /s walks the whole subtree, well past anything you claimed.",
    ),
    _rule(
        "recursive-delete-outside-claim-root",
        _DELETE_SOURCE,
        "A recursive or forced delete must name a target inside the claim root; "
        "`/`, `~`, a bare drive, `.` and `..` are refused outright.",
        targets=True,
    ),
)

#: The compiled patterns, in rule order. `match_destructive_command` is the
#: decision point — one rule is target-sensitive and cannot be judged by its
#: regex alone.
DESTRUCTIVE_COMMAND_PATTERNS: tuple[re.Pattern[str], ...] = tuple(
    rule.pattern for rule in DESTRUCTIVE_COMMAND_RULES
)


def _delete_targets(verb: str, args: str) -> list[str]:
    """The paths a delete command would actually remove."""
    out: list[str] = []
    cmd_style = verb.casefold() in _CMD_VERBS
    for token in _ARGUMENT.findall(args):
        bare = token.strip("\"'")
        if not bare:
            continue
        if bare.startswith("-"):
            continue
        if cmd_style and _CMD_SWITCH.match(bare):
            continue
        out.append(bare)
    return out


def _delete_target_is_unsafe(target: str, *, root: Optional[str]) -> bool:
    """True when deleting `target` reaches outside the claim root."""
    folded = target.replace("\\", "/").casefold()
    trimmed = folded.rstrip("/") or "/"
    if trimmed in _DANGEROUS_TARGETS or target.startswith("~"):
        return True
    if re.fullmatch(r"[a-z]:", trimmed):
        return True
    if any(expansion in folded for expansion in _DANGEROUS_EXPANSIONS):
        return True
    if trimmed.startswith("*"):
        return True
    if root is None:
        return False
    return not is_under(target, root, allow_equal=False)


def match_destructive_command(
    command: str, *, root: Optional[str] = None
) -> Optional[CommandRule]:
    """The first rule `command` violates, or None."""
    if not isinstance(command, str) or not command.strip():
        return None
    for rule in DESTRUCTIVE_COMMAND_RULES:
        if not rule.targets_must_be_under_root:
            if rule.pattern.search(command):
                return rule
            continue
        for match in rule.pattern.finditer(command):
            args = match.group("args") or ""
            if not _DELETE_FORCE_FLAG.search(args):
                continue
            targets = _delete_targets(match.group("verb"), args)
            if not targets:
                return rule
            if any(_delete_target_is_unsafe(t, root=root) for t in targets):
                return rule
    return None


@dataclass(frozen=True)
class CommandViolation:
    """A refused shell command, with the policy that refused it."""

    worker: str
    tool_name: str
    command: str
    pattern: str

    def reason(self) -> str:
        rule = next(
            (r for r in DESTRUCTIVE_COMMAND_RULES if r.name == self.pattern), None
        )
        advice = rule.advice if rule is not None else ""
        return (
            f"{self.tool_name} refused: the command matches the destructive-command "
            f"policy {self.pattern!r}. {advice} Kill by PID only: "
            f"Stop-Process -Id <pid> (find the pid first, and never by name). "
            f"Scope deletes to files you hold a claim on. Command was: "
            f"{self.command.strip()!r}"
        )


class ClaimLedger:
    """Projection of claim events into a file-to-owner map.

    Append-only in, current-state out. `grant` is the decision point: it is
    called on the host, single-threaded, so two workers racing for one file are
    resolved by whoever the host serviced first — and the log records both the
    grant and the rejection.
    """

    def __init__(self, *, root: Optional[str] = None) -> None:
        self._root = root
        self._owner: dict[str, str] = {}
        self._claimed_as: dict[str, str] = {}

    @property
    def root(self) -> Optional[str]:
        """The directory every claim must fall under, if one was configured."""
        return self._root

    # ---- decision ----

    def conflicts(self, worker: str, paths: Iterable[str]) -> list[tuple[str, str]]:
        """`(path, current_owner)` for every path this worker cannot have."""
        out: list[tuple[str, str]] = []
        for raw in paths:
            key = normalize_path(raw, root=self._root)
            owner = self._owner.get(key)
            if owner is not None and owner != worker:
                out.append((self._claimed_as.get(key, raw), owner))
        return out

    def grant(self, worker: str, paths: Iterable[str]) -> list[str]:
        """Record ownership. Caller must check `conflicts` first."""
        granted: list[str] = []
        for raw in paths:
            # Defence in depth: the host validates first, but a path outside the
            # root must never become an ownership record, because the hook would
            # then wave writes through on a location nobody adjudicated.
            if self._root is not None and not is_under(
                raw, self._root, allow_equal=False
            ):
                continue
            key = normalize_path(raw, root=self._root)
            if self._owner.get(key) == worker:
                continue
            self._owner[key] = worker
            self._claimed_as[key] = raw
            granted.append(raw)
        return granted

    def release(self, worker: str, paths: Optional[Iterable[str]] = None) -> list[str]:
        """Drop this worker's claims — all of them if `paths` is None."""
        if paths is None:
            keys = [k for k, v in self._owner.items() if v == worker]
        else:
            keys = [
                normalize_path(p, root=self._root)
                for p in paths
                if self._owner.get(normalize_path(p, root=self._root)) == worker
            ]
        released = []
        for key in keys:
            released.append(self._claimed_as.get(key, key))
            self._owner.pop(key, None)
            self._claimed_as.pop(key, None)
        return released

    # ---- reads ----

    def owner_of(self, path: str) -> Optional[str]:
        return self._owner.get(normalize_path(path, root=self._root))

    def holds(self, worker: str, path: str) -> bool:
        return self._owner.get(normalize_path(path, root=self._root)) == worker

    def owned_by(self, worker: str) -> list[str]:
        return sorted(
            self._claimed_as.get(k, k) for k, v in self._owner.items() if v == worker
        )

    def snapshot(self) -> dict[str, str]:
        return {self._claimed_as.get(k, k): v for k, v in self._owner.items()}

    # ---- rebuild from a log ----

    @classmethod
    def from_events(
        cls, events: Iterable[Event], *, root: Optional[str] = None
    ) -> "ClaimLedger":
        ledger = cls(root=root)
        for e in events:
            if e.type == CLAIM_GRANTED:
                ledger.grant(e.payload["worker"], e.payload.get("paths", []))
            elif e.type == CLAIM_RELEASED:
                ledger.release(e.payload["worker"], e.payload.get("paths"))
        return ledger


def paths_in_tool_input(tool_name: str, tool_input: dict[str, Any]) -> list[str]:
    """Every filesystem path a writing tool is about to touch."""
    if tool_name not in WRITING_TOOLS:
        return []
    paths: list[str] = []
    for key in ("file_path", "notebook_path", "path"):
        value = tool_input.get(key)
        if isinstance(value, str) and value:
            paths.append(value)
    for edit in tool_input.get("edits") or []:
        if isinstance(edit, dict):
            value = edit.get("file_path")
            if isinstance(value, str) and value:
                paths.append(value)
    return paths


def _deny(reason: str) -> dict[str, Any]:
    """The SDK's shape for a refused `PreToolUse`."""
    return {
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "deny",
            "permissionDecisionReason": reason,
        }
    }


def make_claim_hook(
    worker: str,
    ledger: ClaimLedger,
    *,
    on_violation: Optional[Any] = None,
    on_command_violation: Optional[Any] = None,
) -> Any:
    """Build the `PreToolUse` hook that enforces `ledger` for one worker.

    Returns the SDK hook callable. `on_violation` is invoked with the
    `ClaimViolation` before the deny is returned, so the host can put a
    `claim.violated` event on the graph — a refused write is a fact worth
    recording, not just an error string handed back to the model.

    The same seam carries the command policy: the matcher this is registered
    under has no `matcher=`, so the hook already sees `Bash` and `PowerShell`
    calls, and `on_command_violation` receives a `CommandViolation` for one that
    would kill sibling agents or delete outside the claim root.
    """

    async def hook(
        input_data: dict[str, Any], tool_use_id: Optional[str], context: Any
    ) -> dict[str, Any]:
        tool_name = input_data.get("tool_name", "")
        tool_input = input_data.get("tool_input") or {}
        if tool_name in COMMAND_TOOLS:
            rule = match_destructive_command(
                tool_input.get("command", ""), root=ledger.root
            )
            if rule is not None:
                command_violation = CommandViolation(
                    worker=worker,
                    tool_name=tool_name,
                    command=str(tool_input.get("command", "")),
                    pattern=rule.name,
                )
                if on_command_violation is not None:
                    on_command_violation(command_violation)
                return _deny(command_violation.reason())
        for path in paths_in_tool_input(tool_name, tool_input):
            if ledger.holds(worker, path):
                continue
            violation = ClaimViolation(
                worker=worker,
                tool_name=tool_name,
                path=path,
                owner=ledger.owner_of(path),
            )
            if on_violation is not None:
                on_violation(violation)
            return _deny(violation.reason())
        return {}

    return hook


__all__ = [
    "COMMAND_TOOLS",
    "DESTRUCTIVE_COMMAND_PATTERNS",
    "DESTRUCTIVE_COMMAND_RULES",
    "MSYS_DRIVE_PATH",
    "WRITING_TOOLS",
    "ClaimLedger",
    "ClaimViolation",
    "CommandRule",
    "CommandViolation",
    "claim_rejection_reason",
    "is_under",
    "make_claim_hook",
    "match_destructive_command",
    "normalize_path",
    "paths_in_tool_input",
]
