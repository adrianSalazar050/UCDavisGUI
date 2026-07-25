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

Boot invariant (master.md section 11): the server must always be able to
start. A migration that dies partway through must leave the file exactly as
it was before it started, never half-upgraded -- see Ledger._migrate and
Ledger.transaction below.
"""
from __future__ import annotations

import contextlib
import datetime as _dt
import json
import logging
import pathlib
import sqlite3
import threading
import uuid

log = logging.getLogger("server.ledger")

SCHEMA_VERSION = 3

TABLES = (
    "schema_version", "ledger_meta", "badges", "print_runs",
    "run_events", "pieces", "run_badges", "piece_badges",
    "parts", "part_recipes",
    "filament_spools", "filament_consumption",
)

# Applied in order; each entry is the list of statements for that version.
# Forward-only: never edit a shipped migration, always append a new one.
MIGRATIONS = {
    1: [
        # id + CHECK(id = 1) makes this a real singleton: at most one row can
        # ever exist, so "the current version" is never ambiguous and never
        # needs an ORDER BY/MAX to find, and an UPDATE targeting id=1 can
        # never silently affect zero rows once the row exists.
        "CREATE TABLE schema_version "
        "(id INTEGER PRIMARY KEY CHECK (id = 1), version INTEGER NOT NULL)",
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
    2: [
        """CREATE TABLE parts (
             id TEXT PRIMARY KEY,
             part_number TEXT NOT NULL,
             revision TEXT NOT NULL DEFAULT 'A',
             name TEXT NOT NULL DEFAULT '',
             notes TEXT,
             model_filename TEXT, model_sha256 TEXT, model_bytes INTEGER,
             archived INTEGER NOT NULL DEFAULT 0,
             created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
             synced_at TEXT,
             UNIQUE(part_number, revision))""",
        """CREATE TABLE part_recipes (
             id TEXT PRIMARY KEY,
             part_id TEXT NOT NULL,
             name TEXT NOT NULL DEFAULT '',
             preset_tier TEXT, filament_material TEXT,
             nozzle TEXT, bed_type TEXT,
             supports INTEGER NOT NULL DEFAULT 0,
             copies_per_plate INTEGER NOT NULL DEFAULT 1,
             expected_seconds REAL, expected_grams REAL,
             is_default INTEGER NOT NULL DEFAULT 0,
             archived INTEGER NOT NULL DEFAULT 0,
             created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
             synced_at TEXT)""",
        "CREATE INDEX ix_recipes_part ON part_recipes(part_id)",
    ],
    3: [
        """CREATE TABLE filament_spools (
             id TEXT PRIMARY KEY,
             spool_code TEXT NOT NULL UNIQUE,
             material TEXT NOT NULL DEFAULT '',
             colour TEXT, brand TEXT,
             filament_profile TEXT,
             initial_grams REAL,
             purchase_cost REAL, currency TEXT, supplier TEXT,
             purchased_at TEXT,
             status TEXT NOT NULL DEFAULT 'sealed',
             printer_serial TEXT, ams_slot INTEGER,
             archived INTEGER NOT NULL DEFAULT 0,
             created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
             synced_at TEXT)""",
        """CREATE TABLE filament_consumption (
             id TEXT PRIMARY KEY,
             spool_id TEXT NOT NULL,
             run_id TEXT,
             grams REAL NOT NULL,
             basis TEXT,
             created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
             synced_at TEXT)""",
        "CREATE INDEX ix_consumption_spool ON filament_consumption(spool_id)",
        "CREATE INDEX ix_consumption_run ON filament_consumption(run_id)",
    ],
}


# A fixed namespace so a badge's id is a pure function of its code. Phase 4
# pulls this same catalogue down from Supabase; deriving the id from the code
# means that pull upserts onto the rows seeded here instead of creating a
# second, differently-keyed set of every badge.
BADGE_NAMESPACE = uuid.UUID("6f1b6a2e-0a1e-5c9d-9f3a-6b1c2d3e4f50")

# (code, label, severity, auto)
# `auto` means the SYSTEM may apply it. Only run-level signals qualify: a
# detection is {cls, conf, box} in frame pixels with no association to a model
# on the plate, and an HMS code describes the machine, not a part. Everything
# else is a human judgement about one physical piece.
SEED_BADGES = (
    ("spaghetti",       "Spaghetti",        "defect",  True),
    ("stringing",       "Stringing",        "warning", True),
    ("hms_error",       "HMS error",        "warning", True),
    ("autostop",        "Stopped by monitor", "defect", True),
    ("layer_shift",     "Layer shift",      "defect",  False),
    ("warped",          "Warped",           "defect",  False),
    ("poor_adhesion",   "Poor bed adhesion", "defect", False),
    ("under_extrusion", "Under-extrusion",  "defect",  False),
    ("over_extrusion",  "Over-extrusion",   "warning", False),
    ("nozzle_clog",     "Nozzle clog",      "defect",  False),
    ("detached",        "Detached from plate", "defect", False),
    ("rework",          "Needs rework",     "warning", False),
    ("scrap",           "Scrapped",         "defect",  False),
)


def badge_id_for(code: str) -> str:
    return uuid.uuid5(BADGE_NAMESPACE, code).hex


def now_iso() -> str:
    """ISO-8601 UTC, millisecond resolution. Second resolution let two
    events written in the same recorder tick collide, which matters for any
    consumer that sorts by ts alone with no other tiebreaker. Always carries
    a +00:00 offset so the value is unambiguous once it lands in a Postgres
    timestamptz later."""
    return _dt.datetime.now(_dt.timezone.utc).isoformat(timespec="milliseconds")


def new_id() -> str:
    return uuid.uuid4().hex


# Columns update_run() will write. An allowlist rather than "whatever the
# caller passed", because these values arrive from HTTP request bodies and an
# unchecked key would be interpolated straight into SQL.
RUN_WRITABLE = frozenset({
    "printer_name",
    "order_line_id", "part_id", "recipe_id", "spool_id",
    "queue_job_id", "slice_job_id", "sd_path", "subtask_name",
    "planned_seconds", "planned_grams", "copies_planned",
    "bed_type", "nozzle", "material",
    "last_layer", "total_layers", "end_state", "ended_at",
    "actual_grams", "actual_grams_basis", "stopped_by_monitor", "source",
})

END_STATES = ("FINISH", "FAILED", "STOPPED_BY_MONITOR",
              "STOPPED_BY_OPERATOR", "START_UNCONFIRMED", "UNKNOWN")

PIECE_STATUSES = ("pending_inspection", "good", "rework", "scrap")

# Columns create_part()/update_part() will write. part_number and revision
# are ordinarily set once at creation (as explicit named params on
# create_part, not through **fields) but are included here too so a rare
# catalogue correction -- fixing a typo'd part number -- can still go
# through update_part's single _checked() guard rather than needing a
# second, uncontrolled code path.
PART_WRITABLE = frozenset({
    "name", "notes", "model_filename", "model_sha256", "model_bytes",
    "part_number", "revision",
})

# `is_default` is DELIBERATELY absent: the single-default invariant is
# structural, not incidental. The only path to that column is
# set_default_recipe (clear-others-then-set, in one transaction). If
# is_default were writable here, update_recipe(is_default=1) would set a
# second default without clearing the first -- a silently-corrupt catalogue.
RECIPE_WRITABLE = frozenset({
    "name", "preset_tier", "filament_material", "nozzle", "bed_type",
    "supports", "copies_per_plate", "expected_seconds", "expected_grams",
})

# Who applies a badge. DETECTOR is the automated system's identity (the
# recorder writes machine-observed badges under it); OPERATOR is a human.
DETECTOR = "detector"
OPERATOR = "operator"
# Sources that count as a human hand and may apply ANY badge. The check is
# fail-closed on purpose: anything NOT in this set -- "detector", a typo, a
# machine identity added later -- is treated as automated and may apply only
# `auto` badges. The whole "auto badges only for the system" permission model
# hangs on this one comparison, so a novel or misspelled applier must default
# to the RESTRICTED permissions, never the permissive ones.
HUMAN_APPLIERS = frozenset({OPERATOR})


def _checked(fields: dict, allowed: frozenset) -> dict:
    """Reject any key outside the allowlist, then hand back the fields.

    ONE copy of this on purpose. These keys arrive from HTTP request bodies
    and are interpolated into SQL by the callers below, so this check is the
    whole thing standing between a request and `DROP TABLE`. Three
    hand-written copies of a security check is three chances to write one of
    them slightly differently.
    """
    bad = set(fields) - allowed
    if bad:
        raise ValueError(f"not writable columns: {sorted(bad)}")
    return fields


class Ledger:
    """One SQLite file, one connection, one lock.

    A single lock-guarded connection rather than a connection pool: the write
    volume here is a handful of rows per print, and FastAPI's threadpool plus
    RunRecorder's thread both write, so the simplest thing that is definitely
    correct wins. The lock is never held across anything but SQLite calls.

    Note this buys nothing across OTHER connections/processes by itself --
    that is what PRAGMA journal_mode=WAL and busy_timeout are for (see
    _connect). Inside this process there is exactly one connection, so it is
    self._lock, not SQLite, doing the serializing; a second reader in this
    same process is blocked by the lock, same as a writer would be.
    """

    def __init__(self, path: pathlib.Path, *, clock=now_iso):
        self.path = pathlib.Path(path)
        self._clock = clock
        self._lock = threading.RLock()
        # Depth of nested transaction() calls on the thread currently
        # holding self._lock. RLock lets the same thread re-enter (so
        # execute()/query() can be called from inside a transaction()
        # block); this counter is what stops a nested call from issuing a
        # second BEGIN, which SQLite rejects. See transaction().
        self._tx_depth = 0
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._open_or_quarantine()
        self._seed_badges()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(str(self.path), check_same_thread=False,
                               timeout=5.0)
        try:
            conn.row_factory = sqlite3.Row
            # Full manual transaction control. sqlite3's default (legacy)
            # isolation_level implicitly opens a transaction before a DML
            # statement but NOT before DDL -- a bare CREATE TABLE commits on
            # its own the instant it runs, no matter how you wrap it, which
            # is exactly what made the old _migrate non-atomic (tables could
            # commit before the schema_version stamp that was meant to
            # travel with them). isolation_level = None turns that implicit
            # behaviour off entirely, so transaction() (and _migrate, which
            # uses it) can group CREATE TABLE statements and the version
            # stamp into one real BEGIN/COMMIT unit -- SQLite's DDL genuinely
            # is transactional once you ask for the transaction yourself.
            conn.isolation_level = None
            # WAL and busy_timeout matter for OTHER connections on this same
            # file (a second process, a future admin CLI) so a writer never
            # blocks a reader at the SQLite level. Read the pragma's own
            # answer back and warn rather than assume it stuck: some
            # filesystems (network shares, and some cloud-synced folders --
            # this repo lives inside OneDrive) silently refuse WAL, and
            # PRAGMA journal_mode reports whatever mode it actually landed
            # in instead of raising.
            mode = conn.execute("PRAGMA journal_mode=WAL").fetchone()[0]
            if mode.lower() != "wal":
                log.warning(
                    "requested journal_mode=WAL but sqlite reports %r for "
                    "%s -- the filesystem may not support WAL; continuing "
                    "in %s mode", mode, self.path, mode)
            conn.execute("PRAGMA busy_timeout=5000")
            conn.execute("PRAGMA foreign_keys=ON")
            return conn
        except BaseException:
            # A corrupt file can fail as early as the WAL pragma above,
            # before _open_or_quarantine's own integrity_check ever runs.
            # Close here so the failed connection doesn't keep an OS file
            # handle open on the exception's traceback -- on Windows, that
            # leftover handle is exactly what makes the later
            # self.path.replace(...) rename fail with
            # "used by another process", even though this local `conn` is
            # never returned to the caller.
            conn.close()
            raise

    def _open_or_quarantine(self) -> None:
        """Open, verify, and migrate the database -- or move an unusable one
        aside and start fresh. Sets self._conn.

        Refusing to boot is not an option -- master.md section 11: a corrupt
        file must never stop the server, because then there is no UI left to
        fix it from. Deleting it is not an option either: it is the only
        evidence of what went wrong, so it is renamed, never removed.

        MIGRATION RUNS INSIDE THIS GUARD, not after it. A database left
        half-migrated by an older build -- tables created, version row never
        stamped -- replays its migration on the next boot and raises
        OperationalError, which is a DatabaseError subclass. Outside the
        guard that is an unbootable server forever; inside it, the file is
        quarantined and the user gets a working (if empty) ledger back.

        self._conn is assigned before _migrate() runs because _migrate() uses
        self.transaction(), which reads self._conn.
        """
        try:
            self._conn = self._connect()
            self._conn.execute("PRAGMA integrity_check").fetchone()
            self._migrate()
            return
        except sqlite3.DatabaseError as e:
            log.error("ledger at %s is unusable (%s); quarantining it and "
                      "starting a new one", self.path, e)
            # _connect() itself can be where this raises -- truly-corrupt
            # bytes fail the PRAGMA journal_mode=WAL line inside _connect(),
            # before self._conn is ever assigned on this attempt. Guard the
            # close so that case doesn't crash with AttributeError instead
            # of quarantining.
            if getattr(self, "_conn", None) is not None:
                self._conn.close()
        stamp = _dt.datetime.now(_dt.timezone.utc).strftime("%Y%m%dT%H%M%S")
        self.path.replace(self.path.with_name(
            f"{self.path.name}.corrupt-{stamp}"))
        self._conn = self._connect()
        self._migrate()

    def _migrate(self) -> None:
        # Deliberately NOT one `with self._lock:` wrapping the whole method:
        # that would nest around self.transaction()'s own lock acquisition
        # below for no real benefit -- __init__ runs this once, synchronously,
        # before `self` is visible to any other thread, so there is nothing
        # concurrent here to guard against. Reading current version first,
        # fully releasing the lock, THEN looping over transaction() calls
        # means Ledger() construction itself never depends on self._lock
        # being reentrant -- only genuinely nested application code (a
        # future execute()/query() called from inside an open transaction())
        # does. Measured: with the old shape (one lock around the whole
        # method), downgrading self._lock to a plain Lock hung every single
        # test in this file at fixture setup, since each one constructs a
        # Ledger; with this shape, only a test that itself nests calls
        # inside transaction() is affected.
        with self._lock:
            cur = self._conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' "
                "AND name='schema_version'")
            have = cur.fetchone() is not None
            current = 0
            if have:
                row = self._conn.execute(
                    "SELECT version FROM schema_version WHERE id = 1"
                ).fetchone()
                current = row["version"] if row else 0
        creating = current == 0
        for version in sorted(MIGRATIONS):
            if version <= current:
                continue
            # One transaction per version: every CREATE TABLE/INDEX for
            # this version and the version stamp that records it landed
            # either all commit together or none do. Without this, a
            # crash between the last CREATE TABLE and the stamp leaves
            # tables-with-no-version-row -- the next boot sees
            # current = 0, replays this same version, and dies on
            # "table already exists", permanently. transaction() makes
            # that window disappear.
            with self.transaction():
                for statement in MIGRATIONS[version]:
                    self._conn.execute(statement)
                self._conn.execute(
                    "INSERT OR REPLACE INTO schema_version (id, version) "
                    "VALUES (1, ?)", (version,))
            if creating:
                log.info("ledger database created at schema version %d",
                         version)
            else:
                log.info("ledger migrated to schema version %d", version)

    # ---------------- low-level ----------------

    @contextlib.contextmanager
    def transaction(self):
        """One all-or-nothing unit of work spanning multiple statements:
        commits if the block runs to completion, rolls back if it raises.
        _migrate uses this so a version's CREATE TABLEs and its
        schema_version stamp land together or not at all. Later callers use
        it the same way -- writing N piece rows for a run, or closing a run
        alongside its final event, as one unit.

        Re-entrant via SAVEPOINT, not a second BEGIN: calling transaction()
        again, or calling execute()/query(), from inside an already-open
        transaction() block opens a SAVEPOINT scoped to that nested block
        instead of joining the outer transaction outright. That is the
        difference between "the inner block's own writes roll back if it
        raises, even when an enclosing block catches the exception and
        keeps going" (correct -- SAVEPOINT) and "only the outermost level
        ever rolls back anything" (the bug an earlier version of this
        method had: an inner raise with an outer that swallowed it still
        committed the inner's writes, because depth>0 skipped both ROLLBACK
        and COMMIT and just deferred to whatever the outermost level did).

        self._lock is a threading.RLock, not a plain Lock, so this nesting
        doesn't deadlock: a plain Lock would block forever the instant code
        inside this `with` block called self.execute() or self.query(),
        both of which also take the lock.
        """
        with self._lock:
            depth = self._tx_depth
            savepoint = f"sp_{depth}"
            self._conn.execute("BEGIN" if depth == 0 else
                               f"SAVEPOINT {savepoint}")
            self._tx_depth += 1
            try:
                yield self._conn
            except BaseException:
                if depth == 0:
                    self._conn.execute("ROLLBACK")
                else:
                    self._conn.execute(f"ROLLBACK TO {savepoint}")
                    self._conn.execute(f"RELEASE {savepoint}")
                raise
            else:
                self._conn.execute("COMMIT" if depth == 0 else
                                   f"RELEASE {savepoint}")
            finally:
                self._tx_depth -= 1

    def query(self, sql: str, params=()) -> list[dict]:
        with self._lock:
            rows = self._conn.execute(sql, params).fetchall()
        return [dict(r) for r in rows]

    def execute(self, sql: str, params=()) -> int:
        """A single statement as its own transaction: commits on success,
        rolls back on failure, so a caught error (a later task turns a
        duplicate client_uuid's IntegrityError into a None return) never
        leaves an open write transaction sitting on the shared connection --
        that used to be enough on its own to make a second connection block
        with "database is locked"."""
        with self.transaction() as conn:
            cur = conn.execute(sql, params)
            return cur.rowcount

    def _seed_badges(self) -> None:
        """Idempotent: INSERT OR IGNORE on a deterministic id, so reopening
        the database is a no-op and a later cloud pull lands on these rows.
        One transaction() for the whole batch (isolation_level is None, so a
        bare executemany would autocommit per row and a lone commit() is a
        no-op) -- all rows land together or none do."""
        ts = self._clock()
        with self.transaction() as conn:
            conn.executemany(
                "INSERT OR IGNORE INTO badges "
                "(id, code, label, severity, auto, archived, "
                " created_at, updated_at) "
                "VALUES (?, ?, ?, ?, ?, 0, ?, ?)",
                [(badge_id_for(code), code, label, severity, int(auto), ts, ts)
                 for code, label, severity, auto in SEED_BADGES])

    def badges(self) -> list[dict]:
        return self.query(
            "SELECT * FROM badges WHERE archived = 0 ORDER BY code")

    # ---------------- runs ----------------

    def open_run(self, *, printer_serial: str, printer_name: str = "",
                 source: str = "unattributed", **fields) -> str:
        """Insert an open run (end_state NULL) and return its id."""
        _checked(fields, RUN_WRITABLE)
        ts = self._clock()
        run_id = new_id()
        cols = ["id", "printer_serial", "printer_name", "source",
                "started_at", "created_at", "updated_at"]
        vals = [run_id, printer_serial, printer_name or "", source, ts, ts, ts]
        for key, value in fields.items():
            cols.append(key)
            vals.append(value)
        placeholders = ", ".join("?" for _ in cols)
        self.execute(f"INSERT INTO print_runs ({', '.join(cols)}) "
                     f"VALUES ({placeholders})", vals)
        return run_id

    def find_open_run(self, printer_serial: str) -> dict | None:
        rows = self.query(
            "SELECT * FROM print_runs WHERE printer_serial = ? "
            "AND end_state IS NULL ORDER BY started_at DESC LIMIT 1",
            (printer_serial,))
        return rows[0] if rows else None

    def open_runs(self) -> list[dict]:
        return self.query(
            "SELECT * FROM print_runs WHERE end_state IS NULL")

    def update_run(self, run_id: str, **fields) -> None:
        _checked(fields, RUN_WRITABLE)
        if not fields:
            return
        sets = ", ".join(f"{k} = ?" for k in fields)
        params = list(fields.values()) + [self._clock(), run_id]
        self.execute(
            f"UPDATE print_runs SET {sets}, updated_at = ? WHERE id = ?",
            params)

    def close_run(self, run_id: str, *, end_state: str,
                  ended_at: str | None = None, **fields) -> None:
        """Set the terminal state, but ONLY on a run that is still open.

        The `end_state IS NULL` predicate is what makes this idempotent. Both
        the recorder and the start route can reach a close for the same run
        (a start that verifies, then the printer going terminal a second
        later), and the FIRST verdict is the true one -- a later re-close
        would otherwise overwrite FINISH with whatever the next poll saw.
        """
        if end_state not in END_STATES:
            raise ValueError(f"unknown end_state {end_state!r}")
        _checked(fields, RUN_WRITABLE)
        ts = self._clock()
        sets = ["end_state = ?", "ended_at = ?", "updated_at = ?"]
        params: list = [end_state, ended_at or ts, ts]
        for key, value in fields.items():
            sets.append(f"{key} = ?")
            params.append(value)
        self.execute(
            f"UPDATE print_runs SET {', '.join(sets)} "
            f"WHERE id = ? AND end_state IS NULL",
            params + [run_id])

    def get_run(self, run_id: str) -> dict | None:
        rows = self.query("SELECT * FROM print_runs WHERE id = ?", (run_id,))
        return rows[0] if rows else None

    def list_runs(self, *, serial: str | None = None, limit: int = 50,
                  offset: int = 0) -> list[dict]:
        sql = "SELECT * FROM print_runs"
        params: list = []
        if serial:
            sql += " WHERE printer_serial = ?"
            params.append(serial)
        sql += " ORDER BY started_at DESC, rowid DESC LIMIT ? OFFSET ?"
        params += [int(limit), int(offset)]
        return self.query(sql, params)

    # ---------------- events ----------------

    def add_event(self, *, printer_serial: str, kind: str, source: str,
                  run_id: str | None = None, payload=None,
                  ts: str | None = None,
                  client_uuid: str | None = None) -> str | None:
        """Append an event. Returns its id, or None when `client_uuid`
        duplicates one already stored.

        Returning None rather than raising is what makes an ingesting client's
        "retry until it works" safe: a resend after a timeout that actually
        landed is a silent no-op, not an error and not a second row.
        """
        stamp = ts or self._clock()
        event_id = new_id()
        try:
            self.execute(
                "INSERT INTO run_events (id, run_id, printer_serial, ts, "
                "kind, payload, source, client_uuid, created_at, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (event_id, run_id, printer_serial, stamp, kind,
                 json.dumps(payload) if payload is not None else None,
                 source, client_uuid, stamp, stamp))
        except sqlite3.IntegrityError:
            return None
        return event_id

    def events_for(self, run_id: str) -> list[dict]:
        rows = self.query(
            "SELECT * FROM run_events WHERE run_id = ? ORDER BY ts, rowid",
            (run_id,))
        for row in rows:
            row["payload"] = (json.loads(row["payload"])
                              if row["payload"] else None)
        return rows

    # ---------------- pieces ----------------

    def create_pieces(self, run_id: str, count: int, *,
                      part_id=None, order_line_id=None) -> int:
        """One row per planned copy, all pending_inspection. Returns how many
        were created; 0 if they already exist.

        INSERT OR IGNORE against the UNIQUE(run_id, index_in_run) constraint
        makes this idempotent, which matters because a run can reach a
        terminal state twice in the recorder's view (FAILED then IDLE) and
        must not grow a second set of pieces.

        Even on a FAILED run these default to pending_inspection rather than
        scrap: a failed print sometimes still yields usable parts, and that is
        the operator's call to make, not the recorder's.
        """
        ts = self._clock()
        rows = [(new_id(), run_id, part_id, order_line_id, i,
                 "pending_inspection", ts, ts)
                for i in range(1, int(count) + 1)]
        # One transaction() for the batch (isolation_level is None, so a
        # bare executemany would autocommit per row and a lone commit() is a
        # no-op): all N pieces land together or none do.
        with self.transaction() as conn:
            cur = conn.executemany(
                "INSERT OR IGNORE INTO pieces (id, run_id, part_id, "
                "order_line_id, index_in_run, status, created_at, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?)", rows)
            return cur.rowcount

    def pieces_for(self, run_id: str) -> list[dict]:
        return self.query(
            "SELECT * FROM pieces WHERE run_id = ? ORDER BY index_in_run",
            (run_id,))

    def get_piece(self, piece_id: str) -> dict | None:
        rows = self.query("SELECT * FROM pieces WHERE id = ?", (piece_id,))
        return rows[0] if rows else None

    def piece_counts(self, run_ids) -> dict[str, dict]:
        """-> {run_id: {total, good, scrap, rework, pending}} for the given
        runs, in ONE grouped query rather than one query per run: the history
        list renders 50 rows at a time and a per-row query there is 50 round
        trips for a column."""
        run_ids = list(run_ids)
        if not run_ids:
            return {}
        marks = ", ".join("?" for _ in run_ids)
        rows = self.query(
            f"SELECT run_id, status, COUNT(*) AS n FROM pieces "
            f"WHERE run_id IN ({marks}) GROUP BY run_id, status", run_ids)
        out: dict[str, dict] = {}
        for row in rows:
            bucket = out.setdefault(row["run_id"], {
                "total": 0, "good": 0, "scrap": 0, "rework": 0, "pending": 0})
            bucket["total"] += row["n"]
            key = row["status"] if row["status"] in (
                "good", "scrap", "rework") else "pending"
            bucket[key] += row["n"]
        return out

    def set_piece(self, piece_id: str, *, status: str | None = None,
                  inspected_by: str | None = None,
                  notes: str | None = None) -> bool:
        if status is not None and status not in PIECE_STATUSES:
            raise ValueError(f"unknown piece status {status!r}")
        ts = self._clock()
        sets, params = ["updated_at = ?"], [ts]
        if status is not None:
            sets += ["status = ?", "inspected_at = ?"]
            params += [status, ts]
        if inspected_by is not None:
            sets.append("inspected_by = ?")
            params.append(inspected_by)
        if notes is not None:
            sets.append("notes = ?")
            params.append(notes)
        params.append(piece_id)
        return self.execute(
            f"UPDATE pieces SET {', '.join(sets)} WHERE id = ?", params) > 0

    def set_pieces_bulk(self, run_id: str, status: str, *,
                        inspected_by: str | None = None,
                        overrides=None) -> int:
        """Set every piece of a run to `status`, then apply per-index
        exceptions. Returns the number of pieces touched.

        This exists because a plate of eight good parts has to be ONE action.
        If confirming a plate is tedious the verdicts stop being entered, and
        piece-level traceability quietly becomes fiction.
        """
        if status not in PIECE_STATUSES:
            raise ValueError(f"unknown piece status {status!r}")
        # Validate BOTH fields of every override up front, so a bad one raises
        # a clean ValueError before any write rather than a KeyError/TypeError
        # from inside the transaction -- the status path already did this, and
        # an unparseable index_in_run must fail the same way, not midway.
        for override in overrides or []:
            if override.get("status") not in PIECE_STATUSES:
                raise ValueError(
                    f"unknown piece status {override.get('status')!r}")
            try:
                int(override["index_in_run"])
            except (KeyError, TypeError, ValueError):
                raise ValueError(
                    f"override needs an integer index_in_run: {override!r}")
        ts = self._clock()
        # The blanket set and every override are ONE transaction: a plate must
        # never end up half "all good" and half its old verdict because the
        # process died between the two statements. Validate all overrides
        # first (above) so a bad one raises before any write, not midway.
        with self.transaction() as conn:
            touched = conn.execute(
                "UPDATE pieces SET status = ?, inspected_at = ?, "
                "inspected_by = COALESCE(?, inspected_by), updated_at = ? "
                "WHERE run_id = ?",
                (status, ts, inspected_by, ts, run_id)).rowcount
            for override in overrides or []:
                conn.execute(
                    "UPDATE pieces SET status = ?, inspected_at = ?, "
                    "inspected_by = COALESCE(?, inspected_by), updated_at = ? "
                    "WHERE run_id = ? AND index_in_run = ?",
                    (override["status"], ts, inspected_by, ts, run_id,
                     int(override["index_in_run"])))
        return touched

    # ---------------- badges ----------------

    def _badge(self, badge_id: str) -> dict | None:
        rows = self.query("SELECT * FROM badges WHERE id = ?", (badge_id,))
        return rows[0] if rows else None

    def _check_badge(self, badge_id: str, applied_by: str) -> dict:
        badge = self._badge(badge_id)
        if badge is None:
            raise ValueError(f"unknown badge {badge_id!r}")
        if applied_by not in HUMAN_APPLIERS and not badge["auto"]:
            # The split that makes piece-level verdicts meaningful: the
            # system may only apply badges it can actually observe. Fail
            # closed -- an unrecognised applier is treated as automated.
            raise ValueError(
                f"{badge['code']} is not an automatic badge; only a human "
                "can apply it")
        return badge

    def add_run_badge(self, run_id: str, badge_id: str, *,
                      applied_by: str, note: str | None = None) -> None:
        self._check_badge(badge_id, applied_by)
        ts = self._clock()
        self.execute(
            "INSERT OR IGNORE INTO run_badges (id, run_id, badge_id, "
            "applied_by, applied_at, note, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (new_id(), run_id, badge_id, applied_by, ts, note, ts, ts))

    def remove_run_badge(self, run_id: str, badge_id: str) -> bool:
        return self.execute(
            "DELETE FROM run_badges WHERE run_id = ? AND badge_id = ?",
            (run_id, badge_id)) > 0

    def run_badges(self, run_id: str) -> list[dict]:
        return self.query(
            "SELECT b.code, b.label, b.severity, rb.badge_id, rb.applied_by, "
            "rb.applied_at, rb.note FROM run_badges rb "
            "JOIN badges b ON b.id = rb.badge_id WHERE rb.run_id = ? "
            "ORDER BY b.code", (run_id,))

    def add_piece_badge(self, piece_id: str, badge_id: str, *,
                        applied_by: str, note: str | None = None) -> None:
        self._check_badge(badge_id, applied_by)
        ts = self._clock()
        self.execute(
            "INSERT OR IGNORE INTO piece_badges (id, piece_id, badge_id, "
            "applied_by, applied_at, note, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (new_id(), piece_id, badge_id, applied_by, ts, note, ts, ts))

    def remove_piece_badge(self, piece_id: str, badge_id: str) -> bool:
        return self.execute(
            "DELETE FROM piece_badges WHERE piece_id = ? AND badge_id = ?",
            (piece_id, badge_id)) > 0

    def piece_badges(self, piece_id: str) -> list[dict]:
        return self.query(
            "SELECT b.code, b.label, b.severity, pb.badge_id, pb.applied_by, "
            "pb.applied_at, pb.note FROM piece_badges pb "
            "JOIN badges b ON b.id = pb.badge_id WHERE pb.piece_id = ? "
            "ORDER BY b.code", (piece_id,))

    # ---------------- parts ----------------

    def create_part(self, *, part_number: str, revision: str = "A",
                    name: str = "", notes: str | None = None,
                    **fields) -> str:
        """Insert a catalogue part and return its id.

        UNIQUE(part_number, revision) on the table (schema v2) is what
        actually enforces "no duplicate revision" -- this raises
        sqlite3.IntegrityError on a collision rather than silently upserting,
        because a part catalogue entry is meant to be created once and
        revised by adding a new row, not overwritten in place.
        """
        _checked(fields, PART_WRITABLE)
        ts = self._clock()
        part_id = new_id()
        cols = ["id", "part_number", "revision", "name", "notes",
                "created_at", "updated_at"]
        vals = [part_id, part_number, revision, name, notes, ts, ts]
        for key, value in fields.items():
            cols.append(key)
            vals.append(value)
        placeholders = ", ".join("?" for _ in cols)
        self.execute(f"INSERT INTO parts ({', '.join(cols)}) "
                     f"VALUES ({placeholders})", vals)
        return part_id

    def get_part(self, part_id: str) -> dict | None:
        rows = self.query("SELECT * FROM parts WHERE id = ?", (part_id,))
        return rows[0] if rows else None

    def find_part(self, part_number: str, revision: str) -> dict | None:
        rows = self.query(
            "SELECT * FROM parts WHERE part_number = ? AND revision = ?",
            (part_number, revision))
        return rows[0] if rows else None

    def list_parts(self, *, include_archived: bool = False) -> list[dict]:
        sql = "SELECT * FROM parts"
        if not include_archived:
            sql += " WHERE archived = 0"
        sql += " ORDER BY part_number, revision"
        return self.query(sql)

    def update_part(self, part_id: str, **fields) -> None:
        _checked(fields, PART_WRITABLE)
        if not fields:
            return
        sets = ", ".join(f"{k} = ?" for k in fields)
        params = list(fields.values()) + [self._clock(), part_id]
        self.execute(
            f"UPDATE parts SET {sets}, updated_at = ? WHERE id = ?", params)

    def archive_part(self, part_id: str) -> None:
        ts = self._clock()
        self.execute(
            "UPDATE parts SET archived = 1, updated_at = ? WHERE id = ?",
            (ts, part_id))

    # ---------------- recipes ----------------

    def add_recipe(self, part_id: str, *, name: str = "", **fields) -> str:
        """Insert a slicing recipe for a part and return its id."""
        _checked(fields, RECIPE_WRITABLE)
        ts = self._clock()
        recipe_id = new_id()
        cols = ["id", "part_id", "name", "created_at", "updated_at"]
        vals = [recipe_id, part_id, name, ts, ts]
        for key, value in fields.items():
            cols.append(key)
            vals.append(value)
        placeholders = ", ".join("?" for _ in cols)
        self.execute(f"INSERT INTO part_recipes ({', '.join(cols)}) "
                     f"VALUES ({placeholders})", vals)
        return recipe_id

    def recipes_for(self, part_id: str, *,
                    include_archived: bool = False) -> list[dict]:
        sql = "SELECT * FROM part_recipes WHERE part_id = ?"
        if not include_archived:
            sql += " AND archived = 0"
        sql += " ORDER BY is_default DESC, name, rowid"
        return self.query(sql, (part_id,))

    def get_recipe(self, recipe_id: str) -> dict | None:
        rows = self.query(
            "SELECT * FROM part_recipes WHERE id = ?", (recipe_id,))
        return rows[0] if rows else None

    def update_recipe(self, recipe_id: str, **fields) -> None:
        _checked(fields, RECIPE_WRITABLE)
        if not fields:
            return
        sets = ", ".join(f"{k} = ?" for k in fields)
        params = list(fields.values()) + [self._clock(), recipe_id]
        self.execute(
            f"UPDATE part_recipes SET {sets}, updated_at = ? WHERE id = ?",
            params)

    def set_default_recipe(self, part_id: str, recipe_id: str) -> None:
        """Make `recipe_id` the sole default for its part. The clear-then-set
        is ONE transaction so the part is never left with zero defaults or two
        -- 'exactly one default' must hold at every observable moment.

        Refuses a recipe that does not belong to `part_id`: otherwise a caller
        passing a mismatched pair would clear THIS part's defaults while
        stamping a FOREIGN part's recipe as this part's default -- corrupting
        both. Enforced here, at the data layer, so no route can reach it."""
        recipe = self.get_recipe(recipe_id)
        if recipe is None or recipe["part_id"] != part_id:
            raise ValueError(
                f"recipe {recipe_id!r} does not belong to part {part_id!r}")
        ts = self._clock()
        with self.transaction() as conn:
            conn.execute(
                "UPDATE part_recipes SET is_default = 0, updated_at = ? "
                "WHERE part_id = ?", (ts, part_id))
            conn.execute(
                "UPDATE part_recipes SET is_default = 1, updated_at = ? "
                "WHERE id = ?", (ts, recipe_id))

    def default_recipe(self, part_id: str) -> dict | None:
        rows = self.query(
            "SELECT * FROM part_recipes WHERE part_id = ? AND is_default = 1 "
            "AND archived = 0 LIMIT 1", (part_id,))
        return rows[0] if rows else None

    def archive_recipe(self, recipe_id: str) -> None:
        ts = self._clock()
        self.execute(
            "UPDATE part_recipes SET archived = 1, updated_at = ? "
            "WHERE id = ?", (ts, recipe_id))

    def close(self) -> None:
        """Closing twice is safe -- sqlite3.Connection.close() is a no-op on
        a connection that is already closed. Calling query()/execute()
        AFTER close() is not: it raises sqlite3.ProgrammingError, so a
        caller that keeps a background thread alive (RunRecorder, later)
        must tolerate that at shutdown rather than assume close() waits for
        in-flight callers."""
        with self._lock:
            self._conn.close()
