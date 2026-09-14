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
            # check_same_thread=False: caller (the API layer) serializes all
            # mirror calls behind an asyncio.Lock even when offloaded via
            # asyncio.to_thread.
            self._conn = sqlite3.connect(str(self.db_path), check_same_thread=False)
            self._conn.execute("PRAGMA journal_mode=WAL")
            self._conn.execute("PRAGMA synchronous=NORMAL")
            self._create_tables()
        return self._conn

    def _migrate_to_keyed_table(
        self,
        conn: sqlite3.Connection,
        table: str,
        create_sql: str,
        columns: list[str],
    ) -> None:
        """Rebuild `table` with a real PRIMARY KEY, deduplicating existing rows."""
        cursor = conn.execute(f"PRAGMA table_info({table})")
        rows = cursor.fetchall()
        has_pk = any(row[5] == 1 for row in rows)
        if rows and not has_pk:
            conn.execute(f"DROP TABLE IF EXISTS {table}_old")
            conn.execute(f"ALTER TABLE {table} RENAME TO {table}_old")
            conn.execute(create_sql)
            column_list = ", ".join(columns)
            conn.execute(
                f"INSERT OR IGNORE INTO {table} ({column_list}) "
                f"SELECT DISTINCT {column_list} FROM {table}_old"
            )
            conn.execute(f"DROP TABLE {table}_old")
            conn.commit()

    def _create_tables(self) -> None:
        """Create mirror tables if they don't exist."""
        conn = self._ensure_open()
        conn.execute("""
            CREATE TABLE IF NOT EXISTS runs (
                run_id TEXT PRIMARY KEY,
                slug TEXT,
                started_at TEXT,
                status TEXT,
                target_repo TEXT,
                agents_failed INTEGER,
                gate_passed INTEGER
            )
        """)
        # Migration: add columns introduced after the table's first release.
        cursor = conn.execute("PRAGMA table_info(runs)")
        columns = [row[1] for row in cursor.fetchall()]
        for column, decl in (
            ("target_repo", "TEXT"),
            ("agents_failed", "INTEGER"),
            ("gate_passed", "INTEGER"),
        ):
            if column not in columns:
                conn.execute(f"ALTER TABLE runs ADD COLUMN {column} {decl}")
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
        self._migrate_to_keyed_table(
            conn,
            "findings",
            """
            CREATE TABLE findings (
                run_id TEXT,
                seq INTEGER,
                worker TEXT,
                topic TEXT,
                summary TEXT,
                PRIMARY KEY(run_id, seq)
            )
            """,
            ["run_id", "seq", "worker", "topic", "summary"],
        )
        conn.execute("""
            CREATE TABLE IF NOT EXISTS claims (
                run_id TEXT,
                seq INTEGER,
                path TEXT,
                owner TEXT,
                status TEXT,
                PRIMARY KEY(run_id, seq, path)
            )
        """)
        self._migrate_to_keyed_table(
            conn,
            "claims",
            """
            CREATE TABLE claims (
                run_id TEXT,
                seq INTEGER,
                path TEXT,
                owner TEXT,
                status TEXT,
                PRIMARY KEY(run_id, seq, path)
            )
            """,
            ["run_id", "seq", "path", "owner", "status"],
        )
        conn.execute("""
            CREATE TABLE IF NOT EXISTS violations (
                run_id TEXT,
                seq INTEGER,
                worker TEXT,
                tool_name TEXT,
                command TEXT,
                pattern TEXT,
                PRIMARY KEY(run_id, seq)
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS mirror_state (
                run_id TEXT PRIMARY KEY,
                path TEXT,
                byte_offset INTEGER
            )
        """)
        conn.commit()

    def _apply_event(
        self,
        conn: sqlite3.Connection,
        run_id: str,
        raw_line: dict[str, Any],
        target_repo: str | None,
    ) -> None:
        """Upsert mirror rows for one envelope. Caller owns the transaction."""
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

                if isinstance(cost_usd, str):
                    try:
                        cost_usd = float(cost_usd)
                    except (ValueError, TypeError):
                        cost_usd = 0.0

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
                    INSERT OR REPLACE INTO claims (run_id, seq, path, owner, status)
                    VALUES (?, ?, ?, ?, ?)
                    """,
                    (run_id, seq, path, owner, status),
                )

        elif event_type == "command.violated":
            conn.execute(
                """
                INSERT OR REPLACE INTO violations
                (run_id, seq, worker, tool_name, command, pattern)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    run_id,
                    seq,
                    payload.get("worker"),
                    payload.get("tool_name"),
                    payload.get("command"),
                    payload.get("pattern"),
                ),
            )

        elif event_type == "mission.started":
            mission = payload.get("mission")
            conn.execute(
                """
                INSERT INTO runs (run_id, slug, started_at, status, target_repo)
                VALUES (?, ?, ?, 'running', ?)
                ON CONFLICT(run_id) DO UPDATE SET
                    slug=excluded.slug,
                    started_at=excluded.started_at,
                    target_repo=excluded.target_repo
                """,
                (run_id, mission, timestamp, target_repo),
            )

        elif event_type == "mission.completed":
            conn.execute(
                """
                INSERT INTO runs (run_id, status, agents_failed, gate_passed)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(run_id) DO UPDATE SET
                    status=excluded.status,
                    agents_failed=excluded.agents_failed,
                    gate_passed=excluded.gate_passed
                """,
                (
                    run_id,
                    payload.get("status"),
                    payload.get("agents_failed"),
                    payload.get("gate_passed"),
                ),
            )

    def ingest_event(self, run_id: str, raw_line: dict[str, Any], target_repo: str | None = None) -> None:
        """Ingest one JSONL envelope and upsert appropriate mirror rows.

        Args:
            run_id: The run identifier
            raw_line: Already json.loads'd JSONL line with shape
                     {"seq": n, "run_id": "...", "event": {...}}
            target_repo: Optional target repository path for this run
        """
        conn = self._ensure_open()
        with conn:
            self._apply_event(conn, run_id, raw_line, target_repo)

    def _reset_run_rows(self, conn: sqlite3.Connection, run_id: str) -> None:
        for table in ("events", "agents", "findings", "claims", "violations", "runs", "mirror_state"):
            conn.execute(f"DELETE FROM {table} WHERE run_id = ?", (run_id,))

    def delete_run(self, run_id: str) -> None:
        """Drop every mirrored row for one run, including its mirror offset."""
        conn = self._ensure_open()
        with conn:
            self._reset_run_rows(conn, run_id)

    def mirror_log_file(self, run_id: str, jsonl_path: str | Path, target_repo: str | None = None) -> int:
        """Incrementally mirror new lines appended to a JSONL log file.

        Tracks a per-run byte offset in mirror_state so repeated calls (the
        API polls every 2s) only read and ingest lines appended since the
        last call. A trailing line with no newline yet is left unconsumed —
        it is picked up whole on the next call once the writer flushes the
        newline. If the file is now shorter than the stored offset the log
        was recreated; that run's rows are dropped and it is re-ingested
        from byte 0.

        Args:
            run_id: The run identifier
            jsonl_path: Path to the run.jsonl file to mirror
            target_repo: Optional target repository path for this run

        Returns:
            Number of new events ingested by this call.
        """
        conn = self._ensure_open()
        jsonl_path = Path(jsonl_path)
        if not jsonl_path.exists():
            return 0

        file_size = jsonl_path.stat().st_size
        row = conn.execute(
            "SELECT byte_offset FROM mirror_state WHERE run_id = ?", (run_id,)
        ).fetchone()
        offset = row[0] if row else 0
        reset = file_size < offset
        if reset:
            offset = 0

        with jsonl_path.open("rb") as fh:
            fh.seek(offset)
            chunk = fh.read()

        lines = chunk.split(b"\n")
        complete_lines = lines[:-1]
        consumed = len(chunk) if chunk.endswith(b"\n") else len(chunk) - len(lines[-1])

        ingested = 0
        with conn:
            if reset:
                self._reset_run_rows(conn, run_id)
            for raw in complete_lines:
                raw = raw.strip()
                if not raw:
                    continue
                envelope = json.loads(raw.decode("utf-8"))
                self._apply_event(conn, run_id, envelope, target_repo)
                ingested += 1
            conn.execute(
                """
                INSERT INTO mirror_state (run_id, path, byte_offset)
                VALUES (?, ?, ?)
                ON CONFLICT(run_id) DO UPDATE SET
                    path=excluded.path,
                    byte_offset=excluded.byte_offset
                """,
                (run_id, str(jsonl_path), offset + consumed),
            )
        return ingested

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
