"""The append-only JSONL log — one file, three consumers.

Per plan section 2.1 the same file is the live view (`tail -f`), the thing the
graph is projected from, and the thing agents read through MCP. That only works
if it is written synchronously and flushed per event.

This deliberately does *not* use `activegraph.sinks.JSONLEventSink`. The sink
machinery delivers on a background worker with a bounded queue and a
`DROP_NEWEST` overflow policy — correct for an observer, disqualifying for a
log that replay treats as the source of truth. A `graph.add_listener` callback
is synchronous and exception-propagating, which is precisely the guarantee a
durable log needs: it cannot silently drop.
"""

from __future__ import annotations

import json
import threading
from pathlib import Path
from typing import Any, Iterator, Optional

from activegraph import Event, Graph
from activegraph.store.serde import encode_payload


class JSONLLog:
    """Synchronous append-only JSONL writer attached to a `Graph`.

    One line per event: `{"seq": n, "run_id": ..., "event": {...}}`, keys
    sorted, flushed immediately. Attach before emitting anything, or the log
    starts mid-run.
    """

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self._lock = threading.RLock()
        self._file: Optional[Any] = None
        self._seq = 0
        self._graph: Optional[Graph] = None

    # ---- lifecycle ----

    def open(self) -> "JSONLLog":
        with self._lock:
            if self._file is None:
                self.path.parent.mkdir(parents=True, exist_ok=True)
                self._file = self.path.open("a", encoding="utf-8", newline="\n")
        return self

    def attach(self, graph: Graph) -> "JSONLLog":
        """Start recording `graph`. Opens the file if it is not open yet."""
        self.open()
        self._graph = graph
        graph.add_listener(self._on_event)
        return self

    def detach(self) -> None:
        if self._graph is not None:
            self._graph._remove_listener(self._on_event)  # noqa: SLF001
            self._graph = None

    def close(self) -> None:
        self.detach()
        with self._lock:
            if self._file is not None:
                self._file.flush()
                self._file.close()
                self._file = None

    def __enter__(self) -> "JSONLLog":
        return self.open()

    def __exit__(self, *exc: object) -> None:
        self.close()

    # ---- the write path ----

    def _on_event(self, event: Event) -> None:
        with self._lock:
            if self._file is None:
                return
            self._seq += 1
            envelope = {
                "seq": self._seq,
                "run_id": self._graph.run_id if self._graph else None,
                "event": event.to_dict(),
            }
            # `encode_payload` is the framework's normalization authority for
            # Decimal, datetime, and set values; reuse it so a logged payload
            # round-trips identically to a stored one.
            normalized = json.loads(encode_payload(envelope))
            self._file.write(
                json.dumps(normalized, ensure_ascii=False, sort_keys=True) + "\n"
            )
            self._file.flush()

    @property
    def written(self) -> int:
        return self._seq


def read_envelopes(path: str | Path) -> Iterator[dict[str, Any]]:
    """Yield every envelope in a log file, skipping blank trailing lines."""
    with Path(path).open("r", encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if line:
                yield json.loads(line)


def read_events(path: str | Path) -> list[Event]:
    """Rebuild `Event` objects from a log file, in recorded order.

    This is the input to `AgentCache.from_events` and
    `recorded_completion_order` — replay reads the log, never a fixture
    directory.
    """
    events: list[Event] = []
    for envelope in read_envelopes(path):
        raw = envelope["event"]
        events.append(
            Event(
                id=raw["id"],
                type=raw["type"],
                payload=raw.get("payload") or {},
                actor=raw.get("actor"),
                frame_id=raw.get("frame_id"),
                caused_by=raw.get("caused_by"),
                timestamp=raw.get("timestamp") or "",
            )
        )
    return events


__all__ = ["JSONLLog", "read_envelopes", "read_events"]
