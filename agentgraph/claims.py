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

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Optional

from activegraph.core.event import Event

from agentgraph.events import CLAIM_GRANTED, CLAIM_RELEASED

#: Tools that write to disk and so require a claim.
WRITING_TOOLS = frozenset({"Write", "Edit", "NotebookEdit", "MultiEdit"})


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


def make_claim_hook(
    worker: str,
    ledger: ClaimLedger,
    *,
    on_violation: Optional[Any] = None,
) -> Any:
    """Build the `PreToolUse` hook that enforces `ledger` for one worker.

    Returns the SDK hook callable. `on_violation` is invoked with the
    `ClaimViolation` before the deny is returned, so the host can put a
    `claim.violated` event on the graph — a refused write is a fact worth
    recording, not just an error string handed back to the model.
    """

    async def hook(
        input_data: dict[str, Any], tool_use_id: Optional[str], context: Any
    ) -> dict[str, Any]:
        tool_name = input_data.get("tool_name", "")
        tool_input = input_data.get("tool_input") or {}
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
            return {
                "hookSpecificOutput": {
                    "hookEventName": "PreToolUse",
                    "permissionDecision": "deny",
                    "permissionDecisionReason": violation.reason(),
                }
            }
        return {}

    return hook


__all__ = [
    "WRITING_TOOLS",
    "ClaimLedger",
    "ClaimViolation",
    "make_claim_hook",
    "normalize_path",
    "paths_in_tool_input",
]
