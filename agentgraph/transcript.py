"""Per-agent transcripts — everything a worker did, human-readable, live.

The event log records orchestration: requests, responses, findings, claims.
It deliberately does NOT record the worker's inner life — thinking, tool
calls, tool results — because none of that is replayable state. But it is
exactly what a human wants when asking "what did that agent actually do?",
and before this module existed it was simply discarded.

One markdown file per agent call, streamed and flushed line by line, so a
transcript is `tail -f`-able while the agent is still working. Files are
sidecars: they never enter the event log, so replay determinism is untouched
(a replayed run produces no transcripts — no worker runs).
"""

from __future__ import annotations

import json
import threading
from pathlib import Path
from typing import Any, Optional

#: Tool results are context for the reader, not the record of it; a screenful
#: is plenty and a 40KB file dump is noise.
TOOL_RESULT_EXCERPT = 600


class TranscriptWriter:
    """Append-only markdown transcript for one agent call.

    Lazy: the file is created on the first write, so a cache hit — where the
    worker never runs — leaves no empty transcript behind.
    """

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self._lock = threading.RLock()
        self._file: Optional[Any] = None
        self.started = False

    # ---- lifecycle ----

    def _write(self, text: str) -> None:
        with self._lock:
            if self._file is None:
                self.path.parent.mkdir(parents=True, exist_ok=True)
                self._file = self.path.open("w", encoding="utf-8", newline="\n")
                self.started = True
            self._file.write(text)
            self._file.flush()

    def close(self) -> None:
        with self._lock:
            if self._file is not None:
                self._file.close()
                self._file = None

    # ---- what a worker's run is made of ----

    def begin(self, worker: str, prompt: str, model: Optional[str]) -> None:
        self._write(
            f"# {worker}\n\n"
            f"model: `{model or 'default'}`\n\n"
            f"## Prompt\n\n{prompt}\n\n## Run\n\n"
        )

    def thinking(self, text: str) -> None:
        quoted = "\n".join(f"> {line}" for line in text.strip().splitlines())
        self._write(f"{quoted}\n\n")

    def tool_use(self, name: str, args: Any) -> None:
        try:
            rendered = json.dumps(args, default=str)
        except (TypeError, ValueError):
            rendered = str(args)
        if len(rendered) > 400:
            rendered = rendered[:400] + "…"
        self._write(f"**→ {name}** `{rendered}`\n\n")

    def tool_result(self, content: Any) -> None:
        if isinstance(content, list):
            content = " ".join(
                b.get("text", "") if isinstance(b, dict) else str(b) for b in content
            )
        text = str(content or "").strip()
        if len(text) > TOOL_RESULT_EXCERPT:
            text = text[:TOOL_RESULT_EXCERPT] + f"… [+{len(text) - TOOL_RESULT_EXCERPT} chars]"
        self._write(f"```\n{text}\n```\n\n")

    def text(self, text: str) -> None:
        self._write(f"{text}\n\n")

    def result(self, response: Any) -> None:
        """Close the story: outcome, cost, and — on a cut-off — what survived."""
        if not self.started:
            return  # cache hit or nothing happened; leave no file
        if response.error:
            self._write(
                f"## Outcome: FAILED\n\n"
                f"`{response.error.get('type')}`: {response.error.get('message')}\n\n"
            )
            partial = response.error.get("partial_output")
            if partial:
                self._write(f"### Partial output at cut-off\n\n{partial}\n\n")
        else:
            self._write("## Outcome: completed\n\n")
        self._write(
            f"---\ncost: ${response.cost_usd} · turns: {response.num_turns} · "
            f"{response.latency_seconds:.1f}s\n"
        )
        self.close()


__all__ = ["TranscriptWriter"]
