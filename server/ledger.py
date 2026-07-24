"""The local traceability ledger: a SQLite database recording every print run,
the events within it, and the pieces it produced.

Pure persistence. This module never touches the network, the registry, or a
printer -- the same purity PrintQueue has. server/runlog.py does the observing
and hands finished values here.

Why SQLite and not another JSON store: PrinterStore/QueueStore read the whole
file into memory on every load and rewrite it whole on every mutation, which is
right for ten printers and wrong for an append-only event log that grows with
every print forever. sqlite3 is in the standard library, so it costs nothing in
requirements.txt and PyInstaller (master.md section 8) picks it up with no
hidden-import work.

Timestamps are ISO-8601 UTC strings and ids are uuid4 hex strings, both stored
as TEXT. That is deliberate: the same rows are destined for Postgres columns of
type timestamptz and uuid in a later phase, and TEXT round-trips into both
without a conversion layer.
"""
from __future__ import annotations

import datetime as _dt
import json
import logging
import pathlib
import sqlite3
import threading
import uuid

log = logging.getLogger("server.ledger")

SCHEMA_VERSION = 1

TABLES = (
    "schema_version", "ledger_meta", "badges", "print_runs",
    "run_events", "pieces", "run_badges", "piece_badges",
)

# Applied in order; each entry is the list of statements for that version.
# Forward-only: never edit a shipped migration, always append a new one.
MIGRATIONS = {
    1: [
        "CREATE TABLE schema_version (version INTEGER NOT NULL)",
        "CREATE TABLE ledger_meta (key TEXT PRIMARY KEY, value TEXT)",
        """CREATE TABLE badges (
             id TEXT PRIMARY KEY,
             code TEXT NOT NULL UNIQUE,
             label TEXT NOT NULL,
             severity TEXT NOT NULL,
             auto INTEGER NOT NULL DEFAULT 0,
             archived INTEGER NOT NULL DEFAULT 0,
             created_at TEXT NOT NULL,
             updated_at TEXT NOT NULL)""",
        # The nullable *_id columns below reference tables that phases 2-4
        # add (parts, orders, spools). They are created now, unconstrained
        # and unused, so the recorder and the routes are written once rather
        # than reworked three times. Nothing in phase 1 ever sets them.
        """CREATE TABLE print_runs (
             id TEXT PRIMARY KEY,
             printer_serial TEXT NOT NULL,
             printer_name TEXT NOT NULL DEFAULT '',
             order_line_id TEXT, part_id TEXT, recipe_id TEXT, spool_id TEXT,
             source TEXT NOT NULL,
             queue_job_id TEXT, slice_job_id TEXT,
             sd_path TEXT, subtask_name TEXT,
             planned_seconds REAL, planned_grams REAL,
             copies_planned INTEGER NOT NULL DEFAULT 1,
             bed_type TEXT, nozzle TEXT, material TEXT,
             started_at TEXT, ended_at TEXT,
             last_layer INTEGER, total_layers INTEGER,
             end_state TEXT,
             actual_grams REAL, actual_grams_basis TEXT,
             stopped_by_monitor INTEGER NOT NULL DEFAULT 0,
             created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
             synced_at TEXT)""",
        "CREATE INDEX ix_runs_serial ON print_runs(printer_serial, started_at)",
        """CREATE TABLE run_events (
             id TEXT PRIMARY KEY,
             run_id TEXT,
             printer_serial TEXT NOT NULL,
             ts TEXT NOT NULL,
             kind TEXT NOT NULL,
             payload TEXT,
             source TEXT NOT NULL,
             client_uuid TEXT UNIQUE,
             created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
             synced_at TEXT)""",
        "CREATE INDEX ix_events_run ON run_events(run_id, ts)",
        """CREATE TABLE pieces (
             id TEXT PRIMARY KEY,
             run_id TEXT NOT NULL,
             part_id TEXT, order_line_id TEXT,
             index_in_run INTEGER NOT NULL,
             status TEXT NOT NULL,
             inspected_by TEXT, inspected_at TEXT, notes TEXT,
             created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
             synced_at TEXT,
             UNIQUE(run_id, index_in_run))""",
        """CREATE TABLE run_badges (
             id TEXT PRIMARY KEY,
             run_id TEXT NOT NULL, badge_id TEXT NOT NULL,
             applied_by TEXT NOT NULL, applied_at TEXT NOT NULL, note TEXT,
             created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
             synced_at TEXT,
             UNIQUE(run_id, badge_id))""",
        """CREATE TABLE piece_badges (
             id TEXT PRIMARY KEY,
             piece_id TEXT NOT NULL, badge_id TEXT NOT NULL,
             applied_by TEXT NOT NULL, applied_at TEXT NOT NULL, note TEXT,
             created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
             synced_at TEXT,
             UNIQUE(piece_id, badge_id))""",
    ],
}


def now_iso() -> str:
    """ISO-8601 UTC, second-resolution, always with a +00:00 offset so the
    value is unambiguous in Postgres later."""
    return _dt.datetime.now(_dt.timezone.utc).replace(
        microsecond=0).isoformat()


def new_id() -> str:
    return uuid.uuid4().hex


class Ledger:
    """One SQLite file, one connection, one lock.

    A single lock-guarded connection rather than a connection pool: the write
    volume here is a handful of rows per print, and FastAPI's threadpool plus
    RunRecorder's thread both write, so the simplest thing that is definitely
    correct wins. The lock is never held across anything but SQLite calls.
    """

    def __init__(self, path, *, clock=now_iso):
        self.path = pathlib.Path(path)
        self._clock = clock
        self._lock = threading.Lock()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = self._connect()
        self._migrate()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(str(self.path), check_same_thread=False,
                               timeout=5.0)
        conn.row_factory = sqlite3.Row
        # WAL so a reader is never blocked by the recorder's write; busy_timeout
        # so a contended write waits rather than raising "database is locked".
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA busy_timeout=5000")
        conn.execute("PRAGMA foreign_keys=ON")
        return conn

    def _migrate(self) -> None:
        with self._lock:
            cur = self._conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' "
                "AND name='schema_version'")
            have = cur.fetchone() is not None
            current = 0
            if have:
                row = self._conn.execute(
                    "SELECT version FROM schema_version").fetchone()
                current = row["version"] if row else 0
            for version in sorted(MIGRATIONS):
                if version <= current:
                    continue
                for statement in MIGRATIONS[version]:
                    self._conn.execute(statement)
                if version == 1:
                    self._conn.execute(
                        "INSERT INTO schema_version (version) VALUES (?)",
                        (version,))
                else:
                    self._conn.execute(
                        "UPDATE schema_version SET version = ?", (version,))
                self._conn.commit()
                log.info("ledger migrated to schema version %d", version)

    # ---------------- low-level ----------------

    def query(self, sql: str, params=()) -> list[dict]:
        with self._lock:
            rows = self._conn.execute(sql, params).fetchall()
        return [dict(r) for r in rows]

    def execute(self, sql: str, params=()) -> int:
        with self._lock:
            cur = self._conn.execute(sql, params)
            self._conn.commit()
            return cur.rowcount

    def close(self) -> None:
        with self._lock:
            self._conn.close()
