# ERP Traceability Phase 3 — Filament Spools + Consumption Implementation Plan

> **STATUS: IN PROGRESS (started 2026-07-25).** Backend (Tasks 1–5) is being
> implemented against the established Phase 1/2 ledger patterns; the Inventory
> UI (Task 6) is a review checkpoint. Implements **Phase 3** of
> `docs/superpowers/specs/2026-07-24-erp-traceability-design.md` (§4.5, §4.6,
> §9). **`master.md` is authoritative wherever this file disagrees.**

> **For agentic workers:** REQUIRED SUB-SKILL: superpowers:subagent-driven-development
> or superpowers:executing-plans. Steps use checkbox (`- [ ]`) syntax.
> Phase 1 (ledger + recorder) and Phase 2 (parts catalogue, schema v2) are done.

**Goal:** Track physical filament spools, decrement them as prints consume
filament, and give a true per-run/per-piece filament cost plus a reorder
signal — without ever storing a `remaining_grams` counter that can drift.

**Architecture:** Two new tables (`filament_spools`, `filament_consumption`)
at **schema version 3** of `ledger.db`. `remaining_grams` is *derived*
(`initial_grams` minus the sum of that spool's consumption), computed on read.
A printer has at most one **loaded** spool; the start route stamps that spool
onto the run, and when the run ends the recorder writes one
`filament_consumption` row using the run's already-computed `actual_grams` +
`actual_grams_basis` (§4.6). Spool identity is operator-set — the printer's
RFID reading only prefills it (`master.md` §6.3).

**Tech stack:** Python 3.11 stdlib `sqlite3`, FastAPI, pytest, React 19 + Vite,
vitest — same as Phases 1–2.

**Not in this phase:** Supabase sync (Phase 4), arm ingest (Phase 5), outbound
purchase orders (design §11 — out of scope entirely). The low-stock *view*
exists; raising a PO does not.

---

## Background the engineer needs

- Read `server/ledger.py` fully. Phase 3 adds to it exactly as Phase 2 did: a
  new `MIGRATIONS[3]`, `SCHEMA_VERSION = 3`, new `*_WRITABLE` allowlists routed
  through `_checked`, new typed helpers, writes through `execute`/`transaction`.
  **Never edit `MIGRATIONS[1]` or `MIGRATIONS[2]`.**
- `remaining_grams` is **derived, never a column** (spec §4.6). Compute it as
  `initial_grams - COALESCE(SUM(consumption.grams), 0)` in SQL on read. A
  stored counter and the rows it counts drift apart after one failed
  transaction; not having the counter is the fix.
- The run row already carries a `spool_id` column (reserved in Phase 1's
  `print_runs` DDL and in `RUN_WRITABLE`) — Phase 3 finally sets it.
- The recorder's `_end` (in `server/runlog.py`) already computes
  `actual_grams`/`actual_grams_basis` on a run's terminal transition. Phase 3
  adds one call there: if the printer has a loaded spool and the run has an
  `actual_grams`, write a `filament_consumption` row.
- The start route (`server/main.py` `start_queue_job`) already opens the run
  before publishing and passes `bed_type`/`nozzle`. Phase 3 adds `spool_id`
  from the printer's loaded spool.

Conventions unchanged: plain ASCII in `.py`, docstrings explain why,
`ledger=None` means inert, every route calls `_require_ledger`, values bound as
`?` params, column names allowlisted.

---

## File structure

**Modify:**

| Path | Change |
|---|---|
| `server/ledger.py` | `MIGRATIONS[3]`, `SCHEMA_VERSION=3`, `SPOOL_WRITABLE`, spool + consumption helpers, `remaining_grams`/`low_stock` |
| `server/runlog.py` | `_end` writes a `filament_consumption` row against the run's spool |
| `server/main.py` | spool routes + `POST /api/printers/{serial}/spool` (set loaded) + `GET /api/spools/low`; start route passes `spool_id` |
| `server/tests/test_ledger.py` | migration v3 + spool/consumption/remaining/low-stock tests |
| `server/tests/test_runlog.py` | consumption recorded on run end |
| `server/tests/test_ledger_api.py` | spool routes + loaded-spool + start-route spool_id |
| `frontend/src/api/printer.js` | spool fetch wrappers |
| `frontend/src/app/pageRegistry.jsx` | register the Inventory page |
| `master.md` | note spools under §13/§14 (or a new §15) |

**Create:**

| Path | Responsibility |
|---|---|
| `frontend/src/pages/Inventory.jsx` | the spools page |
| `frontend/src/components/inventory/SpoolList.jsx` | spool table with remaining grams + low-stock highlight |
| `frontend/src/components/inventory/SpoolForm.jsx` | add/edit a spool |
| `frontend/src/components/inventory/LoadedSpool.jsx` | "which spool is loaded on this printer" control |

---

## Task 1: Schema v3 — filament_spools + filament_consumption

**Files:** Modify `server/ledger.py`; Test `server/tests/test_ledger.py`

- [ ] **Step 1 — failing tests.** Append to `test_ledger.py`:

```python
def test_schema_upgrades_v2_to_v3(tmp_path):
    """A v2 ledger (Phase 2) must gain the spool tables on reopen and keep
    its rows."""
    import sqlite3 as _sq
    path = tmp_path / "ledger.db"
    con = _sq.connect(str(path)); con.execute("PRAGMA journal_mode=WAL")
    con.execute("CREATE TABLE schema_version (id INTEGER PRIMARY KEY "
                "CHECK (id = 1), version INTEGER NOT NULL)")
    con.execute("INSERT INTO schema_version (id, version) VALUES (1, 2)")
    con.execute("CREATE TABLE parts (id TEXT PRIMARY KEY, part_number TEXT)")
    con.execute("INSERT INTO parts (id, part_number) VALUES ('p1','BRK')")
    con.commit(); con.close()

    led = Ledger(path)
    try:
        assert led.query("SELECT version FROM schema_version")[0]["version"] == SCHEMA_VERSION
        names = {r["name"] for r in led.query(
            "SELECT name FROM sqlite_master WHERE type='table'")}
        assert {"filament_spools", "filament_consumption"} <= names
        assert led.query("SELECT id FROM parts")[0]["id"] == "p1"
    finally:
        led.close()


def test_fresh_database_is_at_v3(led):
    assert led.query("SELECT version FROM schema_version")[0]["version"] == 3
```

- [ ] **Step 2 — run, watch fail** (`SCHEMA_VERSION` is 2; tables missing).
  `python -m pytest server/tests/test_ledger.py -k "v3 or v2_to_v3" -q`

- [ ] **Step 3 — implement.** Set `SCHEMA_VERSION = 3`; add
  `"filament_spools", "filament_consumption"` to `TABLES`. Append
  `MIGRATIONS[3]` (do NOT edit `[1]`/`[2]`):

```python
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
```

- [ ] **Step 4 — run** the two tests, then full `test_ledger.py`, then
  `python -m pytest -q`.

- [ ] **Step 5 — commit** `feat(ledger): schema v3 -- filament spools + consumption`.

---

## Task 2: Spool helpers (CRUD + remaining_grams + loaded spool)

**Files:** Modify `server/ledger.py`; Test `server/tests/test_ledger.py`

Add `SPOOL_WRITABLE = frozenset({"material","colour","brand",
"filament_profile","initial_grams","purchase_cost","currency","supplier",
"purchased_at","status","printer_serial","ams_slot","spool_code"})` and:

- `create_spool(*, spool_code, material="", **fields) -> id`
- `get_spool(spool_id) -> dict|None`
- `list_spools(*, include_archived=False) -> list[dict]` — each row carries a
  computed `remaining_grams` (see below)
- `update_spool(spool_id, **fields) -> None`
- `archive_spool(spool_id) -> None`
- `remaining_grams(spool_id) -> float|None` — `initial_grams` minus the sum of
  that spool's consumption; `None` if `initial_grams` is null
- `set_loaded_spool(printer_serial, spool_id) -> None` — in ONE `transaction()`:
  clear `printer_serial`/status on any spool currently `in_use` on this printer
  (set them back to `sealed`? no — to `in_use=False`), then mark this spool
  `status='in_use'`, `printer_serial=serial`. "At most one loaded spool per
  printer" must hold at every observable moment (same discipline as
  `set_default_recipe`).
- `loaded_spool(printer_serial) -> dict|None` — the `in_use` spool on that
  printer, or None

> `remaining_grams` in `list_spools`: use one grouped query, not one per row —
> `SELECT s.*, s.initial_grams - COALESCE(SUM(c.grams),0) AS remaining_grams
> FROM filament_spools s LEFT JOIN filament_consumption c ON c.spool_id = s.id
> WHERE s.archived = 0 GROUP BY s.id`. When `initial_grams` is NULL the
> subtraction is NULL — surface that as `remaining_grams = None`, not 0.

- [ ] **Step 1 — failing tests** covering: create + get round-trip; unique
  `spool_code` (duplicate raises `sqlite3.IntegrityError`); `list_spools`
  computes `remaining_grams` = initial minus consumption; `remaining_grams` is
  None when `initial_grams` is null; `set_loaded_spool` makes exactly one spool
  `in_use` per printer (loading a second clears the first); `loaded_spool`
  returns it; `update_spool` rejects an unknown column; archive hides.

```python
def test_spool_remaining_grams_is_initial_minus_consumption(led):
    sid = led.create_spool(spool_code="S-1", material="PLA", initial_grams=1000.0)
    led.add_consumption(sid, run_id=None, grams=120.0, basis="planned")
    led.add_consumption(sid, run_id=None, grams=80.0, basis="proportional")
    assert led.remaining_grams(sid) == 800.0
    row = [s for s in led.list_spools() if s["id"] == sid][0]
    assert row["remaining_grams"] == 800.0


def test_remaining_is_none_without_an_initial_weight(led):
    sid = led.create_spool(spool_code="S-2", material="PLA")
    assert led.remaining_grams(sid) is None
    row = [s for s in led.list_spools() if s["id"] == sid][0]
    assert row["remaining_grams"] is None


def test_exactly_one_loaded_spool_per_printer(led):
    a = led.create_spool(spool_code="A", material="PLA")
    b = led.create_spool(spool_code="B", material="PETG")
    led.set_loaded_spool("PRN1", a)
    led.set_loaded_spool("PRN1", b)          # must unload A
    assert led.loaded_spool("PRN1")["id"] == b
    loaded = [s for s in led.list_spools() if s["status"] == "in_use"
              and s["printer_serial"] == "PRN1"]
    assert [s["id"] for s in loaded] == [b]


def test_a_duplicate_spool_code_is_rejected(led):
    import sqlite3 as _sq
    led.create_spool(spool_code="DUP", material="PLA")
    with pytest.raises(_sq.IntegrityError):
        led.create_spool(spool_code="DUP", material="PETG")
```

- [ ] **Step 2/3/4/5** — watch fail, implement (mirror the Phase 2 part/recipe
  helpers; `set_loaded_spool` uses `transaction()` like `set_default_recipe`),
  run file + full suite, commit `feat(ledger): filament spool helpers with derived remaining grams`.

---

## Task 3: Consumption helpers + low-stock

**Files:** Modify `server/ledger.py`; Test `server/tests/test_ledger.py`

- `add_consumption(spool_id, *, run_id=None, grams, basis=None) -> str` —
  insert one row; `grams` required
- `consumption_for_run(run_id) -> list[dict]`
- `low_stock(threshold_grams) -> list[dict]` — spools (not archived) whose
  computed `remaining_grams` is not None and `< threshold_grams`, each with
  `remaining_grams`

- [ ] **Step 1 — failing tests:**

```python
def test_low_stock_lists_only_spools_below_the_threshold(led):
    low = led.create_spool(spool_code="LOW", material="PLA", initial_grams=100.0)
    led.add_consumption(low, run_id=None, grams=90.0, basis="planned")   # 10 left
    full = led.create_spool(spool_code="FULL", material="PLA", initial_grams=1000.0)
    unknown = led.create_spool(spool_code="UNK", material="PLA")         # no initial
    got = {s["spool_code"] for s in led.low_stock(50.0)}
    assert got == {"LOW"}                 # FULL is above, UNK has no known weight


def test_consumption_is_linked_to_a_run(led):
    sid = led.create_spool(spool_code="S", material="PLA", initial_grams=500.0)
    led.add_consumption(sid, run_id="r1", grams=42.0, basis="planned")
    rows = led.consumption_for_run("r1")
    assert len(rows) == 1 and rows[0]["grams"] == 42.0
```

- [ ] **Steps 2–5** — implement, test, commit
  `feat(ledger): filament consumption + low-stock query`.

---

## Task 4: Record consumption on run end + spool_id on the run

**Files:** Modify `server/runlog.py`, `server/main.py`; Test
`server/tests/test_runlog.py`, `server/tests/test_ledger_api.py`

The wiring that makes spools decrement automatically.

- [ ] **Step 1 — failing test (runlog).** Append to `test_runlog.py` — a run
  with a `spool_id` and an `actual_grams` writes a consumption row on FINISH:

```python
def test_finish_records_filament_consumption_against_the_runs_spool(led):
    sid = led.create_spool(spool_code="S", material="PLA", initial_grams=1000.0)
    run = led.open_run(printer_serial="S1", source="queue",
                       spool_id=sid, planned_grams=20.0)
    run_ticks(led, [[summary(state="IDLE")],
                    [summary(state="RUNNING", layer=100, total=100)],
                    [summary(state="FINISH")]])
    cons = led.consumption_for_run(run)
    assert len(cons) == 1
    assert cons[0]["grams"] == 20.0            # the run's actual_grams
    assert cons[0]["basis"] == "planned"
    assert cons[0]["spool_id"] == sid
    assert led.remaining_grams(sid) == 980.0


def test_a_run_without_a_spool_records_no_consumption(led):
    run = led.open_run(printer_serial="S1", source="queue", planned_grams=20.0)
    run_ticks(led, [[summary(state="IDLE")],
                    [summary(state="RUNNING")],
                    [summary(state="FINISH")]])
    assert led.consumption_for_run(run) == []
```

- [ ] **Step 2 — run, watch fail.**

- [ ] **Step 3 — implement in `runlog._end`.** After `create_pieces`, add:

```python
        # Decrement the spool this run drew from, if one was loaded when it
        # started (the start route stamps run.spool_id). Uses the run's own
        # actual_grams estimate + basis -- the printer never reports consumed
        # filament, so this inherits that estimate's honesty (basis says which
        # kind). No spool, or no gram estimate -> no consumption row.
        spool_id = run.get("spool_id")
        if spool_id and grams:
            try:
                self.ledger.add_consumption(spool_id, run_id=run["id"],
                                            grams=grams, basis=basis)
            except Exception as e:  # noqa: BLE001
                log.error("could not record consumption for run %s: %s",
                          run["id"], e)
```

(`grams`/`basis` are the locals `_end` already computed via `self._grams`.)

- [ ] **Step 4 — failing test (start route stamps spool_id).** Append to
  `test_ledger_api.py` — reuse the `StartableService`/`FakeQueue` harness, but
  give the ledger a loaded spool for the serial and assert the run row gets it:

```python
def test_start_stamps_the_loaded_spool_onto_the_run(tmp_path, led):
    sid = led.create_spool(spool_code="LOADED", material="PLA")
    led.set_loaded_spool("S1", sid)
    client, _ = _start_client(tmp_path, led)     # from Task 13 of Phase 1
    client.post("/api/printers/S1/queue/j1/start")
    run = led.list_runs()[0]
    assert run["spool_id"] == sid
```

- [ ] **Step 5 — implement in `start_queue_job`.** In the `open_run(...)` call,
  add a line reading the loaded spool (guarded — the ledger may not have the
  method on a fake, and there may be no loaded spool):

```python
                    spool_id=_maybe_spool(ledger, serial),
```

and a module-level helper next to `_maybe`:

```python
def _maybe_spool(ledger, serial: str):
    """The loaded spool's id for a printer, or None -- never let a spool
    lookup fail a start (a ledger problem must not cost a print)."""
    if ledger is None:
        return None
    try:
        loaded = ledger.loaded_spool(serial)
        return loaded["id"] if loaded else None
    except Exception:  # noqa: BLE001
        return None
```

- [ ] **Step 6 — run** both test files, then `python -m pytest -q`.
- [ ] **Step 7 — commit** `feat(runlog): decrement the loaded spool when a run ends`.

---

## Task 5: Spool routes

**Files:** Modify `server/main.py`; Test `server/tests/test_ledger_api.py`

Routes, all behind `_require_ledger`:

| Route | Behaviour |
|---|---|
| `GET /api/spools` | `{spools:[...]}` with `remaining_grams`, archived hidden |
| `POST /api/spools` | create; 409 on duplicate `spool_code` |
| `GET /api/spools/{id}` | one spool + its consumption |
| `PUT /api/spools/{id}` | edit |
| `DELETE /api/spools/{id}` | archive (soft); 204 |
| `GET /api/spools/low?threshold=` | low-stock list (default threshold e.g. 100 g) |
| `GET /api/printers/{serial}/spool` | the loaded spool for a printer (or null) |
| `POST /api/printers/{serial}/spool` | `{spool_id}` → set loaded; `{spool_id: null}` → unload |

> Route ordering: register `/api/spools/low` BEFORE `/api/spools/{id}` so
> "low" is not captured as an `{id}`. (FastAPI matches in declaration order for
> same-prefix paths.)

- [ ] TDD each (mirror the Phase 2 parts-route tests + `FakeRegistry`). Assert:
  404 without a ledger; duplicate spool_code → 409; setting a loaded spool then
  `GET .../spool` returns it; low-stock filters correctly. Commit
  `feat(api): filament spool routes + loaded-spool control`.

---

## Task 6: Inventory frontend (review checkpoint)

**Files:** create the inventory components + page; modify `printer.js`,
`pageRegistry.jsx`.

Follow the Parts page shape (fleet-wide list + a per-printer "loaded spool"
control that uses the `printers`/`selected` props). Register as
`inventory: { title: "Inventory", group: "Monitor", component: Inventory }`.

- API wrappers: `fetchSpools`, `fetchSpool`, `createSpool`, `updateSpool`,
  `archiveSpool`, `fetchLowStock`, `fetchLoadedSpool(serial)`,
  `setLoadedSpool(serial, spoolId)`.
- `SpoolList` shows each spool's material/colour/brand, remaining grams (with a
  "~" when consumption basis was an estimate — mirror the History page), and a
  low-stock highlight.
- `LoadedSpool` lets the operator pick which spool is on the selected printer
  (prefilled from `detect_loaded_filament` where available).

- [ ] Build (`npm run build`) + a gstack visual check under `--mock`: add a
  spool, load it on a printer, run a mock print to completion, confirm the
  spool's remaining grams dropped. Commit
  `feat(frontend): Inventory page -- spools, remaining grams, loaded spool`.

**← REVIEW CHECKPOINT.** Backend (Tasks 1–5) is self-contained and fully
tested. Task 6 (UI) is where a human should look.

---

## Task 7: Docs + verification

- [ ] Add a short section to `master.md` (a new §15, "Filament inventory"):
  spools are per-physical-spool with a `spool_code`; `remaining_grams` is
  derived (never a column, spec §4.6); a printer has one loaded spool; the
  start route stamps `run.spool_id` and the recorder writes one
  `filament_consumption` row on the run's terminal transition using the run's
  `actual_grams` + basis; a low-stock view but no purchase orders (out of
  scope). Run `test_docs.py`.
- [ ] Update this plan's status banner to SHIPPED with the test counts.
- [ ] Full verification:
  `python -m pytest -q && cd frontend && npm test && npm run build`.
- [ ] Commit `docs: filament inventory (master.md section 15)`.

---

## Self-review notes

- **Spec coverage (§4.5, §4.6, §9):** spools table + helpers (Tasks 1–2),
  consumption table + helpers (Tasks 1, 3), derived `remaining_grams` never
  stored (Task 2), consumption recorded on run end from `actual_grams`+basis
  (Task 4), loaded-spool control (Tasks 2, 5), low-stock view (Tasks 3, 5),
  Inventory UI (Task 6). The `filament_profile` column ties a spool to
  `available_filaments` (spec §4.5) — stored but not yet auto-matched to a
  slice; that link is a Phase-4 nicety, not built here.
- **Deferred, correctly:** Supabase sync (Phase 4), arm ingest (Phase 5),
  purchase orders (design §11). No task here touches them.
- **Migration safety:** Task 1 proves v2→v3 preserves data and never edits
  earlier migrations; the per-version transaction (Phase 1) makes it atomic.
- **Consistent names:** `create_spool`, `get_spool`, `list_spools`,
  `update_spool`, `archive_spool`, `remaining_grams`, `set_loaded_spool`,
  `loaded_spool`, `add_consumption`, `consumption_for_run`, `low_stock`;
  API `setLoadedSpool`/`fetchLoadedSpool`; run column `spool_id`.
- **The invariant that matters:** never a stored `remaining_grams`. Every read
  computes it from `initial_grams - SUM(consumption)`, so a failed transaction
  can't leave a counter lying.
