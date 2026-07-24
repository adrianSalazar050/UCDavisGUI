# ERP Traceability Phase 1 — Local Ledger + Run Recording Implementation Plan

> **STATUS: NOT STARTED (written 2026-07-24).** No task below has been
> executed. Implements **Phase 1 only** of
> `docs/superpowers/specs/2026-07-24-erp-traceability-design.md`.
>
> Historical record from the moment of writing, not maintained afterwards.
> **`master.md` is authoritative wherever this file disagrees with it.**

> **For agentic workers:** REQUIRED SUB-SKILL: Use
> superpowers:subagent-driven-development (recommended) or
> superpowers:executing-plans to implement this plan task-by-task. Steps use
> checkbox (`- [ ]`) syntax for tracking.

**Goal:** Give the server a durable local record of every print that runs on
it — what printed, on which machine, for how long, how it ended, and what each
resulting piece was judged to be.

**Architecture:** A SQLite database (`ledger.db`) beside `printers.json`,
owned by a new pure-persistence module `server/ledger.py`. A daemon thread
`RunRecorder` in `server/runlog.py` polls `registry.summaries()` and turns
`gcode_state` transitions into run and event rows. The queue start route opens
the run row *before* publishing so attribution is never lost to a race. New
`/api/runs`, `/api/pieces` routes and a History page expose it. Everything is
inert when `ledger=None`.

**Tech Stack:** Python 3.11, `sqlite3` (standard library — no new
dependency), FastAPI, pytest, React 19 + Vite, vitest.

**Not in this phase:** the parts catalogue, filament spools, Supabase sync,
and arm ingest. Those are Phases 2–5 of the spec and no task here may
implement them.

---

## Background an engineer needs before Task 1

**Nothing currently records that a print happened.** The queue deletes a job
once its start is confirmed, slice jobs are never persisted, and the start
route stops watching once verified. There is no history table anywhere. That
is what this phase adds.

**Read these first:**

- `docs/superpowers/specs/2026-07-24-erp-traceability-design.md` — the design.
  §4 is the data model, §5 the recorder, §6 the routes.
- `master.md` §5.4 (starting a print, and why a job is dequeued only on
  confirmation) and §11 (the gotchas — several apply directly).

**Conventions in this repo that this phase must follow:**

1. **"None means inert."** `create_app(..., queue=None, detection=None,
   slicer=None)` disables those feature areas entirely and their routes 404.
   `ledger=None` joins them.
2. **Injectable seams for anything with a clock or a thread.**
   `DetectionCoordinator` takes `tick_s` and a `controller_factory`;
   `SliceCoordinator` takes `run`, `parse`, `clock`. Tests never sleep and
   never touch hardware.
3. **A background loop never dies on one bad tick.** See
   `DetectionCoordinator._loop` in `server/detection.py` — it catches
   `Exception`, logs, and continues.
4. **Two-lock discipline.** A lock guarding in-memory state is never held
   across I/O. See `server/registry.py`.

**One correction to the spec worth knowing before writing the recorder:**
spec §5 says HMS codes are formatted by `bambu_link.decode_hms`. They already
are — `build_summary()` in `server/printer.py:123-135` decodes them and puts
the `AAAA_BBBB_CCCC_DDDD` strings in `summary["hms"]`. The recorder diffs that
list of strings directly and must **not** call `decode_hms` itself.

---

## File Structure

**Create:**

| Path | Responsibility |
|---|---|
| `server/ledger.py` | The SQLite database only: connection, schema, migrations, corrupt-file recovery, and typed row helpers. No network, no registry, no threads |
| `server/runlog.py` | `RunRecorder`: the polling thread that turns summary diffs into rows. Reads the registry, writes the ledger, owns no state that outlives a tick except the previous snapshot |
| `server/tests/test_ledger.py` | Schema, migrations, corrupt recovery, row helpers |
| `server/tests/test_runlog.py` | Every transition, against a fake registry |
| `server/tests/test_ledger_api.py` | Every route, against a temp database |
| `frontend/src/pages/History.jsx` | The page: printer-scoped run list plus a selected run's detail |
| `frontend/src/components/history/RunTable.jsx` | The run list table |
| `frontend/src/components/history/RunDetail.jsx` | One run: fields, event timeline, badges |
| `frontend/src/components/history/PieceGrid.jsx` | The piece verdict UI, including the bulk action |
| `frontend/src/components/history/runFormat.js` | **Pure** formatting/rollup helpers — the only frontend code here that gets unit tests |
| `frontend/src/components/history/runFormat.test.js` | vitest for the above |

**Modify:**

| Path | Change |
|---|---|
| `server/main.py` | `ledger=None` parameter, the run/piece/badge routes, and the start-route integration |
| `server/__main__.py` | Build a `Ledger` and a `RunRecorder`, pass them to `create_app`, add to the lifespan |
| `frontend/src/api/printer.js` | Fetch wrappers for the new routes |
| `frontend/src/app/pageRegistry.jsx` | Register the History page |
| `.gitignore` | `ledger.db*` |
| `master.md` | New §13 |
| `docs/superpowers/README.md` | Index rows for the spec and this plan |

---

## Task 1: Ledger schema and migrations

**Files:**
- Create: `server/ledger.py`
- Test: `server/tests/test_ledger.py`

- [ ] **Step 1: Write the failing test**

Create `server/tests/test_ledger.py`:

```python
import pathlib
import sqlite3

import pytest

from server.ledger import SCHEMA_VERSION, TABLES, Ledger


@pytest.fixture
def led(tmp_path):
    ledger = Ledger(tmp_path / "ledger.db")
    yield ledger
    ledger.close()


def test_creates_every_table(led):
    names = {r["name"] for r in led.query(
        "SELECT name FROM sqlite_master WHERE type='table'")}
    assert set(TABLES) <= names


def test_records_schema_version(led):
    assert led.query("SELECT version FROM schema_version")[0]["version"] == \
        SCHEMA_VERSION


def test_reopening_does_not_re_migrate(tmp_path):
    path = tmp_path / "ledger.db"
    first = Ledger(path)
    first.close()
    second = Ledger(path)
    try:
        rows = second.query("SELECT version FROM schema_version")
        assert len(rows) == 1, "migrations ran twice on reopen"
    finally:
        second.close()


def test_wal_mode_is_on(led):
    mode = led.query("PRAGMA journal_mode")[0]["journal_mode"]
    assert mode.lower() == "wal"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest server/tests/test_ledger.py -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'server.ledger'`

- [ ] **Step 3: Write minimal implementation**

Create `server/ledger.py`:

```python
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
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest server/tests/test_ledger.py -q`
Expected: PASS (4 passed)

- [ ] **Step 5: Commit**

```bash
git add server/ledger.py server/tests/test_ledger.py
git commit -m "feat(ledger): SQLite schema and forward-only migrations"
```

---

## Task 2: Corrupt-database recovery

A corrupt `ledger.db` must not stop the server booting, and must not be
deleted — `master.md` §11's boot invariant, plus the observation that a corrupt
file is the only evidence of what went wrong.

**Files:**
- Modify: `server/ledger.py`
- Test: `server/tests/test_ledger.py`

- [ ] **Step 1: Write the failing test**

Append to `server/tests/test_ledger.py`:

```python
def test_corrupt_database_is_quarantined_not_deleted(tmp_path, caplog):
    path = tmp_path / "ledger.db"
    path.write_bytes(b"this is not a database, not even slightly")

    ledger = Ledger(path)
    try:
        # It booted, and it works.
        assert ledger.query("SELECT version FROM schema_version")[0][
            "version"] == SCHEMA_VERSION
    finally:
        ledger.close()

    # The bad bytes are still on disk under a new name.
    quarantined = list(tmp_path.glob("ledger.db.corrupt-*"))
    assert len(quarantined) == 1, "the corrupt file was not preserved"
    assert quarantined[0].read_bytes().startswith(b"this is not a database")
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest server/tests/test_ledger.py::test_corrupt_database_is_quarantined_not_deleted -q`
Expected: FAIL — `sqlite3.DatabaseError: file is not a database`

- [ ] **Step 3: Write minimal implementation**

**Important — `_migrate()` now uses `self.transaction()`, which reads
`self._conn`.** So `self._conn` must be assigned *before* `_migrate()` runs.
Do NOT change `_migrate`'s signature; leave it reading `self._conn`. Instead
have `_open_or_quarantine` assign `self._conn` itself, and have `__init__`
just call it.

In `server/ledger.py`, replace the two lines in `__init__` that read

```python
        self._conn = self._connect()
        self._migrate()
```

with a single call (it assigns `self._conn` as a side effect):

```python
        self._open_or_quarantine()
```

Then add this method immediately after `_connect`:

```python
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
        stamp = _dt.datetime.now(_dt.timezone.utc).strftime("%Y%m%dT%H%M%S")
        self.path.replace(self.path.with_name(
            f"{self.path.name}.corrupt-{stamp}"))
        self._conn = self._connect()
        self._migrate()
```

> One subtlety the implementer must handle: a corrupt file can fail the
> `integrity_check` *or* fail inside `_migrate()` (the half-migrated case),
> and in the latter case `self._conn` is an open handle to the bad file. The
> `self.path.replace(...)` on Windows will fail if that handle is still open,
> so close `self._conn` before the rename. Add `self._conn.close()` right
> after the `log.error(...)` line.

Add a second test alongside the one above, for the half-migrated case
specifically — it is a *different* failure from corrupt bytes, and it is the
one that would otherwise be unrecoverable:

```python
def test_a_half_migrated_database_is_quarantined_too(tmp_path):
    """Tables present, version row never stamped -- what an interrupted
    migration from an older build leaves behind. The replay raises
    OperationalError, and that must quarantine rather than refuse to boot."""
    path = tmp_path / "ledger.db"
    scratch = sqlite3.connect(str(path))
    scratch.execute(
        "CREATE TABLE schema_version (id INTEGER PRIMARY KEY CHECK (id = 1), "
        "version INTEGER NOT NULL)")
    scratch.commit()
    scratch.close()

    ledger = Ledger(path)
    try:
        assert ledger.query("SELECT version FROM schema_version")[0][
            "version"] == SCHEMA_VERSION
        assert ledger.query("SELECT * FROM print_runs") == []
    finally:
        ledger.close()
    assert len(list(tmp_path.glob("ledger.db.corrupt-*"))) == 1
```

> This test needs `import sqlite3` in the test file. Task 1's cleanup removed
> that import as unused — add it back here, where it is genuinely used.

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest server/tests/test_ledger.py -q`
Expected: PASS — both new tests green, plus everything from Task 1.

- [ ] **Step 5: Verify the guard actually guards**

Temporarily make `_open_or_quarantine` do nothing but `self._conn =
self._connect(); self._migrate()` — no `integrity_check`, no `except` — and
rerun.
Expected: **both** new tests FAIL, the first with `sqlite3.DatabaseError: file
is not a database` and the second with `sqlite3.OperationalError: table
schema_version already exists`. Restore the method.

This step is not ceremony — `server/tests/test_docs.py`'s docstring records
that a guard never seen to fail is decoration.

- [ ] **Step 6: Commit**

```bash
git add server/ledger.py server/tests/test_ledger.py
git commit -m "feat(ledger): quarantine a corrupt database instead of failing to boot"
```

---

## Task 3: Seed the badge catalogue

**Files:**
- Modify: `server/ledger.py`
- Test: `server/tests/test_ledger.py`

- [ ] **Step 1: Write the failing test**

Append to `server/tests/test_ledger.py`:

```python
from server.ledger import SEED_BADGES, badge_id_for


def test_seeds_the_badge_catalogue(led):
    codes = {b["code"] for b in led.badges()}
    assert codes == {code for code, _, _, _ in SEED_BADGES}


def test_seed_badge_ids_are_deterministic(tmp_path):
    a = Ledger(tmp_path / "a.db")
    b = Ledger(tmp_path / "b.db")
    try:
        ids_a = {x["code"]: x["id"] for x in a.badges()}
        ids_b = {x["code"]: x["id"] for x in b.badges()}
        assert ids_a == ids_b
        assert ids_a["spaghetti"] == badge_id_for("spaghetti")
    finally:
        a.close()
        b.close()


def test_reseeding_does_not_duplicate(tmp_path):
    path = tmp_path / "ledger.db"
    first = Ledger(path)
    count = len(first.badges())
    first.close()
    second = Ledger(path)
    try:
        assert len(second.badges()) == count
    finally:
        second.close()


def test_only_auto_badges_are_marked_auto(led):
    auto = {b["code"] for b in led.badges() if b["auto"]}
    assert auto == {"spaghetti", "stringing", "hms_error", "autostop"}
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest server/tests/test_ledger.py -q`
Expected: FAIL — `ImportError: cannot import name 'SEED_BADGES'`

- [ ] **Step 3: Write minimal implementation**

Add to `server/ledger.py`, after `MIGRATIONS`:

```python
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
```

In `Ledger.__init__`, add a seed call after `self._migrate()`:

```python
        self._migrate()
        self._seed_badges()
```

And add these methods to `Ledger`:

```python
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
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest server/tests/test_ledger.py -q`
Expected: PASS (16 passed — 12 from Tasks 1-2 plus the 4 new badge tests)

- [ ] **Step 5: Commit**

```bash
git add server/ledger.py server/tests/test_ledger.py
git commit -m "feat(ledger): seed the badge catalogue with deterministic ids"
```

---

## Task 4: Run rows — open, find, update, close

**Files:**
- Modify: `server/ledger.py`
- Test: `server/tests/test_ledger.py`

- [ ] **Step 1: Write the failing test**

Append to `server/tests/test_ledger.py`:

```python
def test_open_run_returns_an_id_and_leaves_it_open(led):
    run_id = led.open_run(printer_serial="S1", printer_name="A1",
                          source="unattributed")
    run = led.get_run(run_id)
    assert run["end_state"] is None
    assert run["printer_serial"] == "S1"
    assert run["printer_name"] == "A1"
    assert run["started_at"] is not None


def test_find_open_run_finds_only_the_open_one(led):
    closed = led.open_run(printer_serial="S1", printer_name="A1",
                          source="queue")
    led.close_run(closed, end_state="FINISH")
    assert led.find_open_run("S1") is None

    open_id = led.open_run(printer_serial="S1", printer_name="A1",
                           source="queue")
    assert led.find_open_run("S1")["id"] == open_id
    assert led.find_open_run("S2") is None


def test_update_run_sets_only_named_fields(led):
    run_id = led.open_run(printer_serial="S1", printer_name="A1",
                          source="queue")
    led.update_run(run_id, last_layer=42, total_layers=100)
    run = led.get_run(run_id)
    assert run["last_layer"] == 42
    assert run["total_layers"] == 100
    assert run["source"] == "queue"


def test_update_run_rejects_an_unknown_column(led):
    run_id = led.open_run(printer_serial="S1", printer_name="A1",
                          source="queue")
    with pytest.raises(ValueError):
        led.update_run(run_id, drop_table="oops")


def test_close_run_stamps_end_state_and_ended_at(led):
    run_id = led.open_run(printer_serial="S1", printer_name="A1",
                          source="queue")
    led.close_run(run_id, end_state="FINISH")
    run = led.get_run(run_id)
    assert run["end_state"] == "FINISH"
    assert run["ended_at"] is not None


def test_close_run_is_idempotent(led):
    run_id = led.open_run(printer_serial="S1", printer_name="A1",
                          source="queue")
    led.close_run(run_id, end_state="FINISH")
    first = led.get_run(run_id)["ended_at"]
    led.close_run(run_id, end_state="FAILED")
    again = led.get_run(run_id)
    assert again["end_state"] == "FINISH", "a closed run was reopened"
    assert again["ended_at"] == first


def test_list_runs_is_newest_first_and_filterable(led):
    a = led.open_run(printer_serial="S1", printer_name="A1", source="queue")
    b = led.open_run(printer_serial="S2", printer_name="A1m", source="queue")
    ids = [r["id"] for r in led.list_runs()]
    assert set(ids) == {a, b}
    assert [r["id"] for r in led.list_runs(serial="S2")] == [b]
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest server/tests/test_ledger.py -q`
Expected: FAIL — `AttributeError: 'Ledger' object has no attribute 'open_run'`

- [ ] **Step 3: Write minimal implementation**

Add to `server/ledger.py`, after `badges()`:

```python
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
```

and these methods:

```python
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
```

> Parameter order in `close_run` is load-bearing: `params` collects the SET
> values in the same order the `sets` list names them, and `run_id` goes last
> because the `WHERE id = ?` placeholder is last in the SQL. Get that wrong
> and SQLite will not complain — it will silently write the id into a data
> column.

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest server/tests/test_ledger.py -q`
Expected: PASS (23 passed — 16 prior plus 7 new run tests)

- [ ] **Step 5: Commit**

```bash
git add server/ledger.py server/tests/test_ledger.py
git commit -m "feat(ledger): run rows with an idempotent close"
```

---

## Task 5: Events

**Files:**
- Modify: `server/ledger.py`
- Test: `server/tests/test_ledger.py`

- [ ] **Step 1: Write the failing test**

Append to `server/tests/test_ledger.py`:

```python
def test_add_event_records_payload_as_json(led):
    run_id = led.open_run(printer_serial="S1", source="queue")
    led.add_event(printer_serial="S1", run_id=run_id, kind="hms_raised",
                  source="server", payload={"code": "0300_1100_0002_0001"})
    events = led.events_for(run_id)
    assert len(events) == 1
    assert events[0]["kind"] == "hms_raised"
    assert events[0]["payload"] == {"code": "0300_1100_0002_0001"}


def test_events_are_returned_oldest_first(led):
    run_id = led.open_run(printer_serial="S1", source="queue")
    for kind in ("state_change", "hms_raised", "hms_cleared"):
        led.add_event(printer_serial="S1", run_id=run_id, kind=kind,
                      source="server")
    assert [e["kind"] for e in led.events_for(run_id)] == [
        "state_change", "hms_raised", "hms_cleared"]


def test_duplicate_client_uuid_is_ignored(led):
    run_id = led.open_run(printer_serial="S1", source="queue")
    first = led.add_event(printer_serial="S1", run_id=run_id,
                          kind="operator_note", source="operator",
                          client_uuid="abc")
    second = led.add_event(printer_serial="S1", run_id=run_id,
                           kind="operator_note", source="operator",
                           client_uuid="abc")
    assert first is not None
    assert second is None, "a repeated client_uuid created a second row"
    assert len(led.events_for(run_id)) == 1
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest server/tests/test_ledger.py -q`
Expected: FAIL — `AttributeError: 'Ledger' object has no attribute 'add_event'`

- [ ] **Step 3: Write minimal implementation**

Add to `server/ledger.py`:

```python
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
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest server/tests/test_ledger.py -q`
Expected: PASS (26 passed — 23 prior plus 3 new event tests)

- [ ] **Step 5: Commit**

```bash
git add server/ledger.py server/tests/test_ledger.py
git commit -m "feat(ledger): append-only run events with idempotent ingest keys"
```

---

## Task 6: Pieces and badges

**Files:**
- Modify: `server/ledger.py`
- Test: `server/tests/test_ledger.py`

- [ ] **Step 1: Write the failing test**

Append to `server/tests/test_ledger.py`:

```python
def test_create_pieces_makes_one_row_per_copy(led):
    run_id = led.open_run(printer_serial="S1", source="queue",
                          copies_planned=4)
    assert led.create_pieces(run_id, 4) == 4
    pieces = led.pieces_for(run_id)
    assert [p["index_in_run"] for p in pieces] == [1, 2, 3, 4]
    assert {p["status"] for p in pieces} == {"pending_inspection"}


def test_create_pieces_is_idempotent(led):
    run_id = led.open_run(printer_serial="S1", source="queue")
    led.create_pieces(run_id, 3)
    assert led.create_pieces(run_id, 3) == 0
    assert len(led.pieces_for(run_id)) == 3


def test_set_piece_records_the_inspector(led):
    run_id = led.open_run(printer_serial="S1", source="queue")
    led.create_pieces(run_id, 1)
    piece = led.pieces_for(run_id)[0]
    assert led.set_piece(piece["id"], status="scrap", inspected_by="adrian")
    updated = led.pieces_for(run_id)[0]
    assert updated["status"] == "scrap"
    assert updated["inspected_by"] == "adrian"
    assert updated["inspected_at"] is not None


def test_set_piece_rejects_an_unknown_status(led):
    run_id = led.open_run(printer_serial="S1", source="queue")
    led.create_pieces(run_id, 1)
    piece = led.pieces_for(run_id)[0]
    with pytest.raises(ValueError):
        led.set_piece(piece["id"], status="excellent")


def test_bulk_applies_a_status_with_per_index_overrides(led):
    run_id = led.open_run(printer_serial="S1", source="queue")
    led.create_pieces(run_id, 8)
    changed = led.set_pieces_bulk(run_id, "good", inspected_by="adrian",
                                  overrides=[{"index_in_run": 3,
                                              "status": "scrap"}])
    assert changed == 8
    by_index = {p["index_in_run"]: p["status"]
                for p in led.pieces_for(run_id)}
    assert by_index[3] == "scrap"
    assert by_index[1] == by_index[8] == "good"


def test_piece_counts_rolls_up_by_run(led):
    a = led.open_run(printer_serial="S1", source="queue")
    b = led.open_run(printer_serial="S1", source="queue")
    led.create_pieces(a, 3)
    led.create_pieces(b, 1)
    for piece in led.pieces_for(a)[:2]:
        led.set_piece(piece["id"], status="good")
    counts = led.piece_counts([a, b])
    assert counts[a] == {"total": 3, "good": 2, "scrap": 0, "rework": 0,
                         "pending": 1}
    assert counts[b]["pending"] == 1
    assert led.piece_counts([]) == {}


def test_run_badges_add_and_remove(led):
    run_id = led.open_run(printer_serial="S1", source="queue")
    led.add_run_badge(run_id, badge_id_for("spaghetti"),
                      applied_by="detector")
    assert [b["code"] for b in led.run_badges(run_id)] == ["spaghetti"]
    # Applying the same badge twice is not an error and not a second row.
    led.add_run_badge(run_id, badge_id_for("spaghetti"),
                      applied_by="detector")
    assert len(led.run_badges(run_id)) == 1
    assert led.remove_run_badge(run_id, badge_id_for("spaghetti"))
    assert led.run_badges(run_id) == []


def test_a_non_auto_badge_cannot_be_applied_by_the_detector(led):
    run_id = led.open_run(printer_serial="S1", source="queue")
    with pytest.raises(ValueError):
        led.add_run_badge(run_id, badge_id_for("warped"),
                          applied_by="detector")


def test_piece_badges_add_and_remove(led):
    run_id = led.open_run(printer_serial="S1", source="queue")
    led.create_pieces(run_id, 1)
    piece = led.pieces_for(run_id)[0]
    led.add_piece_badge(piece["id"], badge_id_for("warped"),
                        applied_by="operator")
    assert [b["code"] for b in led.piece_badges(piece["id"])] == ["warped"]
    assert led.remove_piece_badge(piece["id"], badge_id_for("warped"))
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest server/tests/test_ledger.py -q`
Expected: FAIL — `AttributeError: 'Ledger' object has no attribute 'create_pieces'`

- [ ] **Step 3: Write minimal implementation**

Add the constant near `END_STATES`:

```python
PIECE_STATUSES = ("pending_inspection", "good", "rework", "scrap")
```

and these methods to `Ledger`:

```python
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
        # One transaction() for the batch (isolation_level is None, so a bare
        # executemany would autocommit per row and a lone commit() is a
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
        for override in overrides or []:
            if override.get("status") not in PIECE_STATUSES:
                raise ValueError(
                    f"unknown piece status {override.get('status')!r}")
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
        if applied_by == "detector" and not badge["auto"]:
            # The split that makes piece-level verdicts meaningful: the
            # system may only apply badges it can actually observe.
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
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest server/tests/test_ledger.py -q`
Expected: PASS (35 passed — 26 prior plus 9 new piece/badge tests)

- [ ] **Step 5: Commit**

```bash
git add server/ledger.py server/tests/test_ledger.py
git commit -m "feat(ledger): pieces, bulk verdicts, and two-level badges"
```

---

## Task 7: `RunRecorder` opens and adopts runs

**Files:**
- Create: `server/runlog.py`
- Test: `server/tests/test_runlog.py`

- [ ] **Step 1: Write the failing test**

Create `server/tests/test_runlog.py`:

```python
import pytest

from server.ledger import Ledger
from server.runlog import RunRecorder


class FakeRegistry:
    """Emits a scripted list of summaries, one list per tick. Matches the
    shape build_summary() produces (server/printer.py) closely enough for the
    recorder: serial, name, gcode_state, layer_num, total_layer_num, hms,
    subtask_name."""

    def __init__(self, script):
        self._script = list(script)
        self._i = 0

    def summaries(self):
        if self._i < len(self._script):
            out = self._script[self._i]
            self._i += 1
            return out
        return self._script[-1] if self._script else []


def summary(serial="S1", name="A1", state="IDLE", layer=None, total=None,
            hms=(), subtask=None):
    return {"serial": serial, "name": name, "gcode_state": state,
            "layer_num": layer, "total_layer_num": total, "hms": list(hms),
            "subtask_name": subtask}


@pytest.fixture
def led(tmp_path):
    ledger = Ledger(tmp_path / "ledger.db")
    yield ledger
    ledger.close()


def run_ticks(led, script, **kwargs):
    rec = RunRecorder(FakeRegistry(script), led, **kwargs)
    for _ in script:
        rec.tick()
    return rec


def test_opens_an_unattributed_run_when_a_print_appears(led):
    run_ticks(led, [[summary(state="IDLE")],
                    [summary(state="RUNNING", layer=1, total=100)]])
    runs = led.list_runs()
    assert len(runs) == 1
    assert runs[0]["source"] == "unattributed"
    assert runs[0]["end_state"] is None


def test_prepare_counts_as_the_start_of_a_print(led):
    run_ticks(led, [[summary(state="IDLE")], [summary(state="PREPARE")]])
    assert len(led.list_runs()) == 1


def test_does_not_open_a_second_run_while_one_is_open(led):
    run_ticks(led, [[summary(state="IDLE")],
                    [summary(state="PREPARE")],
                    [summary(state="RUNNING")],
                    [summary(state="RUNNING")]])
    assert len(led.list_runs()) == 1


def test_adopts_a_run_the_start_route_already_opened(led):
    existing = led.open_run(printer_serial="S1", printer_name="A1",
                            source="queue", sd_path="/Benchy.gcode.3mf")
    run_ticks(led, [[summary(state="IDLE")],
                    [summary(state="RUNNING", layer=1, total=100)]])
    runs = led.list_runs()
    assert len(runs) == 1, "the recorder opened a duplicate run"
    assert runs[0]["id"] == existing
    assert runs[0]["source"] == "queue"


def test_records_the_printer_name_as_a_snapshot(led):
    run_ticks(led, [[summary(state="IDLE", name="Bench A1")],
                    [summary(state="RUNNING", name="Bench A1")]])
    assert led.list_runs()[0]["printer_name"] == "Bench A1"


def test_writes_a_state_change_event_on_open(led):
    run_ticks(led, [[summary(state="IDLE")], [summary(state="RUNNING")]])
    run_id = led.list_runs()[0]["id"]
    kinds = [e["kind"] for e in led.events_for(run_id)]
    assert "state_change" in kinds
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest server/tests/test_runlog.py -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'server.runlog'`

- [ ] **Step 3: Write minimal implementation**

Create `server/runlog.py`:

```python
"""RunRecorder: turn gcode_state transitions into ledger rows.

The one component that observes prints. It polls registry.summaries() and
writes to the ledger; it opens no socket, subscribes to no MQTT topic, and
sends no command, so it cannot disturb a print no matter how wrong it is.

Everything it writes is derived from the difference between two consecutive
summaries. `printer.BUSY_STATES` is reused as the "a print is happening here"
predicate rather than restating that list, so the two can never drift.

Note on HMS: build_summary() (server/printer.py) has ALREADY decoded the raw
attr/code integers into 'AAAA_BBBB_CCCC_DDDD' strings by the time a summary
reaches here. Diff those strings; do not call decode_hms again.
"""
from __future__ import annotations

import logging
import threading

from .ledger import badge_id_for
from .printer import BUSY_STATES

log = logging.getLogger("server.runlog")

TICK_S = 1.0


class RunRecorder:
    """Poll summaries, write runs and events. `detection` is optional and is
    only read (for the stopped_by_monitor latch); None means the auto-stop
    attribution simply never fires, which is what the desktop build gets."""

    def __init__(self, registry, ledger, *, detection=None,
                 tick_s: float = TICK_S):
        self.registry = registry
        self.ledger = ledger
        self.detection = detection
        self._tick_s = tick_s
        self._prev: dict[str, dict] = {}
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._loop, daemon=True)

    # ---------------- the tick ----------------

    def tick(self) -> None:
        for summary in self.registry.summaries() or []:
            serial = summary.get("serial")
            if not serial:
                continue
            try:
                self._one(serial, summary)
            except Exception as e:  # noqa: BLE001
                # One printer's bad summary must not stop the others being
                # recorded, and must never propagate into the print path.
                log.exception("run recording failed for %s: %s", serial, e)
            self._prev[serial] = summary

    def _one(self, serial: str, summary: dict) -> None:
        state = (summary.get("gcode_state") or "").upper()
        prev = self._prev.get(serial)
        prev_state = (prev.get("gcode_state") or "").upper() if prev else None
        busy = state in BUSY_STATES
        was_busy = prev_state in BUSY_STATES if prev_state else False

        if busy and not was_busy:
            self._begin(serial, summary, state)

    def _begin(self, serial: str, summary: dict, state: str) -> None:
        """Open a run, or adopt the one the start route already opened.

        Adoption is why the start route creates its row BEFORE publishing: it
        is the only place that knows the queue job, and if this tick got there
        first the attributed row and an unattributed one would both exist.
        """
        run = self.ledger.find_open_run(serial)
        if run is None:
            run_id = self.ledger.open_run(
                printer_serial=serial,
                printer_name=summary.get("name") or "",
                source="unattributed",
                subtask_name=summary.get("subtask_name"),
                total_layers=summary.get("total_layer_num"),
                last_layer=summary.get("layer_num"))
        else:
            # Adopted. Fill in only what the start route could not know --
            # the printer reports subtask_name and total_layer_num itself,
            # and it had not done so yet when the route opened the row.
            run_id = run["id"]
            fields = {}
            if not run.get("printer_name"):
                fields["printer_name"] = summary.get("name") or ""
            if summary.get("subtask_name"):
                fields["subtask_name"] = summary.get("subtask_name")
            if summary.get("total_layer_num"):
                fields["total_layers"] = summary.get("total_layer_num")
            if fields:
                self.ledger.update_run(run_id, **fields)
        self.ledger.add_event(printer_serial=serial, run_id=run_id,
                              kind="state_change", source="server",
                              payload={"to": state})

    # ---------------- thread ----------------

    def _loop(self) -> None:
        while not self._stop.wait(self._tick_s):
            try:
                self.tick()
            except Exception as e:  # noqa: BLE001
                log.exception("run recorder tick failed: %s", e)

    def start(self) -> None:
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread.is_alive():
            self._thread.join(timeout=2)
```

> `printer_name` is only backfilled when the adopted row has none. It is a
> snapshot, not a live mirror: renaming a printer next month must not rewrite
> what last month's runs say it was called.

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest server/tests/test_runlog.py -q`
Expected: PASS (6 passed)

- [ ] **Step 5: Commit**

```bash
git add server/runlog.py server/tests/test_runlog.py
git commit -m "feat(runlog): open and adopt print runs from state transitions"
```

---

## Task 8: Layer progress and HMS events

**Files:**
- Modify: `server/runlog.py`
- Test: `server/tests/test_runlog.py`

- [ ] **Step 1: Write the failing test**

Append to `server/tests/test_runlog.py`:

```python
def test_layer_progress_updates_the_run_and_writes_no_events(led):
    run_ticks(led, [[summary(state="IDLE")],
                    [summary(state="RUNNING", layer=1, total=100)],
                    [summary(state="RUNNING", layer=2, total=100)],
                    [summary(state="RUNNING", layer=3, total=100)]])
    run = led.list_runs()[0]
    assert run["last_layer"] == 3
    assert run["total_layers"] == 100
    kinds = [e["kind"] for e in led.events_for(run["id"])]
    assert kinds.count("state_change") == 1
    assert not [k for k in kinds if k.startswith("layer")], \
        "layer progress must not append events -- a 1200-layer print would " \
        "write 1200 rows of nothing"


def test_a_new_hms_code_raises_an_event(led):
    run_ticks(led, [[summary(state="IDLE")],
                    [summary(state="RUNNING")],
                    [summary(state="RUNNING", hms=["0300_1100_0002_0001"])]])
    run_id = led.list_runs()[0]["id"]
    raised = [e for e in led.events_for(run_id) if e["kind"] == "hms_raised"]
    assert len(raised) == 1
    assert raised[0]["payload"]["code"] == "0300_1100_0002_0001"


def test_an_hms_code_that_persists_does_not_re_raise(led):
    code = "0300_1100_0002_0001"
    run_ticks(led, [[summary(state="IDLE")],
                    [summary(state="RUNNING")],
                    [summary(state="RUNNING", hms=[code])],
                    [summary(state="RUNNING", hms=[code])],
                    [summary(state="RUNNING", hms=[code])]])
    run_id = led.list_runs()[0]["id"]
    raised = [e for e in led.events_for(run_id) if e["kind"] == "hms_raised"]
    assert len(raised) == 1


def test_a_cleared_hms_code_writes_a_cleared_event(led):
    code = "0300_1100_0002_0001"
    run_ticks(led, [[summary(state="IDLE")],
                    [summary(state="RUNNING")],
                    [summary(state="RUNNING", hms=[code])],
                    [summary(state="RUNNING", hms=[])]])
    run_id = led.list_runs()[0]["id"]
    kinds = [e["kind"] for e in led.events_for(run_id)]
    assert kinds.count("hms_raised") == 1
    assert kinds.count("hms_cleared") == 1


def test_an_hms_badge_is_applied_to_the_run(led):
    run_ticks(led, [[summary(state="IDLE")],
                    [summary(state="RUNNING")],
                    [summary(state="RUNNING", hms=["0300_1100_0002_0001"])]])
    run_id = led.list_runs()[0]["id"]
    assert [b["code"] for b in led.run_badges(run_id)] == ["hms_error"]
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest server/tests/test_runlog.py -q`
Expected: FAIL — `assert None == 3` on the layer test (no progress is recorded)

- [ ] **Step 3: Write minimal implementation**

In `server/runlog.py`, replace `_one` with:

```python
    def _one(self, serial: str, summary: dict) -> None:
        state = (summary.get("gcode_state") or "").upper()
        prev = self._prev.get(serial)
        prev_state = (prev.get("gcode_state") or "").upper() if prev else None
        busy = state in BUSY_STATES
        was_busy = prev_state in BUSY_STATES if prev_state else False

        if busy and not was_busy:
            self._begin(serial, summary, state)

        run = self.ledger.find_open_run(serial)
        if run is None:
            return
        if busy:
            self._progress(run, summary)
        self._hms(serial, run["id"], prev, summary)

    def _progress(self, run: dict, summary: dict) -> None:
        """Update the run's layer counters IN PLACE. Deliberately not events:
        a 100-layer print would otherwise write 100 rows containing no
        information, and a 1200-layer print 1200."""
        fields = {}
        layer = summary.get("layer_num")
        total = summary.get("total_layer_num")
        if layer is not None and layer != run.get("last_layer"):
            fields["last_layer"] = layer
        if total and total != run.get("total_layers"):
            fields["total_layers"] = total
        if fields:
            self.ledger.update_run(run["id"], **fields)

    def _hms(self, serial: str, run_id: str, prev: dict | None,
             summary: dict) -> None:
        """Diff the ALREADY-DECODED code strings build_summary() produced."""
        now = {c for c in (summary.get("hms") or []) if isinstance(c, str)}
        before = set()
        if prev:
            before = {c for c in (prev.get("hms") or []) if isinstance(c, str)}
        for code in sorted(now - before):
            self.ledger.add_event(printer_serial=serial, run_id=run_id,
                                  kind="hms_raised", source="server",
                                  payload={"code": code})
            self.ledger.add_run_badge(run_id, badge_id_for("hms_error"),
                                      applied_by="detector",
                                      note=f"HMS {code}")
        for code in sorted(before - now):
            self.ledger.add_event(printer_serial=serial, run_id=run_id,
                                  kind="hms_cleared", source="server",
                                  payload={"code": code})
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest server/tests/test_runlog.py -q`
Expected: PASS (11 passed)

- [ ] **Step 5: Commit**

```bash
git add server/runlog.py server/tests/test_runlog.py
git commit -m "feat(runlog): layer progress in place, HMS raise/clear as events"
```

---

## Task 9: Terminal states, pieces, and the grams estimate

**Files:**
- Modify: `server/runlog.py`
- Test: `server/tests/test_runlog.py`

- [ ] **Step 1: Write the failing test**

Append to `server/tests/test_runlog.py`:

```python
class FakeDetection:
    def __init__(self, stopped_by_monitor=False):
        self._snap = {"stopped_by_monitor": stopped_by_monitor}

    def snapshot(self, serial):
        return dict(self._snap)


def test_finish_closes_the_run_and_creates_pieces(led):
    existing = led.open_run(printer_serial="S1", printer_name="A1",
                            source="queue", copies_planned=4,
                            planned_grams=20.0)
    run_ticks(led, [[summary(state="IDLE")],
                    [summary(state="RUNNING", layer=100, total=100)],
                    [summary(state="FINISH", layer=100, total=100)]])
    run = led.get_run(existing)
    assert run["end_state"] == "FINISH"
    assert run["ended_at"] is not None
    assert len(led.pieces_for(existing)) == 4


def test_finish_records_planned_grams_as_the_actual(led):
    existing = led.open_run(printer_serial="S1", source="queue",
                            planned_grams=20.0)
    run_ticks(led, [[summary(state="IDLE")],
                    [summary(state="RUNNING", layer=100, total=100)],
                    [summary(state="FINISH")]])
    run = led.get_run(existing)
    assert run["actual_grams"] == pytest.approx(20.0)
    assert run["actual_grams_basis"] == "planned"


def test_a_failure_prorates_grams_by_layer_and_says_so(led):
    existing = led.open_run(printer_serial="S1", source="queue",
                            planned_grams=20.0)
    run_ticks(led, [[summary(state="IDLE")],
                    [summary(state="RUNNING", layer=25, total=100)],
                    [summary(state="FAILED", layer=25, total=100)]])
    run = led.get_run(existing)
    assert run["end_state"] == "FAILED"
    assert run["actual_grams"] == pytest.approx(5.0)
    assert run["actual_grams_basis"] == "proportional"


def test_a_monitor_stop_is_distinguished_from_a_plain_failure(led):
    run_ticks(led, [[summary(state="IDLE")],
                    [summary(state="RUNNING")],
                    [summary(state="FAILED")]],
              detection=FakeDetection(stopped_by_monitor=True))
    run = led.list_runs()[0]
    assert run["end_state"] == "STOPPED_BY_MONITOR"
    assert run["stopped_by_monitor"] == 1
    assert "autostop" in {b["code"] for b in led.run_badges(run["id"])}


def test_going_idle_without_finishing_closes_as_unknown(led):
    run_ticks(led, [[summary(state="IDLE")],
                    [summary(state="RUNNING")],
                    [summary(state="IDLE")]])
    assert led.list_runs()[0]["end_state"] == "UNKNOWN"


def test_grams_stay_null_when_nothing_was_planned(led):
    run_ticks(led, [[summary(state="IDLE")],
                    [summary(state="RUNNING", layer=5, total=100)],
                    [summary(state="FAILED")]])
    run = led.list_runs()[0]
    assert run["actual_grams"] is None
    assert run["actual_grams_basis"] is None
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest server/tests/test_runlog.py -q`
Expected: FAIL — `assert None == 'FINISH'` (nothing closes a run yet)

- [ ] **Step 3: Write minimal implementation**

In `server/runlog.py`, add the constant below `TICK_S`:

```python
# gcode_state values that mean the print is over. FAILED is included because
# master.md section 3.1 verified on hardware that a STOPPED print reports
# FAILED -- there is no separate "stopped" state to look for.
TERMINAL_STATES = ("FINISH", "FAILED", "IDLE")
```

Replace `_one`'s final block so the method ends:

```python
        run = self.ledger.find_open_run(serial)
        if run is None:
            return
        if busy:
            self._progress(run, summary)
        self._hms(serial, run["id"], prev, summary)
        if was_busy and not busy and state in TERMINAL_STATES:
            self._end(serial, run, summary, state)
```

and add:

```python
    def _end(self, serial: str, run: dict, summary: dict,
             state: str) -> None:
        run = self.ledger.get_run(run["id"]) or run
        end_state = state if state in ("FINISH", "FAILED") else "UNKNOWN"
        stopped = self._stopped_by_monitor(serial)
        if end_state == "FAILED" and stopped:
            end_state = "STOPPED_BY_MONITOR"

        grams, basis = self._grams(run, end_state)
        self.ledger.close_run(
            run["id"], end_state=end_state,
            stopped_by_monitor=int(bool(stopped)),
            **({"actual_grams": grams, "actual_grams_basis": basis}
               if basis else {}))
        self.ledger.add_event(printer_serial=serial, run_id=run["id"],
                              kind="state_change", source="server",
                              payload={"to": state,
                                       "end_state": end_state})
        if end_state == "STOPPED_BY_MONITOR":
            self.ledger.add_run_badge(run["id"], badge_id_for("autostop"),
                                      applied_by="detector")
        copies = run.get("copies_planned") or 1
        self.ledger.create_pieces(run["id"], int(copies),
                                  part_id=run.get("part_id"),
                                  order_line_id=run.get("order_line_id"))

    def _stopped_by_monitor(self, serial: str) -> bool:
        if self.detection is None:
            return False
        try:
            snap = self.detection.snapshot(serial) or {}
        except Exception as e:  # noqa: BLE001
            log.warning("detection snapshot failed for %s: %s", serial, e)
            return False
        return bool(snap.get("stopped_by_monitor"))

    @staticmethod
    def _grams(run: dict, end_state: str):
        """-> (grams, basis). The printer does NOT report filament consumed,
        so this is always an estimate and the basis column says which kind.

        The proportional estimate is wrong in detail -- layers are not equal
        mass -- which is exactly why the basis is recorded rather than the
        number being presented bare.
        """
        planned = run.get("planned_grams")
        if not planned:
            return None, None
        if end_state == "FINISH":
            return float(planned), "planned"
        layer = run.get("last_layer") or 0
        total = run.get("total_layers") or 0
        if not total:
            return None, None
        fraction = max(0.0, min(1.0, float(layer) / float(total)))
        return round(float(planned) * fraction, 3), "proportional"
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest server/tests/test_runlog.py -q`
Expected: PASS (17 passed)

- [ ] **Step 5: Commit**

```bash
git add server/runlog.py server/tests/test_runlog.py
git commit -m "feat(runlog): close runs, create pieces, estimate grams with a stated basis"
```

---

## Task 10: Startup reconciliation, and never breaking a print

**Files:**
- Modify: `server/runlog.py`
- Test: `server/tests/test_runlog.py`

- [ ] **Step 1: Write the failing test**

Append to `server/tests/test_runlog.py`:

```python
def test_reconcile_startup_closes_a_run_left_open_by_a_restart(led):
    stale = led.open_run(printer_serial="S1", printer_name="A1",
                         source="queue")
    rec = RunRecorder(FakeRegistry([[summary(state="IDLE")]]), led)
    rec.reconcile_startup()
    run = led.get_run(stale)
    assert run["end_state"] == "UNKNOWN"
    kinds = [e["kind"] for e in led.events_for(stale)]
    assert "state_change" in kinds


def test_reconcile_startup_leaves_a_genuinely_running_print_alone(led):
    live = led.open_run(printer_serial="S1", printer_name="A1",
                        source="queue")
    rec = RunRecorder(FakeRegistry([[summary(state="RUNNING")]]), led)
    rec.reconcile_startup()
    assert led.get_run(live)["end_state"] is None


def test_a_ledger_that_raises_never_escapes_the_tick(led):
    class Exploding:
        def __getattr__(self, name):
            def boom(*a, **k):
                raise RuntimeError("ledger on fire")
            return boom

    rec = RunRecorder(FakeRegistry([[summary(state="RUNNING")]]), Exploding())
    rec.tick()   # must not raise
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest server/tests/test_runlog.py -q`
Expected: FAIL — `AttributeError: 'RunRecorder' object has no attribute 'reconcile_startup'`

- [ ] **Step 3: Write minimal implementation**

Add to `RunRecorder`:

```python
    def reconcile_startup(self) -> None:
        """Close runs left open by a restart.

        A row with end_state NULL means "printing right now". After a restart
        that is only true if the printer still says so; otherwise the row
        would stay open forever and poison every "what is running" query from
        then on. UNKNOWN is the honest verdict -- we genuinely do not know how
        that print ended, and MQTT has no history to replay.
        """
        try:
            live = set()
            for summary in self.registry.summaries() or []:
                state = (summary.get("gcode_state") or "").upper()
                if state in BUSY_STATES and summary.get("serial"):
                    live.add(summary["serial"])
            for run in self.ledger.open_runs():
                if run["printer_serial"] in live:
                    continue
                self.ledger.close_run(run["id"], end_state="UNKNOWN")
                self.ledger.add_event(
                    printer_serial=run["printer_serial"], run_id=run["id"],
                    kind="state_change", source="server",
                    payload={"end_state": "UNKNOWN",
                             "reason": "server restarted while this run was "
                                       "open; its outcome was never observed"})
                log.warning("closed orphaned run %s for %s as UNKNOWN",
                            run["id"], run["printer_serial"])
        except Exception as e:  # noqa: BLE001
            log.exception("startup reconciliation failed: %s", e)
```

and make `start()` reconcile first:

```python
    def start(self) -> None:
        self.reconcile_startup()
        self._thread.start()
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest server/tests/test_runlog.py -q`
Expected: PASS (20 passed)

- [ ] **Step 5: Commit**

```bash
git add server/runlog.py server/tests/test_runlog.py
git commit -m "feat(runlog): close orphaned runs on startup, never raise from a tick"
```

---

## Task 11: Read routes — runs, one run, badges

**Files:**
- Modify: `server/main.py`
- Test: `server/tests/test_ledger_api.py`

- [ ] **Step 1: Write the failing test**

Create `server/tests/test_ledger_api.py`:

```python
import pathlib

import pytest
from fastapi.testclient import TestClient

from server.ledger import Ledger, badge_id_for
from server.main import create_app


class FakeRegistry:
    def summaries(self):
        return []

    def get(self, serial):
        return None


@pytest.fixture
def led(tmp_path):
    ledger = Ledger(tmp_path / "ledger.db")
    yield ledger
    ledger.close()


@pytest.fixture
def client(tmp_path, led):
    app = create_app(FakeRegistry(), tmp_path, ledger=led)
    return TestClient(app)


@pytest.fixture
def no_ledger_client(tmp_path):
    app = create_app(FakeRegistry(), tmp_path)
    return TestClient(app)


def test_routes_404_without_a_ledger(no_ledger_client):
    assert no_ledger_client.get("/api/runs").status_code == 404
    assert no_ledger_client.get("/api/badges").status_code == 404


def test_lists_runs_newest_first(client, led):
    a = led.open_run(printer_serial="S1", printer_name="A1", source="queue")
    b = led.open_run(printer_serial="S2", printer_name="A1m", source="queue")
    res = client.get("/api/runs")
    assert res.status_code == 200
    assert {r["id"] for r in res.json()["runs"]} == {a, b}


def test_filters_runs_by_serial(client, led):
    led.open_run(printer_serial="S1", source="queue")
    b = led.open_run(printer_serial="S2", source="queue")
    res = client.get("/api/runs", params={"serial": "S2"})
    assert [r["id"] for r in res.json()["runs"]] == [b]


def test_run_detail_includes_events_pieces_and_badges(client, led):
    run_id = led.open_run(printer_serial="S1", source="queue")
    led.add_event(printer_serial="S1", run_id=run_id, kind="state_change",
                  source="server", payload={"to": "RUNNING"})
    led.create_pieces(run_id, 2)
    led.add_run_badge(run_id, badge_id_for("spaghetti"),
                      applied_by="detector")
    body = client.get(f"/api/runs/{run_id}").json()
    assert body["run"]["id"] == run_id
    assert len(body["events"]) == 1
    assert len(body["pieces"]) == 2
    assert [b["code"] for b in body["badges"]] == ["spaghetti"]
    assert body["pieces"][0]["badges"] == []


def test_unknown_run_is_404(client):
    assert client.get("/api/runs/nope").status_code == 404


def test_the_run_list_carries_a_piece_rollup(client, led):
    run_id = led.open_run(printer_serial="S1", source="queue")
    led.create_pieces(run_id, 3)
    led.set_piece(led.pieces_for(run_id)[0]["id"], status="good")
    row = client.get("/api/runs").json()["runs"][0]
    assert row["piece_counts"] == {"total": 3, "good": 1, "scrap": 0,
                                   "rework": 0, "pending": 2}


def test_a_run_with_no_pieces_still_has_a_zeroed_rollup(client, led):
    led.open_run(printer_serial="S1", source="queue")
    row = client.get("/api/runs").json()["runs"][0]
    assert row["piece_counts"]["total"] == 0


def test_badge_catalogue_is_served(client):
    codes = {b["code"] for b in client.get("/api/badges").json()["badges"]}
    assert "spaghetti" in codes and "warped" in codes
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest server/tests/test_ledger_api.py -q`
Expected: FAIL — `TypeError: create_app() got an unexpected keyword argument 'ledger'`

- [ ] **Step 3: Write minimal implementation**

In `server/main.py`, change the `create_app` signature at line 209:

```python
def create_app(registry, runs_dir: pathlib.Path,
               frontend_dist: pathlib.Path | None = None,
               detection=None, queue=None, slicer=None, auth=None,
               ledger=None) -> FastAPI:
```

and extend its docstring with:

```
    `ledger` is a server.ledger.Ledger (or a test fake); None disables every
    traceability route, the same "None means inert" convention as `queue`,
    `detection`, and `slicer`.
```

Add a guard helper next to the existing `_require_queue` (search for that
name to find the spot) :

```python
    def _require_ledger():
        if ledger is None:
            raise HTTPException(404, "traceability is not enabled on this "
                                     "server")
```

Then add the routes. Put them after the queue routes, before the WebSocket
handler:

```python
    # --- traceability: runs, pieces, badges -------------------------------

    def _run_payload(run_id: str) -> dict:
        run = ledger.get_run(run_id)
        if run is None:
            raise HTTPException(404, "unknown run")
        pieces = []
        for piece in ledger.pieces_for(run_id):
            piece["badges"] = ledger.piece_badges(piece["id"])
            pieces.append(piece)
        return {"run": run, "events": ledger.events_for(run_id),
                "pieces": pieces, "badges": ledger.run_badges(run_id)}

    ZERO_COUNTS = {"total": 0, "good": 0, "scrap": 0, "rework": 0,
                   "pending": 0}

    @app.get("/api/runs")
    def list_runs(serial: str | None = None, limit: int = 50,
                  offset: int = 0):
        _require_ledger()
        limit = max(1, min(int(limit), 500))
        runs = ledger.list_runs(serial=serial, limit=limit,
                                offset=max(0, int(offset)))
        # One grouped query for the whole page, not one per row.
        counts = ledger.piece_counts([r["id"] for r in runs])
        for run in runs:
            run["piece_counts"] = counts.get(run["id"], dict(ZERO_COUNTS))
        return {"runs": runs}

    @app.get("/api/runs/{run_id}")
    def get_run(run_id: str):
        _require_ledger()
        return _run_payload(run_id)

    @app.get("/api/badges")
    def list_badges():
        _require_ledger()
        return {"badges": ledger.badges()}
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest server/tests/test_ledger_api.py -q`
Expected: PASS (8 passed)

- [ ] **Step 5: Verify nothing else broke**

Run: `python -m pytest -q`
Expected: PASS — no failures. `create_app`'s new parameter is keyword-only in
practice and defaults to None, so every existing call site is unaffected.

- [ ] **Step 6: Commit**

```bash
git add server/main.py server/tests/test_ledger_api.py
git commit -m "feat(api): run history read routes, inert without a ledger"
```

---

## Task 12: Write routes — run PATCH, piece verdicts, badges

**Files:**
- Modify: `server/main.py`
- Test: `server/tests/test_ledger_api.py`

- [ ] **Step 1: Write the failing test**

Append to `server/tests/test_ledger_api.py`:

```python
def test_patch_corrects_the_end_state(client, led):
    run_id = led.open_run(printer_serial="S1", source="queue")
    led.close_run(run_id, end_state="FAILED")
    res = client.patch(f"/api/runs/{run_id}",
                       json={"end_state": "STOPPED_BY_OPERATOR"})
    assert res.status_code == 200
    assert led.get_run(run_id)["end_state"] == "STOPPED_BY_OPERATOR"


def test_patch_overrides_actual_grams_and_marks_the_basis_manual(client, led):
    run_id = led.open_run(printer_serial="S1", source="queue")
    client.patch(f"/api/runs/{run_id}", json={"actual_grams": 18.4})
    run = led.get_run(run_id)
    assert run["actual_grams"] == pytest.approx(18.4)
    assert run["actual_grams_basis"] == "manual"


def test_patch_rejects_an_unknown_end_state(client, led):
    run_id = led.open_run(printer_serial="S1", source="queue")
    res = client.patch(f"/api/runs/{run_id}", json={"end_state": "GREAT"})
    assert res.status_code == 400


def test_patch_of_an_unknown_run_is_404(client):
    assert client.patch("/api/runs/nope",
                        json={"end_state": "FINISH"}).status_code == 404


def test_piece_verdict_is_recorded(client, led):
    run_id = led.open_run(printer_serial="S1", source="queue")
    led.create_pieces(run_id, 1)
    piece = led.pieces_for(run_id)[0]
    res = client.patch(f"/api/pieces/{piece['id']}",
                       json={"status": "scrap", "inspected_by": "adrian"})
    assert res.status_code == 200
    assert led.pieces_for(run_id)[0]["status"] == "scrap"


def test_piece_verdict_rejects_an_unknown_status(client, led):
    run_id = led.open_run(printer_serial="S1", source="queue")
    led.create_pieces(run_id, 1)
    piece = led.pieces_for(run_id)[0]
    res = client.patch(f"/api/pieces/{piece['id']}",
                       json={"status": "lovely"})
    assert res.status_code == 400


def test_bulk_sets_a_whole_plate_with_one_exception(client, led):
    run_id = led.open_run(printer_serial="S1", source="queue")
    led.create_pieces(run_id, 8)
    res = client.post(f"/api/runs/{run_id}/pieces/bulk",
                      json={"status": "good", "inspected_by": "adrian",
                            "overrides": [{"index_in_run": 3,
                                           "status": "scrap"}]})
    assert res.status_code == 200
    by_index = {p["index_in_run"]: p["status"] for p in led.pieces_for(run_id)}
    assert by_index[3] == "scrap"
    assert by_index[1] == by_index[8] == "good"


def test_operator_may_apply_a_human_badge_to_a_piece(client, led):
    run_id = led.open_run(printer_serial="S1", source="queue")
    led.create_pieces(run_id, 1)
    piece = led.pieces_for(run_id)[0]
    res = client.post(f"/api/pieces/{piece['id']}/badges",
                      json={"code": "warped"})
    assert res.status_code == 200
    assert [b["code"] for b in led.piece_badges(piece["id"])] == ["warped"]
    assert client.request(
        "DELETE", f"/api/pieces/{piece['id']}/badges",
        json={"code": "warped"}).status_code == 200


def test_a_badge_route_rejects_an_unknown_code(client, led):
    run_id = led.open_run(printer_serial="S1", source="queue")
    res = client.post(f"/api/runs/{run_id}/badges", json={"code": "banana"})
    assert res.status_code == 400
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest server/tests/test_ledger_api.py -q`
Expected: FAIL — 405/404 on the PATCH routes, which do not exist

- [ ] **Step 3: Write minimal implementation**

Add the Pydantic models next to the existing ones near the top of
`server/main.py` (after `ArmBody`):

```python
class PatchRun(BaseModel):
    """Operator corrections to a recorded run.

    end_state is editable because an operator stopping a print at the
    printer's own screen is indistinguishable from a genuine failure -- both
    report FAILED (master.md section 3.1) -- so the recorder writes the honest
    default and a human fixes it here.
    """

    end_state: str | None = None
    actual_grams: float | None = None
    notes: str | None = None


class PatchPiece(BaseModel):
    status: str | None = None
    inspected_by: str | None = None
    notes: str | None = None


class BulkPieces(BaseModel):
    status: str
    inspected_by: str | None = None
    overrides: list[dict] = []


class BadgeRef(BaseModel):
    code: str
    note: str | None = None
```

Add the import of the ledger constants at the top of `server/main.py`,
alongside the existing `from .store import ...`:

```python
from .ledger import END_STATES, PIECE_STATUSES
```

Then add these routes after `list_badges`:

```python
    def _badge_id(code: str) -> str:
        for badge in ledger.badges():
            if badge["code"] == code:
                return badge["id"]
        raise HTTPException(400, f"unknown badge {code!r}")

    @app.patch("/api/runs/{run_id}")
    def patch_run(run_id: str, body: PatchRun):
        """Operator corrections, including attributing a run recorded as
        unattributed. Without this every screen-started print would be
        permanent dead weight in the record."""
        _require_ledger()
        if ledger.get_run(run_id) is None:
            raise HTTPException(404, "unknown run")
        fields = {}
        if body.end_state is not None:
            if body.end_state not in END_STATES:
                raise HTTPException(
                    400, f"end_state must be one of {', '.join(END_STATES)}")
            fields["end_state"] = body.end_state
        if body.actual_grams is not None:
            # An operator-entered figure is a measurement, not an estimate --
            # the basis column has to say so, or it reads as our arithmetic.
            fields["actual_grams"] = float(body.actual_grams)
            fields["actual_grams_basis"] = "manual"
        if fields:
            ledger.update_run(run_id, **fields)
        if body.notes:
            ledger.add_event(printer_serial=ledger.get_run(
                run_id)["printer_serial"], run_id=run_id,
                kind="operator_note", source="operator",
                payload={"note": body.notes})
        return _run_payload(run_id)

    @app.patch("/api/pieces/{piece_id}")
    def patch_piece(piece_id: str, body: PatchPiece):
        _require_ledger()
        if body.status is not None and body.status not in PIECE_STATUSES:
            raise HTTPException(
                400, f"status must be one of {', '.join(PIECE_STATUSES)}")
        ok = ledger.set_piece(piece_id, status=body.status,
                              inspected_by=body.inspected_by,
                              notes=body.notes)
        if not ok:
            raise HTTPException(404, "unknown piece")
        return {"ok": True}

    @app.post("/api/runs/{run_id}/pieces/bulk")
    def bulk_pieces(run_id: str, body: BulkPieces):
        """One action for a whole plate. See the ledger's set_pieces_bulk for
        why this is not a convenience."""
        _require_ledger()
        if ledger.get_run(run_id) is None:
            raise HTTPException(404, "unknown run")
        try:
            changed = ledger.set_pieces_bulk(
                run_id, body.status, inspected_by=body.inspected_by,
                overrides=body.overrides)
        except (ValueError, KeyError, TypeError) as e:
            raise HTTPException(400, str(e))
        return {"changed": changed, **_run_payload(run_id)}

    @app.post("/api/runs/{run_id}/badges")
    def add_run_badge(run_id: str, body: BadgeRef):
        _require_ledger()
        if ledger.get_run(run_id) is None:
            raise HTTPException(404, "unknown run")
        ledger.add_run_badge(run_id, _badge_id(body.code),
                             applied_by="operator", note=body.note)
        return {"badges": ledger.run_badges(run_id)}

    @app.delete("/api/runs/{run_id}/badges")
    def remove_run_badge(run_id: str, body: BadgeRef):
        _require_ledger()
        ledger.remove_run_badge(run_id, _badge_id(body.code))
        return {"badges": ledger.run_badges(run_id)}

    @app.post("/api/pieces/{piece_id}/badges")
    def add_piece_badge(piece_id: str, body: BadgeRef):
        _require_ledger()
        ledger.add_piece_badge(piece_id, _badge_id(body.code),
                               applied_by="operator", note=body.note)
        return {"badges": ledger.piece_badges(piece_id)}

    @app.delete("/api/pieces/{piece_id}/badges")
    def remove_piece_badge(piece_id: str, body: BadgeRef):
        _require_ledger()
        ledger.remove_piece_badge(piece_id, _badge_id(body.code))
        return {"badges": ledger.piece_badges(piece_id)}
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest server/tests/test_ledger_api.py -q`
Expected: PASS (15 passed)

- [ ] **Step 5: Commit**

```bash
git add server/main.py server/tests/test_ledger_api.py
git commit -m "feat(api): run corrections, piece verdicts, bulk plate action, badges"
```

---

## Task 13: Start-route integration

The row must exist **before** the MQTT publish, so the recorder adopts it
rather than racing it — and so a start the printer ignores leaves a record.

**Files:**
- Modify: `server/main.py` (the `start_queue_job` route)
- Test: `server/tests/test_ledger_api.py`

- [ ] **Step 1: Write the failing test**

Append to `server/tests/test_ledger_api.py`:

```python
class StartableService:
    def __init__(self, serial="S1", state="IDLE", starts=True):
        self.serial = serial
        self.name = "A1"
        self._state = state
        self._starts = starts
        self.started = []

    def summary(self):
        return {"serial": self.serial, "name": self.name,
                "gcode_state": self._state, "connection": "ok"}

    def start_print(self, sd_path, plate=1):
        self.started.append((sd_path, plate))
        if self._starts:
            self._state = "PREPARE"


class StartRegistry:
    def __init__(self, service):
        self._svc = service

    def summaries(self):
        return [self._svc.summary()]

    def get(self, serial):
        return self._svc if serial == self._svc.serial else None

    def printer_model(self, serial):
        return ""


class FakeQueue:
    def __init__(self, jobs):
        self._jobs = list(jobs)

    def get(self, serial):
        return list(self._jobs)

    def remove(self, serial, job_id):
        self._jobs = [j for j in self._jobs if j.get("id") != job_id]
        return True

    def totals(self, serial):
        return {"seconds": 0, "grams": 0, "finish_epoch": None}


JOB = {"id": "j1", "sd_path": "/Benchy.gcode.3mf", "name": "Benchy",
       "seconds": 900, "grams": 12.5, "source": "3mf", "model_id": ""}


def _start_client(tmp_path, led, starts=True):
    svc = StartableService(starts=starts)
    app = create_app(StartRegistry(svc), tmp_path, queue=FakeQueue([JOB]),
                     ledger=led)
    return TestClient(app), svc


def test_a_confirmed_start_records_an_attributed_run(tmp_path, led):
    client, _ = _start_client(tmp_path, led)
    res = client.post("/api/printers/S1/queue/j1/start")
    assert res.json()["started"] is True
    runs = led.list_runs()
    assert len(runs) == 1
    assert runs[0]["source"] == "queue"
    assert runs[0]["queue_job_id"] == "j1"
    assert runs[0]["sd_path"] == "/Benchy.gcode.3mf"
    assert runs[0]["planned_grams"] == pytest.approx(12.5)
    assert runs[0]["planned_seconds"] == pytest.approx(900)
    assert runs[0]["end_state"] is None


def test_an_unconfirmed_start_is_recorded_not_forgotten(tmp_path, led):
    client, _ = _start_client(tmp_path, led, starts=False)
    res = client.post("/api/printers/S1/queue/j1/start")
    assert res.json()["started"] is False
    runs = led.list_runs()
    assert len(runs) == 1
    assert runs[0]["end_state"] == "START_UNCONFIRMED"
    kinds = [e["kind"] for e in led.events_for(runs[0]["id"])]
    assert "start_unconfirmed" in kinds


def test_the_run_row_exists_before_the_publish(tmp_path, led):
    """The ordering the recorder's adoption depends on."""
    seen = {}

    class Watching(StartableService):
        def start_print(self, sd_path, plate=1):
            seen["open_run_existed"] = led.find_open_run("S1") is not None
            super().start_print(sd_path, plate)

    svc = Watching()
    app = create_app(StartRegistry(svc), tmp_path, queue=FakeQueue([JOB]),
                     ledger=led)
    TestClient(app).post("/api/printers/S1/queue/j1/start")
    assert seen["open_run_existed"] is True


def test_starting_without_a_ledger_still_works(tmp_path):
    svc = StartableService()
    app = create_app(StartRegistry(svc), tmp_path, queue=FakeQueue([JOB]))
    res = TestClient(app).post("/api/printers/S1/queue/j1/start")
    assert res.json()["started"] is True
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest server/tests/test_ledger_api.py -q`
Expected: FAIL — `assert 0 == 1` (no run is recorded)

- [ ] **Step 3: Write minimal implementation**

In `server/main.py`'s `start_queue_job`, replace the block from the
`try: svc.start_print(...)` line through the `queue.remove(...)` line with:

```python
        # Open the ledger row BEFORE publishing. Two reasons, both load-bearing:
        # RunRecorder adopts an already-open row instead of creating one, so
        # this is what stops its 1 s tick racing us into a duplicate
        # unattributed run; and a start the printer ignores still leaves a
        # record, which nothing in this system used to keep.
        run_id = None
        if ledger is not None:
            try:
                run_id = ledger.open_run(
                    printer_serial=serial,
                    printer_name=(svc.summary().get("name") or ""),
                    source="queue",
                    queue_job_id=job.get("id"),
                    sd_path=job.get("sd_path"),
                    subtask_name=job.get("name"),
                    planned_seconds=job.get("seconds"),
                    planned_grams=job.get("grams"),
                    bed_type=_maybe(registry, "printer_bed_type", serial),
                    nozzle=_maybe(registry, "printer_nozzle", serial))
            except Exception as e:  # noqa: BLE001
                # A ledger problem must never cost a print -- master.md
                # section 11's boot invariant, one layer up.
                log.error("could not open a ledger run for %s: %s", serial, e)

        try:
            svc.start_print(job["sd_path"], plate=job.get("plate") or 1)
        except PrinterBusy as e:
            _close_run_quietly(ledger, run_id, "START_UNCONFIRMED",
                               serial, str(e))
            raise HTTPException(409, str(e))
        except SdError as e:
            _close_run_quietly(ledger, run_id, "START_UNCONFIRMED",
                               serial, str(e))
            raise HTTPException(502, str(e))

        # Read the module globals here, not as verify_start's defaults: a
        # default binds at def time and could not be overridden (tests would
        # burn the full timeout, and it would be un-tunable at runtime).
        started = verify_start(svc, timeout=START_VERIFY_S, poll=START_POLL_S)
        if not started:
            _close_run_quietly(
                ledger, run_id, "START_UNCONFIRMED", serial,
                "the printer never reported a print starting")
            return {"started": False, "job": job,
                    "detail": "the printer did not report a print starting; "
                              "the job is still queued",
                    "jobs": queue.get(serial),
                    "totals": queue.totals(serial)}
        queue.remove(serial, job_id)
```

Add these two module-level helpers to `server/main.py`, just below
`verify_start`:

```python
def _maybe(registry, method: str, serial: str):
    """Call an optional registry accessor, or None if this registry (or a
    test fake) does not have it. printer_bed_type/printer_nozzle exist on
    PrinterRegistry but not on every fake."""
    fn = getattr(registry, method, None)
    if fn is None:
        return None
    try:
        return fn(serial)
    except Exception:  # noqa: BLE001
        return None


def _close_run_quietly(ledger, run_id, end_state: str, serial: str,
                       detail: str) -> None:
    """Close a ledger run without ever letting a ledger problem surface as a
    print-path failure."""
    if ledger is None or run_id is None:
        return
    try:
        ledger.close_run(run_id, end_state=end_state)
        ledger.add_event(printer_serial=serial, run_id=run_id,
                         kind="start_unconfirmed", source="server",
                         payload={"detail": detail})
    except Exception as e:  # noqa: BLE001
        log.error("could not close ledger run %s: %s", run_id, e)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest server/tests/test_ledger_api.py -q`
Expected: PASS (19 passed)

- [ ] **Step 5: Run the whole suite**

Run: `python -m pytest -q`
Expected: PASS with no failures. `test_api.py`'s existing start-route tests
pass a registry with no ledger, which `_maybe` and the `ledger is not None`
guards handle.

- [ ] **Step 6: Commit**

```bash
git add server/main.py server/tests/test_ledger_api.py
git commit -m "feat(api): open the ledger run before publishing a start"
```

---

## Task 14: Wire it into the server entry point

**Files:**
- Modify: `server/__main__.py`, `.gitignore`

- [ ] **Step 1: Add the gitignore entry**

In `.gitignore`, beside the existing `printers.json*` and `queues.json` lines,
add:

```
ledger.db*
```

The `*` matters: SQLite writes `ledger.db-wal` and `ledger.db-shm` beside the
database, and the quarantine path is `ledger.db.corrupt-<stamp>`. This is the
same reasoning the `printers.json*` rule already uses for its temp files.

- [ ] **Step 2: Wire the ledger and recorder**

In `server/__main__.py`, add to the imports:

```python
from .ledger import Ledger
from .runlog import RunRecorder
```

After the `queue = PrintQueue(queue_store)` line, add:

```python
    # The ledger lives beside printers.json/queues.json, so it follows
    # BAMBU_DATA_DIR on the desktop build with no special handling. --mock
    # gets its own file rather than an in-memory store: unlike the queue
    # there is no MemoryLedger, and pointing it at runs-mock/ keeps mock runs
    # out of the real history just as effectively.
    ledger = Ledger((runs_dir / "ledger.db") if a.mock
                    else (runs_dir.parent / "ledger.db"))
    recorder = RunRecorder(registry, ledger, detection=coordinator)
```

Change the `create_app` call to pass it:

```python
    app = create_app(registry, runs_dir, dist, detection=coordinator,
                     queue=queue, slicer=slicer, auth=auth, ledger=ledger)
```

and close the ledger in the `finally`:

```python
    try:
        uvicorn.run(app, host=a.host, port=a.port)
    finally:
        registry.stop_all()
        ledger.close()
```

- [ ] **Step 3: Add the recorder to the app lifespan**

In `server/main.py`, add a `recorder=None` parameter to `create_app`:

```python
def create_app(registry, runs_dir: pathlib.Path,
               frontend_dist: pathlib.Path | None = None,
               detection=None, queue=None, slicer=None, auth=None,
               ledger=None, recorder=None) -> FastAPI:
```

and start it in `lifespan`, after the slicer:

```python
            if slicer is not None:
                slicer.start()
                started.append(slicer)
            if recorder is not None:
                recorder.start()
                started.append(recorder)
```

The `started` list already stops components in reverse order only if they
actually started — do not restructure it.

Then in `server/__main__.py` pass it:

```python
    app = create_app(registry, runs_dir, dist, detection=coordinator,
                     queue=queue, slicer=slicer, auth=auth, ledger=ledger,
                     recorder=recorder)
```

- [ ] **Step 4: Verify by running the server**

Run: `python -m server --mock`

Expected: the server starts, logs `ledger migrated to schema version 1` on
first run, and `runs-mock/ledger.db` exists. Open <http://127.0.0.1:8000> and
confirm the dashboard still loads. The mock "running" printer cycles
`RUNNING → FINISH → IDLE` (see `MockPrinter` in `server/printer.py`), so
within a minute:

Run: `curl http://127.0.0.1:8000/api/runs`
Expected: at least one run object with `printer_serial` `MOCK0000000001`.

Stop the server with Ctrl-C.

- [ ] **Step 5: Run the whole suite**

Run: `python -m pytest -q`
Expected: PASS with no failures.

- [ ] **Step 6: Commit**

```bash
git add server/__main__.py server/main.py .gitignore
git commit -m "feat(server): wire the ledger and run recorder into the app"
```

---

## Task 15: Frontend API wrappers and the pure formatting module

**Files:**
- Modify: `frontend/src/api/printer.js`
- Create: `frontend/src/components/history/runFormat.js`
- Create: `frontend/src/components/history/runFormat.test.js`

- [ ] **Step 1: Write the failing test**

Create `frontend/src/components/history/runFormat.test.js`:

```js
import { describe, expect, it } from "vitest";
import { formatDuration, pieceRollup, runOutcome } from "./runFormat.js";

describe("pieceRollup", () => {
  it("counts each status", () => {
    const pieces = [
      { status: "good" }, { status: "good" },
      { status: "scrap" }, { status: "pending_inspection" },
    ];
    expect(pieceRollup(pieces)).toEqual({
      total: 4, good: 2, scrap: 1, rework: 0, pending: 1,
    });
  });

  it("handles an empty plate", () => {
    expect(pieceRollup([])).toEqual({
      total: 0, good: 0, scrap: 0, rework: 0, pending: 0,
    });
  });

  it("tolerates a missing list", () => {
    expect(pieceRollup(undefined).total).toBe(0);
  });
});

describe("runOutcome", () => {
  it("labels an open run as running", () => {
    expect(runOutcome({ end_state: null })).toEqual({
      label: "Running", tone: "ok",
    });
  });

  it("distinguishes a monitor stop from a plain failure", () => {
    expect(runOutcome({ end_state: "STOPPED_BY_MONITOR" }).label)
      .toBe("Stopped by monitor");
    expect(runOutcome({ end_state: "FAILED" }).label).toBe("Failed");
  });

  it("falls back to the raw value for an unknown state", () => {
    expect(runOutcome({ end_state: "WEIRD" }).label).toBe("WEIRD");
  });
});

describe("formatDuration", () => {
  it("renders hours and minutes", () => {
    expect(formatDuration(3720)).toBe("1h 2m");
  });

  it("renders minutes alone under an hour", () => {
    expect(formatDuration(150)).toBe("2m");
  });

  it("returns a dash for nothing", () => {
    expect(formatDuration(null)).toBe("—");
    expect(formatDuration(0)).toBe("—");
  });
});
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd frontend && npm test`
Expected: FAIL — cannot resolve `./runFormat.js`

- [ ] **Step 3: Write minimal implementation**

Create `frontend/src/components/history/runFormat.js`:

```js
// Pure helpers for the History page. Extracted so they can be tested —
// the same reasoning as detection/roiGeometry.js, which is the only other
// frontend module with unit tests. Keep anything with a fetch or a hook OUT
// of this file.

const OUTCOMES = {
  FINISH: { label: "Finished", tone: "ok" },
  FAILED: { label: "Failed", tone: "bad" },
  STOPPED_BY_MONITOR: { label: "Stopped by monitor", tone: "bad" },
  STOPPED_BY_OPERATOR: { label: "Stopped by operator", tone: "warn" },
  START_UNCONFIRMED: { label: "Never started", tone: "warn" },
  UNKNOWN: { label: "Unknown", tone: "warn" },
};

export function runOutcome(run) {
  if (!run?.end_state) return { label: "Running", tone: "ok" };
  return OUTCOMES[run.end_state] ?? { label: run.end_state, tone: "warn" };
}

export function pieceRollup(pieces) {
  const out = { total: 0, good: 0, scrap: 0, rework: 0, pending: 0 };
  for (const piece of pieces ?? []) {
    out.total += 1;
    if (piece.status === "good") out.good += 1;
    else if (piece.status === "scrap") out.scrap += 1;
    else if (piece.status === "rework") out.rework += 1;
    else out.pending += 1;
  }
  return out;
}

export function formatDuration(seconds) {
  if (!seconds) return "—";
  const total = Math.round(seconds);
  const h = Math.floor(total / 3600);
  const m = Math.round((total % 3600) / 60);
  return h ? `${h}h ${m}m` : `${m}m`;
}

// Seconds between two ISO-8601 stamps, or null if either is missing.
export function elapsedSeconds(startedAt, endedAt) {
  if (!startedAt || !endedAt) return null;
  const a = Date.parse(startedAt);
  const b = Date.parse(endedAt);
  if (Number.isNaN(a) || Number.isNaN(b)) return null;
  return Math.max(0, (b - a) / 1000);
}
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd frontend && npm test`
Expected: PASS — all runFormat tests green, plus the existing roiGeometry ones.

- [ ] **Step 5: Add the fetch wrappers**

Append to `frontend/src/api/printer.js`:

```js
// --- traceability -------------------------------------------------------
// All of these 404 when the server was built without a ledger, the same
// "None means inert" convention the slice routes use.

export async function fetchRuns(serial, { limit = 50 } = {}) {
  const query = new URLSearchParams({ limit: String(limit) });
  if (serial) query.set("serial", serial);
  const res = await fetch(`/api/runs?${query}`);
  if (!res.ok) throw new Error(await detail(res));
  return res.json();
}

// { run, events, pieces (each with .badges), badges }
export async function fetchRun(runId) {
  const res = await fetch(`/api/runs/${encodeURIComponent(runId)}`);
  if (!res.ok) throw new Error(await detail(res));
  return res.json();
}

export async function patchRun(runId, body) {
  const res = await fetch(`/api/runs/${encodeURIComponent(runId)}`, {
    method: "PATCH",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
  if (!res.ok) throw new Error(await detail(res));
  return res.json();
}

export async function patchPiece(pieceId, body) {
  const res = await fetch(`/api/pieces/${encodeURIComponent(pieceId)}`, {
    method: "PATCH",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
  if (!res.ok) throw new Error(await detail(res));
  return res.json();
}

// One action for a whole plate. overrides: [{ index_in_run, status }]
export async function bulkPieces(runId, { status, inspected_by, overrides }) {
  const res = await fetch(
    `/api/runs/${encodeURIComponent(runId)}/pieces/bulk`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ status, inspected_by, overrides: overrides ?? [] }),
    });
  if (!res.ok) throw new Error(await detail(res));
  return res.json();
}

export async function fetchBadges() {
  const res = await fetch("/api/badges");
  if (!res.ok) throw new Error(await detail(res));
  return res.json();
}

export async function addPieceBadge(pieceId, code) {
  const res = await fetch(
    `/api/pieces/${encodeURIComponent(pieceId)}/badges`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ code }),
    });
  if (!res.ok) throw new Error(await detail(res));
  return res.json();
}

export async function removePieceBadge(pieceId, code) {
  const res = await fetch(
    `/api/pieces/${encodeURIComponent(pieceId)}/badges`, {
      method: "DELETE",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ code }),
    });
  if (!res.ok) throw new Error(await detail(res));
  return res.json();
}
```

- [ ] **Step 6: Commit**

```bash
git add frontend/src/api/printer.js frontend/src/components/history/
git commit -m "feat(frontend): traceability API wrappers and tested run formatting"
```

---

## Task 16: The History page

**Files:**
- Create: `frontend/src/components/history/RunTable.jsx`
- Create: `frontend/src/components/history/PieceGrid.jsx`
- Create: `frontend/src/components/history/RunDetail.jsx`
- Create: `frontend/src/pages/History.jsx`
- Modify: `frontend/src/app/pageRegistry.jsx`

- [ ] **Step 1: Create `RunTable.jsx`**

```jsx
import { formatDuration, elapsedSeconds, runOutcome } from "./runFormat.js";

// The run list for one printer. Selection is owned by the page above.
// Piece counts come from the server (run.piece_counts) rather than being
// derived here: the list endpoint deliberately does not ship every piece row.
export default function RunTable({ runs, selectedId, onSelect }) {
  if (!runs.length) {
    return <p className="muted">No runs recorded yet.</p>;
  }
  return (
    <table className="table">
      <thead>
        <tr>
          <th>Started</th><th>File</th><th>Outcome</th>
          <th>Layers</th><th>Time</th><th>Grams</th><th>Pieces</th>
        </tr>
      </thead>
      <tbody>
        {runs.map((run) => {
          const outcome = runOutcome(run);
          const rollup = run.piece_counts ?? { total: 0, good: 0 };
          return (
            <tr key={run.id}
                onClick={() => onSelect(run.id)}
                className={run.id === selectedId ? "selected" : ""}>
              <td>{(run.started_at ?? "").replace("T", " ").slice(0, 16)}</td>
              <td>{run.subtask_name ?? run.sd_path ?? "—"}</td>
              <td><span className={`pill pill-${outcome.tone}`}>
                {outcome.label}</span></td>
              <td>{run.last_layer ?? "—"}/{run.total_layers ?? "—"}</td>
              <td>{formatDuration(
                elapsedSeconds(run.started_at, run.ended_at))}</td>
              <td>{run.actual_grams == null
                ? "—"
                : `${run.actual_grams} g`}
                {run.actual_grams_basis && run.actual_grams_basis !== "manual"
                  ? " ~" : ""}</td>
              <td>{rollup.total ? `${rollup.good}/${rollup.total}` : "—"}</td>
            </tr>
          );
        })}
      </tbody>
    </table>
  );
}
```

> The `~` suffix on an estimated grams figure is deliberate and matches the
> queue's `TotalsBar`, which marks its own projected finish time the same way.
> An estimate must never render identically to a measurement.

- [ ] **Step 2: Create `PieceGrid.jsx`**

```jsx
import { useState } from "react";
import Button from "../ui/Button.jsx";

const STATUSES = [
  ["good", "Good"],
  ["rework", "Rework"],
  ["scrap", "Scrap"],
  ["pending_inspection", "Pending"],
];

// Piece verdicts for one run. The bulk row is the primary control: a plate of
// eight has to be confirmable in one action, or the verdicts stop being
// entered at all and piece-level traceability becomes fiction.
export default function PieceGrid({ pieces, busy, onBulk, onSetPiece }) {
  const [inspector, setInspector] = useState("");

  if (!pieces.length) {
    return <p className="muted">
      No pieces yet — they are created when the run ends.</p>;
  }

  return (
    <div className="stack">
      <div className="row">
        <input value={inspector} placeholder="Inspected by"
               onChange={(e) => setInspector(e.target.value)} />
        {STATUSES.slice(0, 3).map(([value, label]) => (
          <Button key={value} disabled={busy}
                  onClick={() => onBulk(value, inspector)}>
            All {label.toLowerCase()}
          </Button>
        ))}
      </div>
      <table className="table">
        <thead>
          <tr><th>#</th><th>Status</th><th>Badges</th><th>Inspected by</th></tr>
        </thead>
        <tbody>
          {pieces.map((piece) => (
            <tr key={piece.id}>
              <td>{piece.index_in_run}</td>
              <td>
                <select value={piece.status} disabled={busy}
                        onChange={(e) => onSetPiece(
                          piece.id, e.target.value, inspector)}>
                  {STATUSES.map(([value, label]) => (
                    <option key={value} value={value}>{label}</option>
                  ))}
                </select>
              </td>
              <td>{(piece.badges ?? []).map((b) => b.label).join(", ") || "—"}</td>
              <td>{piece.inspected_by ?? "—"}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}
```

- [ ] **Step 3: Create `RunDetail.jsx`**

```jsx
import Card from "../ui/Card.jsx";
import PieceGrid from "./PieceGrid.jsx";
import { formatDuration, elapsedSeconds, pieceRollup, runOutcome }
  from "./runFormat.js";

const END_STATES = [
  "FINISH", "FAILED", "STOPPED_BY_MONITOR", "STOPPED_BY_OPERATOR",
  "START_UNCONFIRMED", "UNKNOWN",
];

export default function RunDetail({ detail, busy, onBulk, onSetPiece,
                                    onCorrectEndState }) {
  if (!detail) return <p className="muted">Select a run.</p>;
  const { run, events, pieces, badges } = detail;
  const outcome = runOutcome(run);
  const rollup = pieceRollup(pieces);

  return (
    <div className="stack">
      <Card title={run.subtask_name ?? run.sd_path ?? "Run"}>
        <dl className="kv">
          <dt>Printer</dt><dd>{run.printer_name || run.printer_serial}</dd>
          <dt>Source</dt><dd>{run.source}</dd>
          <dt>Outcome</dt>
          <dd>
            <span className={`pill pill-${outcome.tone}`}>{outcome.label}</span>
            {run.end_state && (
              <select value={run.end_state} disabled={busy}
                      onChange={(e) => onCorrectEndState(e.target.value)}>
                {END_STATES.map((s) => <option key={s} value={s}>{s}</option>)}
              </select>
            )}
          </dd>
          <dt>Layers</dt>
          <dd>{run.last_layer ?? "—"} / {run.total_layers ?? "—"}</dd>
          <dt>Elapsed</dt>
          <dd>{formatDuration(elapsedSeconds(run.started_at, run.ended_at))}</dd>
          <dt>Filament</dt>
          <dd>
            {run.actual_grams == null ? "—" : `${run.actual_grams} g`}
            {run.actual_grams_basis
              ? ` (${run.actual_grams_basis})`
              : ""}
          </dd>
        </dl>
        {badges.length > 0 && (
          <p>{badges.map((b) => (
            <span key={b.badge_id} className="pill pill-warn">{b.label}</span>
          ))}</p>
        )}
      </Card>

      <Card title={`Pieces — ${rollup.good}/${rollup.total} good`
                   + (rollup.pending ? `, ${rollup.pending} unconfirmed` : "")}>
        <PieceGrid pieces={pieces} busy={busy} onBulk={onBulk}
                   onSetPiece={onSetPiece} />
      </Card>

      <Card title="Timeline">
        <ul className="timeline">
          {events.map((e) => (
            <li key={e.id}>
              <code>{(e.ts ?? "").replace("T", " ").slice(0, 19)}</code>{" "}
              <strong>{e.kind}</strong>{" "}
              {e.payload ? <span className="muted">
                {JSON.stringify(e.payload)}</span> : null}
            </li>
          ))}
        </ul>
      </Card>
    </div>
  );
}
```

> `run.actual_grams_basis` is rendered verbatim rather than prettified. The
> point of that column is that nobody can mistake `proportional` for a
> measurement, and softening the wording defeats it.

- [ ] **Step 4: Create `History.jsx`**

```jsx
import { useCallback, useEffect, useRef, useState } from "react";
import { bulkPieces, fetchRun, fetchRuns, patchPiece, patchRun }
  from "../api/printer.js";
import RunDetail from "../components/history/RunDetail.jsx";
import RunTable from "../components/history/RunTable.jsx";
import Card from "../components/ui/Card.jsx";
import PageFrame from "../components/ui/PageFrame.jsx";

const POLL_MS = 5000;

// One printer's history. Mounted with key={serial} by History below, the same
// remount-instead-of-Effect-reset pattern SdFiles and Queue use.
function HistoryPanel({ printer }) {
  const [runs, setRuns] = useState([]);
  const [selectedId, setSelectedId] = useState(null);
  const [detail, setDetail] = useState(null);
  const [err, setErr] = useState(null);
  const [busy, setBusy] = useState(false);

  // Only the most recently issued request may write state — an out-of-order
  // response must never clobber a newer one (same guard as SdBrowser).
  const requestId = useRef(0);

  const loadRuns = useCallback(async () => {
    const id = (requestId.current += 1);
    try {
      const data = await fetchRuns(printer.serial);
      if (id === requestId.current) {
        setRuns(data.runs);
        setErr(null);
      }
    } catch (e) {
      if (id === requestId.current) setErr(e.message);
    }
  }, [printer.serial]);

  const loadDetail = useCallback(async (runId) => {
    if (!runId) return setDetail(null);
    try {
      setDetail(await fetchRun(runId));
    } catch (e) {
      setErr(e.message);
    }
  }, []);

  useEffect(() => {
    loadRuns();
    const t = setInterval(loadRuns, POLL_MS);
    return () => clearInterval(t);
  }, [loadRuns]);

  useEffect(() => { loadDetail(selectedId); }, [selectedId, loadDetail]);

  const act = useCallback(async (fn) => {
    setBusy(true);
    try {
      const next = await fn();
      if (next?.run) setDetail(next);
      else await loadDetail(selectedId);
      await loadRuns();
      setErr(null);
    } catch (e) {
      setErr(e.message);
    } finally {
      setBusy(false);
    }
  }, [loadDetail, loadRuns, selectedId]);

  return (
    <div className="stack">
      {err && <p className="error">{err}</p>}
      <Card title={`Runs — ${printer.name || printer.serial}`}>
        <RunTable runs={runs} selectedId={selectedId}
                  onSelect={setSelectedId} />
      </Card>
      <RunDetail
        detail={detail}
        busy={busy}
        onBulk={(status, inspector) => act(() => bulkPieces(
          selectedId, { status, inspected_by: inspector || null }))}
        onSetPiece={(pieceId, status, inspector) => act(() => patchPiece(
          pieceId, { status, inspected_by: inspector || null }))}
        onCorrectEndState={(endState) => act(() => patchRun(
          selectedId, { end_state: endState }))}
      />
    </div>
  );
}

export default function History({ printers, selected }) {
  const printer = printers.find((p) => p.serial === selected);
  return (
    <PageFrame title="History">
      {printer
        ? <HistoryPanel key={printer.serial} printer={printer} />
        : <p className="muted">Select a printer.</p>}
    </PageFrame>
  );
}
```

- [ ] **Step 5: Register the page**

In `frontend/src/app/pageRegistry.jsx`, add the import and the entry:

```jsx
import History from "../pages/History.jsx";
```

```jsx
  queue: { title: "Queue", group: "Monitor", component: Queue },
  // After queue: history is what the queue turns into once a job has run.
  history: { title: "History", group: "Monitor", component: History },
```

- [ ] **Step 6: Build and look at it**

Run: `cd frontend && npm run build`
Expected: build succeeds with no errors.

Then, in two terminals:

```bash
python -m server --mock
cd frontend && npm run dev
```

Open <http://127.0.0.1:5173>, pick the `mock-bench` printer, and open History.
Expected: within a minute a run appears (the mock printer cycles
`RUNNING → FINISH → IDLE`), clicking it shows the timeline, and "All good"
sets every piece.

- [ ] **Step 7: Commit**

```bash
git add frontend/src
git commit -m "feat(frontend): History page with run timeline and piece verdicts"
```

---

## Task 17: Documentation

**Files:**
- Modify: `master.md`, `docs/superpowers/README.md`
- Modify: this plan's status banner

- [ ] **Step 1: Add master.md §13**

Append a new section to `master.md`, after §12 and before nothing else — it
must be the last numbered section so §12's subsections stay contiguous:

```markdown
---

## 13. The traceability ledger

`ledger.db` (SQLite, beside `printers.json`) is the durable record of every
print this server has observed. Before it existed nothing recorded that a
print had happened: the queue drops a job on confirmed start, slice jobs are
never persisted (§6.5), and the start route stops watching once verified
(§5.4).

| Module | Owns |
|---|---|
| `server/ledger.py` | The database only — schema, forward-only migrations, row helpers. No network, no registry, the same purity `PrintQueue` has |
| `server/runlog.py` | `RunRecorder`: a 1 s daemon thread that turns `registry.summaries()` diffs into run and event rows |

**`ledger=None` means inert**, the same convention as `queue`/`detection`/
`slicer`: every route 404s and no thread starts.

**The start route opens the run row before publishing.** It is the only place
that knows the queue job, so if `RunRecorder`'s tick got there first the
attributed row and an unattributed one would both exist. That ordering also
means a start the printer never confirms is recorded as
`end_state = START_UNCONFIRMED` instead of being forgotten — §5.4 leaves the
job queued but used to keep no record at all.

**Layer progress updates a column; it does not append events.** A 1,200-layer
print would otherwise write 1,200 event rows containing no information.

**`actual_grams` is always an estimate and the row says which kind.** The
printer does not report filament consumed, so `actual_grams_basis` is
`planned` on FINISH, `proportional` (by layer fraction) on a failure, or
`manual` when an operator overrides it. Layers are not equal mass, so the
proportional figure is wrong in detail — recording the basis is what stops it
being quoted later as a measurement.

**Badges attach at two levels, and the levels are not interchangeable.**
Automatic badges (`spaghetti`, `stringing`, `hms_error`, `autostop`) attach to
the **run**, because a detection is `{cls, conf, box}` in frame pixels (§4.1)
with no association to a model on the plate. Human verdicts attach to the
**piece**. `badges.auto` enforces it: `applied_by="detector"` is refused for
any badge not marked auto.

**A corrupt database is quarantined, not deleted and not fatal.** On open,
`PRAGMA integrity_check`; on failure the file is renamed
`ledger.db.corrupt-<stamp>` and a fresh one is created — §11's boot invariant,
plus the observation that the corrupt file is the only evidence of what went
wrong.

**Known gaps.** A print that runs while the server is down is unrecorded and
unrecoverable — MQTT has no history to replay. And an operator stopping a
print at the printer's own screen is indistinguishable from a genuine failure
(§3.1), so `end_state` defaults to the honest `FAILED` and is correctable from
the History page.

Design: `docs/superpowers/specs/2026-07-24-erp-traceability-design.md`. Phases
2–5 (parts catalogue, filament spools, Supabase sync, arm ingest) are designed
there and **not implemented**.
```

- [ ] **Step 2: Add the index rows**

In `docs/superpowers/README.md`, add to the "What's here" table:

```markdown
| 📐 design only | `specs/2026-07-24-erp-traceability-design.md` | ERP traceability: local ledger, pieces, inventory, Supabase sync. Phases 2–5 are **not built** |
| ✅ shipped | `plans/2026-07-24-erp-traceability-phase1-ledger.md` | Phase 1 only: the ledger and run recording |
```

- [ ] **Step 3: Update this plan's status banner**

Change the banner at the top of
`docs/superpowers/plans/2026-07-24-erp-traceability-phase1-ledger.md` from
`STATUS: NOT STARTED` to:

```markdown
> **STATUS: SHIPPED (2026-07-24).** Phase 1 landed as written below.
> **Not verified on hardware** — see the gate in the spec's section 8.4: no
> real print has been recorded end to end yet.
```

- [ ] **Step 4: Run the doc tests**

Run: `python -m pytest server/tests/test_docs.py -q`
Expected: PASS. If `test_master_section_references_exist` fails, a `§N` in the
new section points at a heading that does not exist — fix the reference, do
not renumber.

- [ ] **Step 5: Run everything**

Run: `python -m pytest -q && cd frontend && npm test && npm run build`
Expected: all green.

- [ ] **Step 6: Commit**

```bash
git add master.md docs/superpowers
git commit -m "docs: record the traceability ledger as master.md section 13"
```

---

## Task 18: The hardware gate

This is the only task that cannot be done at a desk, and per `master.md`
§1.1's discipline the feature stays **unverified** until it is.

- [ ] **Step 1: Start the real server**

```bash
python -m server
```

- [ ] **Step 2: Run one real print through the queue**

Queue a known `.gcode.3mf` for the A1 and start it from the Queue page. Let it
run to completion.

- [ ] **Step 3: Check the record**

Run: `curl http://127.0.0.1:8000/api/runs | python -m json.tool`

Confirm, and write the actual values into the plan's status banner:

- exactly **one** run row for that print — not two (an adoption failure would
  show as an attributed row plus an unattributed one)
- `source` is `queue`, and `queue_job_id` matches the job you started
- `end_state` is `FINISH`
- `last_layer` equals `total_layers`
- `actual_grams_basis` is `planned`, and `actual_grams` matches the 3MF's
  weight
- one piece row per planned copy, all `pending_inspection`

- [ ] **Step 4: Confirm the plate from the History page**

Use "All good" and verify the pieces update and persist across a server
restart.

- [ ] **Step 5: Update the status banner with the measured result**

Replace "Not verified on hardware" with the date and what was observed —
including anything that did **not** match, which per this repo's convention is
recorded rather than quietly fixed later.

- [ ] **Step 6: Commit**

```bash
git add docs/superpowers/plans/2026-07-24-erp-traceability-phase1-ledger.md
git commit -m "docs: record the phase 1 hardware gate result"
```

---

## Self-review notes

**Spec coverage.** Every Phase 1 item in spec §9 maps to a task: `ledger.py`
(Tasks 1–6), `runlog.py` (Tasks 7–10), start-route integration (Task 13), run
history routes (Task 11), piece verdicts including the bulk route (Task 12),
the seeded badge catalogue (Task 3), the History page (Tasks 15–16), and the
three test files throughout. Spec §8.2's corrupt-file recovery, WAL, and
migrations are Tasks 1–2; §8.4's hardware gate is Task 18.

**Deliberately deferred to later phases**, and no task here may implement
them: `parts`/`part_recipes`/`orders`/`order_lines`/`filament_spools`/
`filament_consumption`/`sync_state` tables, `LedgerSync`, the arm ingest
endpoint and its token, and the `sql/` Postgres DDL. The §8.1 schema-drift
guard is therefore also Phase 4 — there is no second dialect to compare
against until the DDL exists.

**Names used consistently across tasks:** `Ledger.open_run`, `find_open_run`,
`open_runs`, `update_run`, `close_run`, `get_run`, `list_runs`, `add_event`,
`events_for`, `create_pieces`, `pieces_for`, `set_piece`, `set_pieces_bulk`,
`badges`, `add_run_badge`, `remove_run_badge`, `run_badges`,
`add_piece_badge`, `remove_piece_badge`, `piece_badges`, `badge_id_for`; and
`RunRecorder.tick`, `reconcile_startup`, `start`, `stop`.
