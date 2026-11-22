"""SQLite storage layer.

A single connection guarded by a lock is sufficient here: the workload is a
low-rate ingest/publish API, and SQLite serializes writers anyway.  WAL mode plus
``synchronous=FULL`` keeps every acknowledged write durable.
"""

from __future__ import annotations

import json
import sqlite3
import threading
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Any, Callable, Iterator

SCHEMA = """
CREATE TABLE IF NOT EXISTS reports (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    service        TEXT    NOT NULL,
    instance       TEXT    NOT NULL,
    window_start   TEXT    NOT NULL,
    window_end     TEXT    NOT NULL,
    source_batch   TEXT    NOT NULL,
    fingerprint    TEXT    NOT NULL,
    payload        TEXT    NOT NULL,
    boundaries     TEXT    NOT NULL,
    counts         TEXT    NOT NULL,
    total_count    INTEGER NOT NULL,
    total_sum_us   REAL    NOT NULL,
    collected_at   TEXT,
    received_at    TEXT    NOT NULL,
    UNIQUE (service, instance, window_start, window_end)
);
CREATE INDEX IF NOT EXISTS idx_reports_window
    ON reports (service, window_start, window_end, received_at);
CREATE INDEX IF NOT EXISTS idx_reports_batch ON reports (service, source_batch);

CREATE TABLE IF NOT EXISTS publishes (
    publish_id     INTEGER PRIMARY KEY,
    service        TEXT    NOT NULL,
    window_start   TEXT    NOT NULL,
    window_end     TEXT    NOT NULL,
    deadline       TEXT    NOT NULL,
    p95_target_us  REAL,
    result         TEXT    NOT NULL,
    created_at     TEXT    NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_publishes_window
    ON publishes (service, window_start, window_end, publish_id);

CREATE TABLE IF NOT EXISTS seq (
    name  TEXT PRIMARY KEY,
    value INTEGER NOT NULL
);
INSERT OR IGNORE INTO seq (name, value) VALUES ('publish_id', 0);
"""


@dataclass(frozen=True)
class ReportRow:
    id: int
    service: str
    instance: str
    window_start: str
    window_end: str
    source_batch: str
    fingerprint: str
    payload: dict[str, Any]
    boundaries: list[float]
    counts: list[int]
    total_count: int
    total_sum_us: float
    collected_at: str | None
    received_at: str


def _row_to_report(row: sqlite3.Row) -> ReportRow:
    return ReportRow(
        id=row["id"],
        service=row["service"],
        instance=row["instance"],
        window_start=row["window_start"],
        window_end=row["window_end"],
        source_batch=row["source_batch"],
        fingerprint=row["fingerprint"],
        payload=json.loads(row["payload"]),
        boundaries=json.loads(row["boundaries"]),
        counts=json.loads(row["counts"]),
        total_count=row["total_count"],
        total_sum_us=row["total_sum_us"],
        collected_at=row["collected_at"],
        received_at=row["received_at"],
    )


class Store:
    def __init__(self, path: str):
        self.path = path
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(path, check_same_thread=False, isolation_level=None)
        self._conn.row_factory = sqlite3.Row
        with self._lock:
            self._conn.execute("PRAGMA journal_mode=WAL")
            self._conn.execute("PRAGMA synchronous=FULL")
            self._conn.execute("PRAGMA foreign_keys=ON")
            self._conn.executescript(SCHEMA)

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        with self._lock:
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                yield self._conn
            except Exception:
                self._conn.execute("ROLLBACK")
                raise
            else:
                self._conn.execute("COMMIT")

    # ---- reports -----------------------------------------------------------------

    def record_report(self, values: dict[str, Any]) -> tuple[ReportRow, str]:
        """Atomically insert or resolve an idempotent re-send.

        Returns ``(row, outcome)`` where outcome is ``inserted``, ``duplicate``
        (same key + same fingerprint) or ``conflict`` (same key, different
        fingerprint, existing row left untouched).
        """
        with self.transaction() as conn:
            existing = conn.execute(
                """SELECT * FROM reports
                   WHERE service=? AND instance=? AND window_start=? AND window_end=?""",
                (
                    values["service"],
                    values["instance"],
                    values["window_start"],
                    values["window_end"],
                ),
            ).fetchone()
            if existing is not None:
                row = _row_to_report(existing)
                if row.fingerprint == values["fingerprint"]:
                    return row, "duplicate"
                return row, "conflict"
            cursor = conn.execute(
                """INSERT INTO reports
                   (service, instance, window_start, window_end, source_batch,
                    fingerprint, payload, boundaries, counts, total_count,
                    total_sum_us, collected_at, received_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    values["service"],
                    values["instance"],
                    values["window_start"],
                    values["window_end"],
                    values["source_batch"],
                    values["fingerprint"],
                    json.dumps(values["payload"], sort_keys=True, ensure_ascii=False),
                    json.dumps(values["boundaries"]),
                    json.dumps(values["counts"]),
                    values["total_count"],
                    values["total_sum_us"],
                    values["collected_at"],
                    values["received_at"],
                ),
            )
            row = conn.execute("SELECT * FROM reports WHERE id=?", (cursor.lastrowid,)).fetchone()
        return _row_to_report(row), "inserted"

    def reports_for_window(
        self,
        service: str,
        window_start: str,
        window_end: str,
        received_until: str | None = None,
        conn: sqlite3.Connection | None = None,
    ) -> list[ReportRow]:
        sql = """SELECT * FROM reports
                 WHERE service=? AND window_start=? AND window_end=?"""
        params: list[Any] = [service, window_start, window_end]
        if received_until is not None:
            sql += " AND received_at <= ?"
            params.append(received_until)
        sql += " ORDER BY instance"
        if conn is not None:
            rows = conn.execute(sql, params).fetchall()
        else:
            with self._lock:
                rows = self._conn.execute(sql, params).fetchall()
        return [_row_to_report(row) for row in rows]

    def list_reports(
        self,
        service: str | None = None,
        source_batch: str | None = None,
        limit: int = 200,
    ) -> list[ReportRow]:
        clauses: list[str] = []
        params: list[Any] = []
        if service:
            clauses.append("service=?")
            params.append(service)
        if source_batch:
            clauses.append("source_batch=?")
            params.append(source_batch)
        sql = "SELECT * FROM reports"
        if clauses:
            sql += " WHERE " + " AND ".join(clauses)
        sql += " ORDER BY id DESC LIMIT ?"
        params.append(limit)
        with self._lock:
            rows = self._conn.execute(sql, params).fetchall()
        return [_row_to_report(row) for row in rows]

    # ---- publishes ---------------------------------------------------------------

    def insert_publish(
        self,
        values: dict[str, Any],
        build_result: Callable[[int, sqlite3.Connection], dict[str, Any]] | None = None,
    ) -> tuple[int, dict[str, Any]]:
        """Allocate a publish id and snapshot the result in one write transaction.

        ``build_result(publish_id, conn)`` runs *inside* the ``BEGIN IMMEDIATE``
        transaction and must read report rows through ``conn``.  A concurrent
        ingest either commits before this transaction starts (and is included)
        or blocks until it finishes (and is excluded), so a snapshot can never be
        built from a half-visible set of reports.
        """
        with self.transaction() as conn:
            conn.execute("UPDATE seq SET value = value + 1 WHERE name='publish_id'")
            publish_id = conn.execute(
                "SELECT value FROM seq WHERE name='publish_id'"
            ).fetchone()["value"]
            result = build_result(publish_id, conn) if build_result is not None else values["result"]
            conn.execute(
                """INSERT INTO publishes
                   (publish_id, service, window_start, window_end, deadline,
                    p95_target_us, result, created_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    publish_id,
                    values["service"],
                    values["window_start"],
                    values["window_end"],
                    values["deadline"],
                    values["p95_target_us"],
                    json.dumps(result, sort_keys=True, ensure_ascii=False),
                    values["created_at"],
                ),
            )
        return publish_id, result

    def get_publish(self, publish_id: int) -> dict[str, Any] | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM publishes WHERE publish_id=?", (publish_id,)
            ).fetchone()
        if row is None:
            return None
        result = json.loads(row["result"])
        return {
            "publish_id": row["publish_id"],
            "service": row["service"],
            "window_start": row["window_start"],
            "window_end": row["window_end"],
            "deadline": row["deadline"],
            "p95_target_us": row["p95_target_us"],
            "created_at": row["created_at"],
            "result": result,
        }

    def list_publishes(self, limit: int = 50) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._conn.execute(
                """SELECT publish_id, service, window_start, window_end, deadline,
                          p95_target_us, created_at, result
                   FROM publishes ORDER BY publish_id DESC LIMIT ?""",
                (limit,),
            ).fetchall()
        output = []
        for row in rows:
            result = json.loads(row["result"])
            output.append(
                {
                    "publish_id": row["publish_id"],
                    "service": row["service"],
                    "window_start": row["window_start"],
                    "window_end": row["window_end"],
                    "deadline": row["deadline"],
                    "p95_target_us": row["p95_target_us"],
                    "created_at": row["created_at"],
                    "status": result.get("p95_evaluation", {}).get("status"),
                    "missing_instances": result.get("missing_instances", []),
                }
            )
        return output
