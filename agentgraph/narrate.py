"""Turn an event log into the story a human actually wants to read.

The JSONL log is the machine surface: canonical, replayable, and unreadable
at any width — a real audit run is a couple hundred long JSON lines. This
module is the human surface over the same bytes: who was sent, what each
agent published as it worked, what got claimed and refused, who was cut off
and what survived the cut, what everything cost.

    python -m agentgraph.narrate run.jsonl > run.md

Derived, never authoritative: narration reads the log and adds nothing to it,
so it can be regenerated at any time, from any log, including one produced by
a $0.00 replay.
"""

from __future__ import annotations

import sys
from typing import Any, Iterable, Optional

from activegraph.core.event import Event

from agentgraph.events import (
    AGENT_REQUESTED,
    AGENT_RESPONDED,
    CLAIM_GRANTED,
    CLAIM_REJECTED,
    CLAIM_VIOLATED,
    FINDING_RECORDED,
)
from agentgraph.log import read_events

#: Keep per-finding lines scannable; full text is always in the log itself.
SUMMARY_WIDTH = 160


def narrate(events: Iterable[Event]) -> str:
    """Render one recorded (or live, or replayed) run as markdown."""
    events = list(events)
    lines: list[str] = []
    run_id = _first_run_marker(events)

    requests: dict[str, dict[str, Any]] = {}
    for e in events:
        if e.type == AGENT_REQUESTED:
            requests[e.id] = {
                "worker": e.payload.get("worker", "?"),
                "prompt": (e.payload.get("identity") or {}).get("prompt", ""),
                "model": (e.payload.get("identity") or {}).get("model"),
                "story": [],
                "response": None,
            }

    board: list[Event] = []  # host-seeded facts, before any agent
    violations: list[Event] = []
    for e in events:
        if e.type == FINDING_RECORDED:
            if e.caused_by in requests:
                requests[e.caused_by]["story"].append(("finding", e))
            else:
                board.append(e)
        elif e.type in (CLAIM_GRANTED, CLAIM_REJECTED) and e.caused_by in requests:
            requests[e.caused_by]["story"].append(("claim", e))
        elif e.type == CLAIM_VIOLATED:
            violations.append(e)
        elif e.type == AGENT_RESPONDED and e.caused_by in requests:
            requests[e.caused_by]["response"] = e

    # ---- header ----
    n_ok = sum(
        1 for r in requests.values()
        if r["response"] is not None and not r["response"].payload.get("error")
    )
    n_err = sum(
        1 for r in requests.values()
        if r["response"] is not None and r["response"].payload.get("error")
    )
    n_open = sum(1 for r in requests.values() if r["response"] is None)
    cost = _total_cost(requests.values())
    lines.append(f"# Run {run_id} — {len(events)} events")
    lines.append("")
    lines.append(
        f"**{len(requests)} agents**: {n_ok} completed, {n_err} failed"
        + (f", {n_open} never returned (interrupted)" if n_open else "")
        + f" · cost ≥ ${cost}"
    )
    lines.append("")

    # ---- the seeded board ----
    if board:
        lines.append("## Board seed (host facts)")
        lines.append("")
        for e in board:
            topic = e.payload.get("topic", "")
            lines.append(f"- **{topic}** — {_clip(e.payload.get('summary', ''))}")
        lines.append("")

    # ---- one section per agent, in dispatch order ----
    for req_id, r in requests.items():
        lines.append(f"## {r['worker']}")
        lines.append("")
        first = (r["prompt"].strip().splitlines() or [""])[0]
        lines.append(f"*{_clip(first, 120)}*  (`{r['model'] or 'default'}`)")
        lines.append("")
        for kind, e in r["story"]:
            if kind == "finding":
                lines.append(
                    f"- 📌 **{e.payload.get('topic', '')}** — "
                    f"{_clip(e.payload.get('summary', ''))}"
                )
            elif e.type == CLAIM_GRANTED:
                lines.append(f"- 🔒 claimed {_paths(e.payload.get('paths'))}")
            else:
                lines.append(
                    f"- ⛔ claim refused for {_paths(e.payload.get('paths'))}"
                )
        response = r["response"]
        if response is None:
            lines.append("- ⏸ **never returned** — the run stopped first; a "
                         "resume will run this agent")
        else:
            p = response.payload
            error = p.get("error")
            if error:
                lines.append(
                    f"- ❌ **failed** (`{error.get('type')}`): "
                    f"{_clip(str(error.get('message', '')))}"
                )
                partial = error.get("partial_output")
                if partial:
                    lines.append("")
                    lines.append("  What survived the cut-off:")
                    lines.append("")
                    lines.append(_indent(_clip(partial, 1200)))
            else:
                lines.append(
                    f"- ✅ completed · ${p.get('cost_usd', '0')} · "
                    f"{p.get('num_turns', '?')} turns"
                )
        lines.append("")

    # ---- refused writes ----
    if violations:
        lines.append("## Writes refused by the claim hook")
        lines.append("")
        for e in violations:
            lines.append(
                f"- {e.payload.get('worker')} → {e.payload.get('path')} "
                f"(owned by {e.payload.get('owner')})"
            )
        lines.append("")

    # ---- full outputs last, where they read as conclusions ----
    finals = [
        (r["worker"], r["response"].payload.get("output"))
        for r in requests.values()
        if r["response"] is not None and r["response"].payload.get("output")
    ]
    if finals:
        lines.append("## Outputs")
        lines.append("")
        for worker, output in finals:
            lines.append(f"### {worker}")
            lines.append("")
            lines.append(str(output).strip())
            lines.append("")

    return "\n".join(lines)


def narrate_path(path: str) -> str:
    return narrate(read_events(path))


def _first_run_marker(events: list[Event]) -> str:
    for e in events:
        mission = e.payload.get("mission") if e.payload else None
        if mission:
            return str(mission)
    return events[0].type if events else "(empty)"


def _total_cost(requests: Iterable[dict[str, Any]]) -> str:
    from decimal import Decimal

    total = Decimal("0")
    for r in requests:
        if r["response"] is not None:
            total += Decimal(str(r["response"].payload.get("cost_usd", "0") or "0"))
    return str(total)


def _clip(text: str, width: int = SUMMARY_WIDTH) -> str:
    text = " ".join(str(text).split())
    return text if len(text) <= width else text[: width - 1] + "…"


def _paths(paths: Optional[list[str]]) -> str:
    if not paths:
        return "(none)"
    shown = ", ".join(str(p).replace("\\", "/").rsplit("/", 1)[-1] for p in paths[:4])
    extra = f" +{len(paths) - 4} more" if len(paths) > 4 else ""
    return shown + extra


def _indent(text: str) -> str:
    return "\n".join(f"  > {line}" for line in str(text).splitlines())


if __name__ == "__main__":
    if len(sys.argv) != 2:
        raise SystemExit("usage: python -m agentgraph.narrate <run.jsonl>")
    out = narrate_path(sys.argv[1])
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[union-attr]
    print(out)


__all__ = ["narrate", "narrate_path"]
