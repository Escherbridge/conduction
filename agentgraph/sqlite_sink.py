"""SQLite event-mirror sink for AgentGraph runtime.

The JSONL log (log.py's JSONLLog) is the sole replay source of truth. This
SQLite mirror is a queryable projection for UI, never authoritative.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any

from agentgraph.log import read_envelopes


class SqliteMirror:
    """Queryable SQLite projection of AgentGraph JSONL logs.

    Opens DB in WAL mode and creates tables if absent. Designed to mirror
    completed or in-progress log files on demand — the API layer will call
    mirror_log_file periodically or on request.
    """

    def __init__(self, db_path: str | Path) -> None:
        self.db_path = Path(db_path)
        self._conn: sqlite3.Connection | None = None

    def _ensure_open(self) -> sqlite3.Connection:
        if self._conn is None:
            self.db_path.parent.mkdir(parents=True, exist_ok=True)
            self._conn = sqlite3.connect(str(self.db_path))
            # Enable WAL mode for concurrent reads during mirroring
            self._conn.execute("PRAGMA journal_mode=WAL")
            self._create_tables()
        return self._conn

    def _create_tables(self) -> None:
        """Create mirror tables if they don't exist."""
        conn = self._ensure_open()
        conn.execute("""
            CREATE TABLE IF NOT EXISTS runs (
                run_id TEXT PRIMARY KEY,
                slug TEXT,
                started_at TEXT,
                status TEXT
            )
        """)
        # Migration: add target_repo column if it doesn't exist
        cursor = conn.execute("PRAGMA table_info(runs)")
        columns = [row[1] for row in cursor.fetchall()]
        if "target_repo" not in columns:
            conn.execute("ALTER TABLE runs ADD COLUMN target_repo TEXT")
            conn.commit()
        conn.execute("""
            CREATE TABLE IF NOT EXISTS agents (
                run_id TEXT,
                name TEXT,
                model TEXT,
                status TEXT,
                cost_usd REAL,
                turns INTEGER,
                error TEXT,
                PRIMARY KEY(run_id, name)
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS events (
                run_id TEXT,
                seq INTEGER,
                type TEXT,
                actor TEXT,
                payload_json TEXT,
                ts TEXT,
                PRIMARY KEY(run_id, seq)
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS findings (
                run_id TEXT,
                seq INTEGER,
                worker TEXT,
                topic TEXT,
                summary TEXT,
                PRIMARY KEY(run_id, seq)
            )
        """)
        # Migration: recreate findings table with PRIMARY KEY if it lacks one
        cursor = conn.execute("PRAGMA table_info(findings)")
        columns = {row[1]: row for row in cursor.fetchall()}
        # Check if table exists but has no primary key (pk column is 0 for all)
        has_pk = any(row[5] == 1 for row in columns.values())
        if columns and not has_pk:
            # Recreate with deduplication
            conn.execute("DROP TABLE IF EXISTS findings_old")
            conn.execute("ALTER TABLE findings RENAME TO findings_old")
            conn.execute("""
                CREATE TABLE findings (
                    run_id TEXT,
                    seq INTEGER,
                    worker TEXT,
                    topic TEXT,
                    summary TEXT,
                    PRIMARY KEY(run_id, seq)
                )
            """)
            conn.execute("""
                INSERT OR IGNORE INTO findings (run_id, seq, worker, topic, summary)
                SELECT DISTINCT run_id, seq, worker, topic, summary FROM findings_old
            """)
            conn.execute("DROP TABLE findings_old")
            conn.commit()
        conn.execute("""
            CREATE TABLE IF NOT EXISTS claims (
                run_id TEXT,
                path TEXT,
                owner TEXT,
                status TEXT,
                seq INTEGER
            )
        """)
        conn.commit()

    def ingest_event(self, run_id: str, raw_line: dict[str, Any], target_repo: str | None = None) -> None:
        """Ingest one JSONL envelope and upsert appropriate mirror rows.

        Args:
            run_id: The run identifier
            raw_line: Already json.loads'd JSONL line with shape
                     {"seq": n, "run_id": "...", "event": {...}}
            target_repo: Optional target repository path for this run
        """
        conn = self._ensure_open()
        seq = raw_line["seq"]
        event = raw_line["event"]
        event_type = event.get("type", "")
        actor = event.get("actor")
        timestamp = event.get("timestamp", "")
        payload = event.get("payload", {})

        # Always insert into events table for full-fidelity querying
        conn.execute(
            """
            INSERT OR REPLACE INTO events (run_id, seq, type, actor, payload_json, ts)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (run_id, seq, event_type, actor, json.dumps(payload), timestamp),
        )

        # Handle specific event types
        if event_type == "agent.requested":
            worker = payload.get("worker")
            if worker:
                identity = payload.get("identity", {})
                model = identity.get("model")
                conn.execute(
                    """
                    INSERT OR REPLACE INTO agents
                    (run_id, name, model, status, cost_usd, turns, error)
                    VALUES (?, ?, ?, 'requested', 0, 0, NULL)
                    """,
                    (run_id, worker, model),
                )

        elif event_type == "agent.responded":
            worker = payload.get("worker")
            if worker:
                cost_usd = payload.get("cost_usd")
                error = payload.get("error")
                num_turns = payload.get("num_turns")
                status = "error" if error else "completed"

                # Convert cost_usd string to float if needed
                if isinstance(cost_usd, str):
                    try:
                        cost_usd = float(cost_usd)
                    except (ValueError, TypeError):
                        cost_usd = 0.0

                # Convert error dict to string if needed
                if error is not None and isinstance(error, dict):
                    error = json.dumps(error)

                conn.execute(
                    """
                    INSERT OR REPLACE INTO agents
                    (run_id, name, model, status, cost_usd, turns, error)
                    VALUES (
                        ?, ?,
                        COALESCE((SELECT model FROM agents WHERE run_id=? AND name=?), NULL),
                        ?, ?, ?, ?
                    )
                    """,
                    (run_id, worker, run_id, worker, status, cost_usd or 0.0,
                     num_turns or 0, error),
                )

        elif event_type == "finding.recorded":
            worker = payload.get("worker")
            topic = payload.get("topic")
            summary = payload.get("summary")
            conn.execute(
                """
                INSERT OR REPLACE INTO findings (run_id, seq, worker, topic, summary)
                VALUES (?, ?, ?, ?, ?)
                """,
                (run_id, seq, worker, topic, summary),
            )

        elif event_type in ("claim.granted", "claim.rejected", "claim.released", "claim.violated"):
            owner = payload.get("worker")
            paths = payload.get("paths", [])
            status = event_type.split(".")[1]  # Extract status from event type
            for path in paths:
                conn.execute(
                    """
                    INSERT INTO claims (run_id, path, owner, status, seq)
                    VALUES (?, ?, ?, ?, ?)
                    """,
                    (run_id, path, owner, status, seq),
                )

        elif event_type == "mission.started":
            mission = payload.get("mission")
            conn.execute(
                """
                INSERT OR REPLACE INTO runs (run_id, slug, started_at, status, target_repo)
                VALUES (?, ?, ?, 'running', ?)
                """,
                (run_id, mission, timestamp, target_repo),
            )

        conn.commit()

    def mirror_log_file(self, run_id: str, jsonl_path: str | Path, target_repo: str | None = None) -> None:
        """Mirror an entire JSONL log file into the SQLite database.

        Reads the log file using read_envelopes (one JSON object per line,
        each wrapping its event under "event") and calls ingest_event for
        every line.

        Args:
            run_id: The run identifier
            jsonl_path: Path to the run.jsonl file to mirror
            target_repo: Optional target repository path for this run
        """
        for envelope in read_envelopes(jsonl_path):
            self.ingest_event(run_id, envelope, target_repo)

    def close(self) -> None:
        """Close the database connection."""
        if self._conn is not None:
            self._conn.close()
            self._conn = None

    def __enter__(self) -> "SqliteMirror":
        self._ensure_open()
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()


__all__ = ["SqliteMirror"]
