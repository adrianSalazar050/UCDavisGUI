# ERP Traceability Phase 2 — Parts Catalogue + Recipes Implementation Plan

> **STATUS: SHIPPED (2026-07-24), Tasks 1–8.** All of Phase 2 landed: schema
> v2, part/recipe helpers, `PartStore`, the routes, the Parts UI, and the
> slice-from-part path. A backend code review (probe-verified) found two
> catalogue invariants enforced only by route convention (a recipe could get
> two defaults; a wrong-part URL could corrupt a foreign default) — both now
> enforced structurally at the data layer (commit `1bbc091`). Model
> upload/download round trip verified live; 681 backend + 26 frontend tests
> green. **Not yet verified on hardware:** a real slice-from-part print
> producing a part-attributed run. Implements **Phase 2** of
> `docs/superpowers/specs/2026-07-24-erp-traceability-design.md` (§4.1, §9,
> §10). **`master.md` is authoritative wherever this file disagrees.**

> **For agentic workers:** Phase 1's ledger + recorder are done and on
> `main`/`dashboard`. This phase adds durable **parts** (a stored geometry +
> revision) and **recipes** (a stored slicing setup), so an order/run can
> point at a reproducible thing, and so slicing can be driven from a stored
> part instead of an ad-hoc upload.

**Goal:** Give the farm a catalogue: a part is `part_number`+`revision` with a
stored model file; a recipe is a named slicing setup for that part. Later
phases attribute runs and order lines to parts; this phase makes parts exist.

**Architecture:** Two new tables (`parts`, `part_recipes`) added by a
forward-only migration to **schema version 2** of the existing `ledger.db`.
Model bytes live on local disk under `<data-dir>/parts/<part_id>/`, with the
sha256 recorded in the row (metadata in SQLite, bytes on disk — same split the
design §4.1 specifies). New `/api/parts` routes mirror the Phase 1 route
conventions. A Parts page and a "slice this part with this recipe" path close
the loop.

**Tech stack:** Same as Phase 1 — Python 3.11 stdlib `sqlite3`, FastAPI,
pytest, React 19 + Vite, vitest.

**Ownership decision (spec §12 Q2):** `parts` and `part_recipes` are
**locally owned** — the model file and the slicer both live on the LAN, and a
part is normally defined by whoever has the STL. Phase 4 pushes them to
Supabase. If parts turn out to be defined office-side in practice, this flips
to a pulled table; the row shapes are chosen to allow that (uuid PK,
created_at/updated_at/synced_at, archived).

**Not in this phase:** filament spools (Phase 3), Supabase sync (Phase 4),
arm ingest (Phase 5), and attributing a *run* to a part automatically (the
run already has a nullable `part_id`; wiring it from a recipe-driven slice is
Task 8, but auto-attribution of screen-started prints is out of scope).

---

## Background the implementer needs

- Read `server/ledger.py` end to end. Phase 2 adds to it exactly as Phase 1
  did: a new migration entry, new writable-column allowlists routed through
  `_checked`, new typed helpers, all writes through `execute`/`transaction`.
- Migrations are forward-only. `SCHEMA_VERSION` bumps to `2`; add a
  `MIGRATIONS[2]` list. `_migrate` already applies any version `> current` in
  order and stamps `schema_version` via `INSERT OR REPLACE`. **Never edit
  `MIGRATIONS[1]`.** A test in Task 1 proves a v1 database upgrades to v2 on
  reopen without data loss.
- Model files must live under the same data dir as `ledger.db` so the desktop
  build's `BAMBU_DATA_DIR` (`master.md` §8) covers them for free. `ledger.db`
  is at `runs_dir.parent` (real) / `runs_dir` (`--mock`). Parts files go in
  `<ledger.db's dir>/parts/<part_id>/<filename>`.
- The slice flow (Task 8) is `SliceCoordinator.submit(serial, filename, data,
  tier_id, material, supports) -> job_id` in `server/slicejobs.py`, reached by
  `POST /api/printers/{serial}/slice`. A recipe carries exactly the inputs
  `submit` needs, so Task 8 is "read them off a stored part+recipe instead of
  the request."

Conventions unchanged from Phase 1: plain ASCII in `.py`, docstrings explain
why, `ledger=None` means inert, every route calls `_require_ledger`, values
bound as `?` params, column names allowlisted.

---

## File structure

**Modify:**

| Path | Change |
|---|---|
| `server/ledger.py` | `MIGRATIONS[2]`, `SCHEMA_VERSION=2`, `PART_WRITABLE`/`RECIPE_WRITABLE`, and the parts/recipes helpers |
| `server/main.py` | `/api/parts` routes + recipe routes + model up/download; Task 8 slice-from-recipe route |
| `server/__main__.py` | Pass the parts-files dir to `create_app` (or resolve it there) |
| `server/slicejobs.py` | Task 8: a `submit_from_recipe` path that records `part_id`/`recipe_id` on the queue job |
| `server/tests/test_ledger.py` | Migration-v2 + parts/recipes helper tests |
| `server/tests/test_ledger_api.py` | Parts/recipes route tests |
| `frontend/src/api/printer.js` | Parts/recipes fetch wrappers |
| `frontend/src/app/pageRegistry.jsx` | Register the Parts page |
| `master.md` | Extend §13 (or add §14) for the catalogue |

**Create:**

| Path | Responsibility |
|---|---|
| `server/partstore.py` | Model-file storage on disk: write bytes + sha256, read back, delete on archive. Pure of the DB; the ledger holds metadata, this holds bytes. Mirrors the ledger/queue purity split. |
| `server/tests/test_partstore.py` | Storage round-trip, sha256, path safety (no traversal via filename) |
| `frontend/src/pages/Parts.jsx` | The catalogue page |
| `frontend/src/components/parts/PartList.jsx` | Parts table + revision grouping |
| `frontend/src/components/parts/PartForm.jsx` | Add/edit part + model upload |
| `frontend/src/components/parts/RecipeEditor.jsx` | Recipes for a part, default toggle |

---

## Task 1: Migration to schema version 2 — parts + part_recipes tables

**Files:** Modify `server/ledger.py`; Test `server/tests/test_ledger.py`

- [ ] **Step 1 — failing test.** Append to `test_ledger.py`:

```python
def test_schema_upgrades_v1_to_v2_without_data_loss(tmp_path):
    """A ledger created at v1 (Phase 1) must gain the parts tables on reopen
    and keep its existing rows."""
    import sqlite3 as _sq
    path = tmp_path / "ledger.db"
    # Build a v1-shaped database by hand: schema_version=1 + a print_runs row.
    con = _sq.connect(str(path)); con.execute("PRAGMA journal_mode=WAL")
    con.execute("CREATE TABLE schema_version (id INTEGER PRIMARY KEY "
                "CHECK (id = 1), version INTEGER NOT NULL)")
    con.execute("INSERT INTO schema_version (id, version) VALUES (1, 1)")
    con.execute("CREATE TABLE print_runs (id TEXT PRIMARY KEY, "
                "printer_serial TEXT, end_state TEXT)")
    con.execute("INSERT INTO print_runs (id, printer_serial) VALUES "
                "('r1','S1')")
    con.commit(); con.close()

    led = Ledger(path)
    try:
        assert led.query("SELECT version FROM schema_version")[0][
            "version"] == SCHEMA_VERSION      # now 2
        names = {r["name"] for r in led.query(
            "SELECT name FROM sqlite_master WHERE type='table'")}
        assert {"parts", "part_recipes"} <= names
        # the pre-existing run survived
        assert led.query("SELECT id FROM print_runs")[0]["id"] == "r1"
    finally:
        led.close()


def test_fresh_database_is_at_v2(led):
    assert led.query("SELECT version FROM schema_version")[0][
        "version"] == 2
```

- [ ] **Step 2 — run, watch fail** (`SCHEMA_VERSION` is 1; `parts` missing).
  `python -m pytest server/tests/test_ledger.py -k "v2 or v1_to_v2" -q`

- [ ] **Step 3 — implement.** In `server/ledger.py`: change
  `SCHEMA_VERSION = 2` and add the `TABLES` entries `"parts", "part_recipes"`.
  Add `MIGRATIONS[2]` (do NOT touch `MIGRATIONS[1]`):

```python
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
```

`_migrate` already stamps the new version via `INSERT OR REPLACE`, and Phase
1 Task 1 wrapped each version's statements in one `transaction()` — so v2
lands atomically. No other change needed there.

- [ ] **Step 4 — run** the two tests; then the full `test_ledger.py` (the
  fresh-db badge/schema tests still pass because migration runs both v1 and v2
  on a new file). Then `python -m pytest -q`.

- [ ] **Step 5 — commit** `feat(ledger): schema v2 -- parts and part_recipes tables`.

---

## Task 2: Part helpers

**Files:** Modify `server/ledger.py`; Test `server/tests/test_ledger.py`

Helpers: `create_part(*, part_number, revision="A", name="", notes=None,
**fields) -> id` (fields allowlisted by `PART_WRITABLE` = the model_* columns);
`get_part(id)`; `find_part(part_number, revision)`; `list_parts(include_archived=False)`;
`update_part(id, **fields)`; `archive_part(id)` (sets `archived=1`).

- [ ] **Step 1 — failing tests** covering: create returns id and round-trips;
  `UNIQUE(part_number, revision)` — a duplicate raises `sqlite3.IntegrityError`
  (a *new revision* of the same number is allowed); `find_part` returns the
  right row or None; `list_parts` hides archived unless asked; `update_part`
  rejects an unknown column (ValueError via `_checked`); `archive_part` hides it.

```python
def test_create_and_find_part(led):
    pid = led.create_part(part_number="BRK-100", revision="A", name="Bracket")
    assert led.find_part("BRK-100", "A")["id"] == pid
    assert led.get_part(pid)["name"] == "Bracket"
    assert led.find_part("BRK-100", "B") is None


def test_a_new_revision_is_allowed_but_a_duplicate_is_not(led):
    led.create_part(part_number="BRK-100", revision="A")
    led.create_part(part_number="BRK-100", revision="B")   # fine
    import sqlite3 as _sq
    with pytest.raises(_sq.IntegrityError):
        led.create_part(part_number="BRK-100", revision="A")   # dup


def test_update_part_rejects_unknown_column(led):
    pid = led.create_part(part_number="X", revision="A")
    with pytest.raises(ValueError):
        led.update_part(pid, drop="oops")


def test_archive_hides_a_part(led):
    pid = led.create_part(part_number="X", revision="A")
    led.archive_part(pid)
    assert led.list_parts() == []
    assert len(led.list_parts(include_archived=True)) == 1
```

- [ ] **Step 2 — run, watch fail.**
- [ ] **Step 3 — implement**, using the Phase 1 patterns: `PART_WRITABLE =
  frozenset({"name","notes","model_filename","model_sha256","model_bytes",
  "part_number","revision"})`, `_checked(fields, PART_WRITABLE)`, all writes
  through `execute`.
- [ ] **Step 4 — run** file + full suite. **Step 5 — commit**
  `feat(ledger): part catalogue helpers`.

---

## Task 3: Recipe helpers

**Files:** Modify `server/ledger.py`; Test `server/tests/test_ledger.py`

Helpers: `add_recipe(part_id, *, name="", **fields) -> id` (`RECIPE_WRITABLE`
= tier/material/nozzle/bed_type/supports/copies_per_plate/expected_*/is_default);
`recipes_for(part_id, include_archived=False)`; `get_recipe(id)`;
`update_recipe(id, **fields)`; `set_default_recipe(part_id, recipe_id)`
(clears the flag on the part's other recipes and sets it on this one, in ONE
`transaction()`); `default_recipe(part_id)`; `archive_recipe(id)`.

- [ ] **Step 1 — failing tests**: add returns id; `recipes_for` lists a part's
  recipes; `set_default_recipe` makes exactly one default (setting a second
  clears the first — assert only one `is_default` across the part); a bad
  column raises ValueError; archive hides.

```python
def test_exactly_one_default_recipe_per_part(led):
    pid = led.create_part(part_number="X", revision="A")
    r1 = led.add_recipe(pid, name="Standard PLA")
    r2 = led.add_recipe(pid, name="Fine PLA")
    led.set_default_recipe(pid, r1)
    led.set_default_recipe(pid, r2)     # must clear r1
    defaults = [r for r in led.recipes_for(pid) if r["is_default"]]
    assert [d["id"] for d in defaults] == [r2]
    assert led.default_recipe(pid)["id"] == r2
```

- [ ] **Step 2/3/4/5** as before. Commit `feat(ledger): part recipes with a single default`.

---

## Task 4: Model-file storage (`server/partstore.py`)

**Files:** Create `server/partstore.py`, `server/tests/test_partstore.py`

A tiny module: bytes on disk, sha256 computed, path traversal impossible.
Pure of the ledger (the ledger stores the metadata row; this stores bytes),
the same responsibility split as `queue.py` vs the API layer.

```python
"""Model-file storage for parts: bytes on disk, sha256 for integrity.

Kept out of server/ledger.py deliberately -- the ledger holds the metadata
row (filename, sha256, size); the bytes live here on disk under the same data
dir as ledger.db so the desktop build's BAMBU_DATA_DIR covers both. Model
files can be large (an STL is easily tens of MB); putting them in SQLite would
bloat the file the recorder writes to on every tick.
"""
from __future__ import annotations
import hashlib, os, pathlib, shutil

MODEL_EXTS = (".stl", ".3mf", ".step", ".stp", ".obj")


class PartStore:
    def __init__(self, root: pathlib.Path):
        self.root = pathlib.Path(root)

    def _dir(self, part_id: str) -> pathlib.Path:
        # part_id is a server-generated uuid hex -- no separators, so it cannot
        # traverse. We still basename the FILENAME (client-supplied) below.
        return self.root / "parts" / part_id

    def save(self, part_id: str, filename: str, data: bytes) -> dict:
        """-> {filename, sha256, bytes}. Overwrites any prior model for this
        part (a revision is a new part_id, so this only overwrites a genuine
        replace of the same part's file)."""
        name = os.path.basename(filename)      # strip any directory component
        if not name or not name.lower().endswith(MODEL_EXTS):
            raise ValueError(f"not a model file: {filename!r}")
        d = self._dir(part_id)
        d.mkdir(parents=True, exist_ok=True)
        # clear old files so a re-upload with a different name doesn't leave
        # two models in the dir
        for old in d.iterdir():
            old.unlink()
        (d / name).write_bytes(data)
        return {"filename": name,
                "sha256": hashlib.sha256(data).hexdigest(),
                "bytes": len(data)}

    def open_bytes(self, part_id: str, filename: str) -> bytes:
        p = self._dir(part_id) / os.path.basename(filename)
        return p.read_bytes()

    def delete(self, part_id: str) -> None:
        shutil.rmtree(self._dir(part_id), ignore_errors=True)
```

- [ ] Tests: round-trip (`save` then `open_bytes` returns identical bytes and
  the sha256 matches `hashlib.sha256(data).hexdigest()`); a non-model
  extension raises; a filename containing `../` or a backslash lands as a
  basename inside the part dir, never outside `root` (assert the written path
  is under `root`); `delete` removes the dir; a re-`save` with a new filename
  leaves exactly one file. Commit `feat(partstore): on-disk model storage with sha256`.

---

## Task 5: Part routes

**Files:** Modify `server/main.py`, `server/__main__.py`; Test `server/tests/test_ledger_api.py`

`create_app` gains a `partstore=None` parameter (None means model up/download
404, but part *metadata* routes still work if the ledger is present — a part
can exist as a number/name without a stored file). Routes, all behind
`_require_ledger`:

| Route | Behaviour |
|---|---|
| `GET /api/parts` | `{parts: [...]}`, archived hidden; each part carries its `default_recipe` id (or null) |
| `POST /api/parts` | `{part_number, revision?, name?, notes?}` → 201 the row; 409 on duplicate (part_number, revision) |
| `GET /api/parts/{id}` | part + its recipes; 404 unknown |
| `PUT /api/parts/{id}` | edit name/notes/revision/part_number; 404 unknown |
| `DELETE /api/parts/{id}` | archive (soft); also `partstore.delete` if present; 204 |
| `POST /api/parts/{id}/model` | multipart model upload → `partstore.save`, then `update_part` with the returned filename/sha256/bytes. 400 on non-model ext or no partstore; **sync `def`** (large body off the event loop, same as the slice/FTPS routes) |
| `GET /api/parts/{id}/model` | stream the stored bytes; 404 if none; sync `def` |
| `POST /api/parts/{id}/recipes` | add a recipe; 201 |
| `PUT /api/parts/{id}/recipes/{rid}` | edit; `is_default: true` routes to `set_default_recipe` |
| `DELETE /api/parts/{id}/recipes/{rid}` | archive recipe; 204 |

- [ ] TDD each route (mirror `test_ledger_api.py` fakes; add a `FakePartStore`
  for the model routes). Assert: 404 without a ledger; duplicate part → 409;
  model upload round-trips through `GET .../model` with a matching sha256;
  a recipe marked default clears the previous default. Commit
  `feat(api): parts and recipes routes`.

- [ ] In `__main__.py`, build `PartStore(ledger.db's dir)` and pass it. In
  `--mock`, point it at `runs-mock/`.

---

## Task 6: Frontend API wrappers + doc pass (backend checkpoint)

**Files:** Modify `frontend/src/api/printer.js`, `master.md`

- [ ] Append wrappers: `fetchParts`, `fetchPart`, `createPart`, `updatePart`,
  `archivePart`, `uploadPartModel(id, file)`, `addRecipe`, `updateRecipe`,
  `archiveRecipe`. Same `detail(res)` reuse and shape as Phase 1's block.
- [ ] Extend `master.md` §13 (or add a short §14) documenting the catalogue:
  parts = number+revision with an on-disk model, recipes = named slice setups
  with one default, ownership local, files under the data dir. Run
  `test_docs.py`. Commit `feat(frontend): parts API wrappers; docs: the catalogue`.

**← REVIEW CHECKPOINT.** Backend + wrappers are self-contained and fully
tested here. Tasks 7–8 (UI and slice-integration) are where a human should
look before they land.

---

## Task 7: The Parts page (review checkpoint — verified by build + eye)

**Files:** Create `Parts.jsx`, `PartList.jsx`, `PartForm.jsx`,
`RecipeEditor.jsx`; modify `pageRegistry.jsx`.

Follow the History page's shape (a panel per selection, `PageFrame` with no
title, the UI kit's `Card`/`Button`/`Field`). The page is **fleet-wide**, not
per-printer (a part is not tied to one machine), so unlike History it does not
key on `printer.serial` — it lists all parts, and selecting one shows its
model info + recipe editor. Register as `parts: { title: "Parts", group:
"Monitor", component: Parts }` after `history`.

- [ ] Build (`npm run build`) and eyeball under `python -m server --mock`:
  add a part, upload an STL, add two recipes, toggle the default. Commit
  `feat(frontend): Parts catalogue page`.

---

## Task 8: Slice a stored part with a stored recipe (review checkpoint)

The payoff: instead of uploading an STL + choosing preset/material every time,
pick a part + recipe and slice it — and record `part_id`/`recipe_id` on the
resulting queue job, so when that job prints, Phase 1's start route already
carries the part into the run row (the run's `part_id` column exists and is
threaded through `open_run`).

**Files:** Modify `server/slicejobs.py`, `server/main.py`, and the Slice page.

- [ ] Add `SliceCoordinator.submit_from_part(serial, part_id, recipe_id) ->
  job_id`: reads the model bytes from `PartStore`, the tier/material/supports
  from the recipe, and calls the SAME internal path `submit` uses — but stamps
  `part_id` and `recipe_id` onto the job dict so `PrintQueue.add` carries them.
- [ ] The start route (Phase 1) already writes `part_id` onto the run via
  `open_run(**fields)` if the job carries it — confirm and add `part_id`/
  `recipe_id` to the job→run field passthrough in `start_queue_job` (one line
  each, both in `RUN_WRITABLE`).
- [ ] Route `POST /api/printers/{serial}/slice/from-part` `{part_id,
  recipe_id}` → 202 `{job_id}`.
- [ ] Frontend: on the Slice page (or the Parts page) a "Slice for <printer>"
  action that calls it.
- [ ] Test the coordinator path with a fake PartStore + fake run_slice; assert
  the produced queue job carries `part_id`/`recipe_id`, and (via the existing
  start-route tests' shape) that starting it records a run with that `part_id`.
- [ ] **Hardware/integration gate:** slice a real stored part, start it, and
  confirm the run row carries the `part_id` and reaches FINISH with pieces.
  Stays unverified on hardware until run, per §1.1.

---

## Self-review notes

- **Spec coverage (§9 Phase 2):** parts catalogue (Tasks 1–2, 5), recipes
  (Tasks 3, 5), model file storage (Task 4), pointing the slice flow at a
  stored recipe (Task 8), Parts UI (Task 7). §4.1 columns are all present in
  the Task 1 DDL.
- **Deferred, correctly:** filament (Phase 3), Supabase (Phase 4), arm
  (Phase 5). No task here touches them.
- **Migration safety:** Task 1 proves a v1→v2 upgrade preserves data and never
  edits `MIGRATIONS[1]`.
- **Consistent names across tasks:** `create_part`, `get_part`, `find_part`,
  `list_parts`, `update_part`, `archive_part`; `add_recipe`, `recipes_for`,
  `get_recipe`, `update_recipe`, `set_default_recipe`, `default_recipe`,
  `archive_recipe`; `PartStore.save/open_bytes/delete`;
  `submit_from_part`.
- **Open question carried from the spec (§12 Q3):** one part per run. If mixed
  plates arrive, a `run_parts` join is the change — not attempted here.
