"""Regression tests for the SQLite mirror's incremental/idempotent contract.

`SqliteMirror` (sqlite_sink.py) is a queryable projection of the JSONL log —
never authoritative, per its own docstring. Today `mirror_log_file` re-reads
the *entire* file on every call via `read_envelopes` and re-runs
`ingest_event` for every line. That happens to be idempotent for `events`
(keyed with `INSERT OR REPLACE` on `(run_id, seq)`) but not for `claims`,
which has no primary key at all — mirroring twice duplicates every claim
row. `mission.completed` also is not handled, so a run's `status` in
`runs` never leaves `"running"`.

Another agent is adding a `mirror_state` offset table (so mirroring is a
resumed tail-read, not a full re-scan) and a primary key on `claims`. These
tests write directly to that target contract; the claims-dedup and
run-status assertions are expected to fail against today's code.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

from agentgraph.sqlite_sink import SqliteMirror


def envelope(seq: int, event_type: str, payload: dict, *, actor: str = "host") -> dict:
    return {
        "seq": seq,
        "run_id": "R",
        "event": {
            "id": f"evt_{seq}",
            "type": event_type,
            "actor": actor,
            "timestamp": "2026-08-21T00:00:00Z",
            "caused_by": None,
            "frame_id": None,
            "payload": payload,
        },
    }


def write_envelopes(path: Path, envelopes: list[dict]) -> None:
    with path.open("w", encoding="utf-8") as fh:
        for env in envelopes:
            fh.write(json.dumps(env) + "\n")


def append_envelope(path: Path, env: dict) -> None:
    with path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(env) + "\n")


def base_envelopes() -> list[dict]:
    return [
        envelope(1, "mission.started", {"mission": "m"}),
        envelope(2, "agent.requested", {"worker": "alpha", "identity": {"model": "m"}}),
        envelope(3, "claim.granted", {"worker": "alpha", "paths": ["src/x.py"]}),
        envelope(4, "claim.granted", {"worker": "alpha", "paths": ["src/x.py"]}),
        envelope(5, "finding.recorded", {"worker": "alpha", "topic": "t", "summary": "s"}),
        envelope(6, "mission.completed", {"status": "completed"}),
    ]


def test_mirroring_twice_does_not_duplicate_claim_rows(tmp_path: Path) -> None:
    log_path = tmp_path / "run.jsonl"
    write_envelopes(log_path, base_envelopes())

    mirror = SqliteMirror(tmp_path / "mirror.db")
    mirror.mirror_log_file("R", log_path)
    mirror.mirror_log_file("R", log_path)

    conn = sqlite3.connect(str(mirror.db_path))
    try:
        rows = conn.execute("SELECT run_id, path, owner, status, seq FROM claims").fetchall()
    finally:
        conn.close()
        mirror.close()

    # Two distinct claim.granted events (seq 3 and 4) were logged once each;
    # mirroring the same file twice must not double them to four.
    assert len(rows) == len(set(rows)) == 2, f"expected exactly 2 distinct claim rows, got {rows!r}"


def test_mission_completed_updates_run_status(tmp_path: Path) -> None:
    log_path = tmp_path / "run.jsonl"
    write_envelopes(log_path, base_envelopes())

    mirror = SqliteMirror(tmp_path / "mirror.db")
    mirror.mirror_log_file("R", log_path)

    conn = sqlite3.connect(str(mirror.db_path))
    try:
        status = conn.execute("SELECT status FROM runs WHERE run_id = ?", ("R",)).fetchone()[0]
    finally:
        conn.close()
        mirror.close()

    assert status == "completed"


def test_mirroring_a_third_time_ingests_only_the_newly_appended_event(
    tmp_path: Path,
) -> None:
    log_path = tmp_path / "run.jsonl"
    write_envelopes(log_path, base_envelopes())

    mirror = SqliteMirror(tmp_path / "mirror.db")
    mirror.mirror_log_file("R", log_path)
    mirror.mirror_log_file("R", log_path)

    conn = sqlite3.connect(str(mirror.db_path))
    before = conn.execute("SELECT COUNT(*) FROM events").fetchone()[0]
    conn.close()

    append_envelope(
        log_path,
        envelope(7, "finding.recorded", {"worker": "beta", "topic": "t2", "summary": "s2"}),
    )
    mirror.mirror_log_file("R", log_path)

    conn = sqlite3.connect(str(mirror.db_path))
    try:
        after = conn.execute("SELECT COUNT(*) FROM events").fetchone()[0]
    finally:
        conn.close()
        mirror.close()

    assert after - before == 1, (
        f"expected exactly one new event row after the append, got a delta of {after - before}"
    )
