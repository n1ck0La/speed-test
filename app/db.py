from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator


SCHEMA = """
PRAGMA journal_mode = WAL;
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS speedtests (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    recorded_at TEXT NOT NULL,
    success INTEGER NOT NULL DEFAULT 1,
    error TEXT,
    server_id TEXT,
    server_name TEXT,
    server_sponsor TEXT,
    server_location TEXT,
    download_bps REAL,
    upload_bps REAL,
    ping_ms REAL,
    bytes_sent INTEGER,
    bytes_received INTEGER,
    external_ip TEXT,
    raw_json TEXT
);

CREATE INDEX IF NOT EXISTS idx_speedtests_recorded_at
    ON speedtests(recorded_at DESC);

CREATE TABLE IF NOT EXISTS ping_targets (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    host TEXT NOT NULL,
    interval_seconds INTEGER NOT NULL,
    probe_count INTEGER NOT NULL,
    timeout_seconds REAL NOT NULL,
    active INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS ping_results (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    target_id INTEGER NOT NULL REFERENCES ping_targets(id) ON DELETE CASCADE,
    recorded_at TEXT NOT NULL,
    packets_sent INTEGER NOT NULL,
    packets_received INTEGER NOT NULL,
    packet_loss REAL NOT NULL,
    min_ms REAL,
    avg_ms REAL,
    max_ms REAL,
    jitter_ms REAL,
    success INTEGER NOT NULL DEFAULT 1,
    error TEXT,
    raw_output TEXT
);

CREATE INDEX IF NOT EXISTS idx_ping_results_target_recorded
    ON ping_results(target_id, recorded_at DESC);

CREATE TABLE IF NOT EXISTS mtr_targets (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    host TEXT NOT NULL,
    interval_seconds INTEGER NOT NULL,
    probe_count INTEGER NOT NULL,
    timeout_seconds REAL NOT NULL,
    max_hops INTEGER NOT NULL,
    active INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS mtr_runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    target_id INTEGER NOT NULL REFERENCES mtr_targets(id) ON DELETE CASCADE,
    recorded_at TEXT NOT NULL,
    destination TEXT NOT NULL,
    hop_count INTEGER NOT NULL DEFAULT 0,
    success INTEGER NOT NULL DEFAULT 1,
    error TEXT,
    raw_output TEXT
);

CREATE INDEX IF NOT EXISTS idx_mtr_runs_target_recorded
    ON mtr_runs(target_id, recorded_at DESC);

CREATE TABLE IF NOT EXISTS mtr_hops (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id INTEGER NOT NULL REFERENCES mtr_runs(id) ON DELETE CASCADE,
    hop_index INTEGER NOT NULL,
    address TEXT,
    packets_sent INTEGER NOT NULL,
    packets_received INTEGER NOT NULL,
    packet_loss REAL NOT NULL,
    min_ms REAL,
    avg_ms REAL,
    max_ms REAL,
    jitter_ms REAL,
    reached_target INTEGER NOT NULL DEFAULT 0
);

CREATE INDEX IF NOT EXISTS idx_mtr_hops_run_hop
    ON mtr_hops(run_id, hop_index);
"""


def utcnow() -> str:
    return datetime.now(timezone.utc).isoformat()


class Database:
    def __init__(self, path: Path) -> None:
        self.path = Path(path)

    @contextmanager
    def connection(self) -> Iterator[sqlite3.Connection]:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(self.path, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        try:
            yield conn
            conn.commit()
        finally:
            conn.close()

    def initialize(self) -> None:
        with self.connection() as conn:
            conn.executescript(SCHEMA)

    def list_ping_targets(self) -> list[dict]:
        with self.connection() as conn:
            rows = conn.execute(
                "SELECT * FROM ping_targets ORDER BY active DESC, name COLLATE NOCASE"
            ).fetchall()
        return [dict(row) for row in rows]

    def get_ping_target(self, target_id: int) -> dict | None:
        with self.connection() as conn:
            row = conn.execute(
                "SELECT * FROM ping_targets WHERE id = ?",
                (target_id,),
            ).fetchone()
        return dict(row) if row else None

    def create_ping_target(
        self,
        name: str,
        host: str,
        interval_seconds: int,
        probe_count: int,
        timeout_seconds: float,
        active: bool,
    ) -> int:
        now = utcnow()
        with self.connection() as conn:
            cursor = conn.execute(
                """
                INSERT INTO ping_targets (
                    name, host, interval_seconds, probe_count, timeout_seconds,
                    active, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    name,
                    host,
                    interval_seconds,
                    probe_count,
                    timeout_seconds,
                    1 if active else 0,
                    now,
                    now,
                ),
            )
            return int(cursor.lastrowid)

    def delete_ping_target(self, target_id: int) -> None:
        with self.connection() as conn:
            conn.execute("DELETE FROM ping_targets WHERE id = ?", (target_id,))

    def toggle_ping_target(self, target_id: int) -> None:
        with self.connection() as conn:
            conn.execute(
                """
                UPDATE ping_targets
                SET active = CASE active WHEN 1 THEN 0 ELSE 1 END,
                    updated_at = ?
                WHERE id = ?
                """,
                (utcnow(), target_id),
            )

    def record_ping_result(self, target_id: int, result: dict) -> None:
        with self.connection() as conn:
            conn.execute(
                """
                INSERT INTO ping_results (
                    target_id, recorded_at, packets_sent, packets_received,
                    packet_loss, min_ms, avg_ms, max_ms, jitter_ms,
                    success, error, raw_output
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    target_id,
                    result["recorded_at"],
                    result["packets_sent"],
                    result["packets_received"],
                    result["packet_loss"],
                    result["min_ms"],
                    result["avg_ms"],
                    result["max_ms"],
                    result["jitter_ms"],
                    1 if result["success"] else 0,
                    result["error"],
                    result["raw_output"],
                ),
            )

    def latest_ping_result(self, target_id: int) -> dict | None:
        with self.connection() as conn:
            row = conn.execute(
                """
                SELECT * FROM ping_results
                WHERE target_id = ?
                ORDER BY recorded_at DESC
                LIMIT 1
                """,
                (target_id,),
            ).fetchone()
        return dict(row) if row else None

    def ping_history(self, target_id: int, since_iso: str, until_iso: str | None = None) -> list[dict]:
        with self.connection() as conn:
            if until_iso:
                rows = conn.execute(
                    """
                    SELECT recorded_at, avg_ms, jitter_ms, packet_loss
                    FROM ping_results
                    WHERE target_id = ? AND recorded_at >= ? AND recorded_at <= ?
                    ORDER BY recorded_at ASC
                    """,
                    (target_id, since_iso, until_iso),
                ).fetchall()
            else:
                rows = conn.execute(
                    """
                    SELECT recorded_at, avg_ms, jitter_ms, packet_loss
                    FROM ping_results
                    WHERE target_id = ? AND recorded_at >= ?
                    ORDER BY recorded_at ASC
                    """,
                    (target_id, since_iso),
                ).fetchall()
        return [dict(row) for row in rows]

    def list_mtr_targets(self) -> list[dict]:
        with self.connection() as conn:
            rows = conn.execute(
                "SELECT * FROM mtr_targets ORDER BY active DESC, name COLLATE NOCASE"
            ).fetchall()
        return [dict(row) for row in rows]

    def get_mtr_target(self, target_id: int) -> dict | None:
        with self.connection() as conn:
            row = conn.execute(
                "SELECT * FROM mtr_targets WHERE id = ?",
                (target_id,),
            ).fetchone()
        return dict(row) if row else None

    def create_mtr_target(
        self,
        name: str,
        host: str,
        interval_seconds: int,
        probe_count: int,
        timeout_seconds: float,
        max_hops: int,
        active: bool,
    ) -> int:
        now = utcnow()
        with self.connection() as conn:
            cursor = conn.execute(
                """
                INSERT INTO mtr_targets (
                    name, host, interval_seconds, probe_count,
                    timeout_seconds, max_hops, active, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    name,
                    host,
                    interval_seconds,
                    probe_count,
                    timeout_seconds,
                    max_hops,
                    1 if active else 0,
                    now,
                    now,
                ),
            )
            return int(cursor.lastrowid)

    def delete_mtr_target(self, target_id: int) -> None:
        with self.connection() as conn:
            conn.execute("DELETE FROM mtr_targets WHERE id = ?", (target_id,))

    def toggle_mtr_target(self, target_id: int) -> None:
        with self.connection() as conn:
            conn.execute(
                """
                UPDATE mtr_targets
                SET active = CASE active WHEN 1 THEN 0 ELSE 1 END,
                    updated_at = ?
                WHERE id = ?
                """,
                (utcnow(), target_id),
            )

    def record_mtr_run(self, target_id: int, result: dict) -> int:
        with self.connection() as conn:
            cursor = conn.execute(
                """
                INSERT INTO mtr_runs (
                    target_id, recorded_at, destination, hop_count,
                    success, error, raw_output
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    target_id,
                    result["recorded_at"],
                    result["destination"],
                    result["hop_count"],
                    1 if result["success"] else 0,
                    result["error"],
                    result["raw_output"],
                ),
            )
            run_id = int(cursor.lastrowid)
            for hop in result["hops"]:
                conn.execute(
                    """
                    INSERT INTO mtr_hops (
                        run_id, hop_index, address, packets_sent, packets_received,
                        packet_loss, min_ms, avg_ms, max_ms, jitter_ms, reached_target
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        run_id,
                        hop["hop_index"],
                        hop["address"],
                        hop["packets_sent"],
                        hop["packets_received"],
                        hop["packet_loss"],
                        hop["min_ms"],
                        hop["avg_ms"],
                        hop["max_ms"],
                        hop["jitter_ms"],
                        1 if hop["reached_target"] else 0,
                    ),
                )
        return run_id

    def latest_mtr_run(self, target_id: int) -> dict | None:
        with self.connection() as conn:
            row = conn.execute(
                """
                SELECT * FROM mtr_runs
                WHERE target_id = ?
                ORDER BY recorded_at DESC
                LIMIT 1
                """,
                (target_id,),
            ).fetchone()
        return dict(row) if row else None

    def mtr_hops_for_run(self, run_id: int) -> list[dict]:
        with self.connection() as conn:
            rows = conn.execute(
                """
                SELECT * FROM mtr_hops
                WHERE run_id = ?
                ORDER BY hop_index ASC
                """,
                (run_id,),
            ).fetchall()
        return [dict(row) for row in rows]

    def record_speedtest(self, result: dict) -> None:
        with self.connection() as conn:
            conn.execute(
                """
                INSERT INTO speedtests (
                    recorded_at, success, error, server_id, server_name,
                    server_sponsor, server_location, download_bps, upload_bps,
                    ping_ms, bytes_sent, bytes_received, external_ip, raw_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    result["recorded_at"],
                    1 if result["success"] else 0,
                    result["error"],
                    result["server_id"],
                    result["server_name"],
                    result["server_sponsor"],
                    result["server_location"],
                    result["download_bps"],
                    result["upload_bps"],
                    result["ping_ms"],
                    result["bytes_sent"],
                    result["bytes_received"],
                    result["external_ip"],
                    result["raw_json"],
                ),
            )

    def latest_speedtest(self) -> dict | None:
        with self.connection() as conn:
            row = conn.execute(
                "SELECT * FROM speedtests ORDER BY recorded_at DESC LIMIT 1"
            ).fetchone()
        return dict(row) if row else None

    def earliest_speedtest(self) -> dict | None:
        with self.connection() as conn:
            row = conn.execute(
                "SELECT * FROM speedtests ORDER BY recorded_at ASC LIMIT 1"
            ).fetchone()
        return dict(row) if row else None

    def earliest_ping_result(self, target_id: int) -> dict | None:
        with self.connection() as conn:
            row = conn.execute(
                "SELECT * FROM ping_results WHERE target_id = ? ORDER BY recorded_at ASC LIMIT 1",
                (target_id,),
            ).fetchone()
        return dict(row) if row else None

    def earliest_mtr_run(self, target_id: int) -> dict | None:
        with self.connection() as conn:
            row = conn.execute(
                "SELECT * FROM mtr_runs WHERE target_id = ? ORDER BY recorded_at ASC LIMIT 1",
                (target_id,),
            ).fetchone()
        return dict(row) if row else None

    def speedtest_history(self, since_iso: str, until_iso: str | None = None) -> list[dict]:
        with self.connection() as conn:
            if until_iso:
                rows = conn.execute(
                    """
                    SELECT recorded_at, download_bps, upload_bps, ping_ms
                    FROM speedtests
                    WHERE recorded_at >= ? AND recorded_at <= ?
                    ORDER BY recorded_at ASC
                    """,
                    (since_iso, until_iso),
                ).fetchall()
            else:
                rows = conn.execute(
                    """
                    SELECT recorded_at, download_bps, upload_bps, ping_ms
                    FROM speedtests
                    WHERE recorded_at >= ?
                    ORDER BY recorded_at ASC
                    """,
                    (since_iso,),
                ).fetchall()
        return [dict(row) for row in rows]

    def purge_older_than(self, cutoff_iso: str) -> None:
        with self.connection() as conn:
            conn.execute(
                "DELETE FROM speedtests WHERE recorded_at < ?",
                (cutoff_iso,),
            )
            conn.execute(
                "DELETE FROM ping_results WHERE recorded_at < ?",
                (cutoff_iso,),
            )
            conn.execute(
                "DELETE FROM mtr_runs WHERE recorded_at < ?",
                (cutoff_iso,),
            )
