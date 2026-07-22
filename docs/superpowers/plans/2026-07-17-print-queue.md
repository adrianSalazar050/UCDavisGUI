# Print Queue (Phase 2) Implementation Plan

> **STATUS: SHIPPED (2026-07-17).**
>
> Historical record, not maintained. **`master.md` is authoritative wherever this file disagrees with it.**
>
> Task checkboxes below were never ticked during execution; read the status line above, not the boxes.
>
> The queue job schema here is incomplete: jobs now also carry `model_id`, and starting a job whose model conflicts with the printer's is refused with a 409 (`master.md` §5.3).
>
> Stale throughout, and not corrected in place:
> - any **test count** (this tree quotes 194, 228, 316, ~100 — run `python -m pytest -q` instead)
> - the printer: an **A1 mini** (`0300CA633005010`, `192.168.137.2`) until 2026-07-19, an **A1** (`03919D531805572`) since 2026-07-21

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development. Steps use checkbox (`- [ ]`) syntax.

**Goal:** A per-printer **planner** queue: order SD-card jobs, show each file's estimated print time + filament grams (read from the sliced `.gcode.3mf`), running totals, and a projected finish time. **Planner only** — it does not command the printer.

**Architecture:** `server/threemf.py` (pure XML parser), `server/sdcard.fetch_file` (FTPS download), `server/queue.py` + `queues.json` (per-serial ordered job list + totals, testable against `tmp_path`), API routes that orchestrate them, and a new **Queue** page. Design is from the approved spec (see "Phase 2 details — Queue"); the 3MF format below is **confirmed against the real printer**.

**Tech Stack:** Python 3 (stdlib `zipfile`/`xml.etree`), FastAPI, FTPS via the existing `ImplicitFTP_TLS`, React+Vite, pytest.

**Spec:** `docs/superpowers/specs/2026-07-17-failure-detection-autostop-queue-design.md`.

**Confirmed 3MF format (real printer, `smallCylinderPLA15m17s` → 917s):** a `.gcode.3mf` is a zip; `Metadata/slice_info.config` is XML:
```xml
<config><plate>
  <metadata key="prediction" value="917"/>   <!-- print time, SECONDS -->
  <metadata key="weight" value="1.69"/>       <!-- filament, GRAMS -->
  <filament id="1" type="PLA" color="#000000" used_g="1.69" used_m="0.57"/>
</plate></config>
```
Multiple `<plate>` elements are possible (multi-plate projects); sum them for the file total (single-plate is the norm for a print-ready file).

**Run tests from repo root with `python -m pytest`.** Baseline: **228 passed**. Frontend: `cd frontend && npm run build` (no JS test runner).

---

### Task 1: `server/threemf.py` — parse time + grams (pure)

**Files:** Create `server/threemf.py`; Create `server/tests/test_threemf.py`.

- [ ] **Step 1: Write failing tests** — `server/tests/test_threemf.py`. Build a fixture 3MF in-memory (a zip with a `Metadata/slice_info.config`) so no network is needed:

```python
import io
import zipfile

from server.threemf import parse_slice_info, SLICE_INFO_PATH

SLICE_INFO = """<?xml version="1.0" encoding="UTF-8"?>
<config>
  <header><header_item key="X-BBL-Client-Type" value="slicer"/></header>
  <plate>
    <metadata key="index" value="1"/>
    <metadata key="prediction" value="917"/>
    <metadata key="weight" value="1.69"/>
    <filament id="1" type="PLA" color="#000000" used_g="1.69" used_m="0.57"/>
  </plate>
</config>"""


def _zip(members):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        for name, data in members.items():
            z.writestr(name, data)
    return buf.getvalue()


def test_parses_prediction_and_weight():
    got = parse_slice_info(_zip({SLICE_INFO_PATH: SLICE_INFO}))
    assert got["seconds"] == 917
    assert got["grams"] == 1.69
    assert got["filaments"][0]["type"] == "PLA"


def test_sums_multiple_plates():
    two = SLICE_INFO.replace("</config>",
        '<plate><metadata key="prediction" value="100"/>'
        '<metadata key="weight" value="2.0"/></plate></config>')
    got = parse_slice_info(_zip({SLICE_INFO_PATH: two}))
    assert got["seconds"] == 1017      # 917 + 100
    assert round(got["grams"], 2) == 3.69


def test_missing_slice_info_is_all_none():
    got = parse_slice_info(_zip({"3D/3dmodel.model": "<model/>"}))
    assert got == {"seconds": None, "grams": None, "filaments": []}


def test_not_a_zip_is_all_none():
    assert parse_slice_info(b"not a zip") == {"seconds": None, "grams": None,
                                              "filaments": []}


def test_malformed_xml_is_all_none():
    got = parse_slice_info(_zip({SLICE_INFO_PATH: "<config><plate"}))
    assert got == {"seconds": None, "grams": None, "filaments": []}
```

- [ ] **Step 2: Run to verify it fails** — `python -m pytest server/tests/test_threemf.py -v` → `ModuleNotFoundError`.

- [ ] **Step 3: Implement** — `server/threemf.py`:

```python
"""Parse a sliced Bambu .gcode.3mf for its estimated print time + filament use.

A .gcode.3mf is a zip; Metadata/slice_info.config is XML with one <plate> per
plate, each carrying <metadata key="prediction" .../> (seconds) and
<metadata key="weight" .../> (grams), plus <filament .../> rows. Confirmed
against a real A1 mini file. Pure and tolerant: any missing/corrupt part yields
None for that field, so the queue UI can fall back to manual entry.
"""
from __future__ import annotations

import io
import xml.etree.ElementTree as ET
import zipfile

SLICE_INFO_PATH = "Metadata/slice_info.config"

_EMPTY = {"seconds": None, "grams": None, "filaments": []}


def _num(v, cast):
    try:
        return cast(v)
    except (TypeError, ValueError):
        return None


def parse_slice_info(data: bytes) -> dict:
    """bytes of a .gcode.3mf -> {seconds:int|None, grams:float|None,
    filaments:[{type,color,used_g}]}. Never raises."""
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as z:
            raw = z.read(SLICE_INFO_PATH)
    except (zipfile.BadZipFile, KeyError, OSError):
        return dict(_EMPTY)
    try:
        root = ET.fromstring(raw)
    except ET.ParseError:
        return dict(_EMPTY)

    seconds, grams, filaments = None, None, []
    for plate in root.iter("plate"):
        for md in plate.findall("metadata"):
            key, val = md.get("key"), md.get("value")
            if key == "prediction":
                s = _num(val, int)
                if s is not None:
                    seconds = (seconds or 0) + s
            elif key == "weight":
                g = _num(val, float)
                if g is not None:
                    grams = (grams or 0.0) + g
        for f in plate.findall("filament"):
            filaments.append({"type": f.get("type"), "color": f.get("color"),
                              "used_g": _num(f.get("used_g"), float)})
    if grams is not None:
        grams = round(grams, 2)
    return {"seconds": seconds, "grams": grams, "filaments": filaments}
```

- [ ] **Step 4: Run to verify it passes** — `python -m pytest server/tests/test_threemf.py -v` → PASS.

- [ ] **Step 5: Commit** — `git add server/threemf.py server/tests/test_threemf.py && git commit -m "feat(queue): threemf.py parses print time + filament grams from slice_info.config"`

---

### Task 2: `sdcard.fetch_file` — FTPS download

**Files:** Modify `server/sdcard.py`; Modify `server/tests/test_sdcard.py`.

- [ ] **Step 1: Write failing test** — append to `server/tests/test_sdcard.py`. Reuse the module's existing fake-FTP pattern (grep the test file for how `ImplicitFTP_TLS`/`list_dir` is faked). The test substitutes a fake FTP whose `retrbinary` feeds bytes to the callback, and asserts `fetch_file` returns them + normalizes/guards the path. If the file has no such fake yet, add a minimal one:

```python
def test_fetch_file_returns_bytes(monkeypatch):
    import server.sdcard as sd

    class FakeFTP:
        def __init__(self, *a, **k): pass
        def connect(self, *a, **k): pass
        def login(self, *a, **k): pass
        def prot_p(self): pass
        def set_pasv(self, *a, **k): pass
        def retrbinary(self, cmd, cb):
            assert cmd == "RETR /Benchy.gcode.3mf"
            cb(b"PK\x03\x04zipbytes")
        def close(self): pass

    monkeypatch.setattr(sd, "ImplicitFTP_TLS", FakeFTP)
    assert sd.fetch_file("h", "code", "/Benchy.gcode.3mf") == b"PK\x03\x04zipbytes"


def test_fetch_file_rejects_traversal():
    import server.sdcard as sd
    import pytest
    with pytest.raises(sd.SdError):
        sd.fetch_file("h", "code", "/../etc/passwd")
```

- [ ] **Step 2: Run to verify it fails** — `python -m pytest server/tests/test_sdcard.py -k fetch_file -v` → FAIL (`AttributeError`).

- [ ] **Step 3: Implement** — in `server/sdcard.py`, add after `list_dir`:

```python
def fetch_file(host: str, access_code: str, path: str) -> bytes:
    """Download one file off the card over FTPS. Always raises SdError on
    failure (same contract as list_dir); the message never contains the
    access code. Path is traversal-guarded via normalize_path."""
    target = normalize_path(path)
    ftp = ImplicitFTP_TLS(context=_ssl_context(), timeout=TIMEOUT_S)
    chunks: list[bytes] = []
    try:
        ftp.connect(host, FTPS_PORT)
        ftp.login(FTP_USER, access_code)
        ftp.prot_p()
        ftp.set_pasv(True)
        ftp.retrbinary(f"RETR {target}", chunks.append)
        return b"".join(chunks)
    except SdError:
        raise
    except ftplib.all_errors as e:
        raise SdError(f"Could not fetch {target} on {host}: {e}") from e
    except Exception as e:  # putline ValueError etc. -> clean SdError
        raise SdError(f"Could not fetch {target} on {host}: unexpected "
                      f"error ({type(e).__name__})") from e
    finally:
        try:
            ftp.close()
        except Exception:
            pass
```

- [ ] **Step 4: Run to verify it passes** — `python -m pytest server/tests/test_sdcard.py -v` → PASS.

- [ ] **Step 5: Commit** — `git commit -m "feat(queue): sdcard.fetch_file downloads a file over FTPS"`

---

### Task 3: `server/queue.py` + `queues.json`

**Files:** Create `server/queue.py`; Create `server/tests/test_queue.py`.

- [ ] **Step 1: Write failing tests** — `server/tests/test_queue.py`:

```python
import time

from server.queue import QueueStore, PrintQueue


def job(id, name, seconds=600, grams=10.0, source="3mf"):
    return {"id": id, "sd_path": "/" + name, "name": name,
            "seconds": seconds, "grams": grams, "source": source}


def test_add_remove_reorder_and_get(tmp_path):
    q = PrintQueue(QueueStore(tmp_path / "queues.json"))
    q.add("S1", job("a", "A.3mf"))
    q.add("S1", job("b", "B.3mf"))
    assert [j["id"] for j in q.get("S1")] == ["a", "b"]
    q.reorder("S1", ["b", "a"])
    assert [j["id"] for j in q.get("S1")] == ["b", "a"]
    assert q.remove("S1", "b") is True
    assert [j["id"] for j in q.get("S1")] == ["a"]
    assert q.remove("S1", "nope") is False


def test_totals(tmp_path):
    q = PrintQueue(QueueStore(tmp_path / "queues.json"))
    q.add("S1", job("a", "A.3mf", seconds=600, grams=10.0))
    q.add("S1", job("b", "B.3mf", seconds=1200, grams=5.5))
    t = q.totals("S1", now=1000.0)
    assert t["seconds"] == 1800 and t["grams"] == 15.5
    assert t["finish_epoch"] == 1000.0 + 1800   # planner hint, labeled ~ in UI


def test_totals_ignore_none_metrics(tmp_path):
    q = PrintQueue(QueueStore(tmp_path / "queues.json"))
    q.add("S1", job("a", "A.3mf", seconds=None, grams=None, source="manual"))
    q.add("S1", job("b", "B.3mf", seconds=600, grams=10.0))
    t = q.totals("S1", now=0.0)
    assert t["seconds"] == 600 and t["grams"] == 10.0


def test_persistence_round_trip(tmp_path):
    p = tmp_path / "queues.json"
    PrintQueue(QueueStore(p)).add("S1", job("a", "A.3mf"))
    assert [j["id"] for j in PrintQueue(QueueStore(p)).get("S1")] == ["a"]
```

- [ ] **Step 2: Run to verify it fails** — `python -m pytest server/tests/test_queue.py -v` → `ModuleNotFoundError`.

- [ ] **Step 3: Implement** — `server/queue.py`. `QueueStore` mirrors `PrinterStore`'s atomic write (temp + `os.replace`, utf-8-sig read, tolerant of a bad file → `{}`); `PrintQueue` holds `{serial: [job,...]}` guarded by a lock, persisting after each mutation. Jobs are plain dicts (no secrets — only filenames + cached metrics). `totals(serial, now)` sums non-None `seconds`/`grams` and returns `{"seconds","grams","finish_epoch"}` (`finish_epoch = now + seconds`, or `None` if seconds is 0/None). Follow `server/store.py`'s file-safety patterns exactly (atomic, never raise on read). Keep it pure of network/registry — the API layer does the fetch+parse and hands finished job dicts in.

> Full method set: `add(serial, job)`, `remove(serial, id) -> bool`, `reorder(serial, ids)` (keep only known ids, in the given order, drop unknowns), `get(serial) -> list`, `totals(serial, now=None)`. Lock discipline like `registry.py`: never hold the lock across the disk write (snapshot under lock, write outside).

- [ ] **Step 4: Run to verify it passes** — PASS.

- [ ] **Step 5: Commit** — `git commit -m "feat(queue): per-printer PrintQueue + queues.json persistence + totals"`

---

### Task 4: Queue API routes

**Files:** Modify `server/main.py`, `server/__main__.py` (build the `PrintQueue`); Modify `server/tests/test_api.py`.

- [ ] **Step 1: Write failing tests** — append to `server/tests/test_api.py`. `create_app` gains a `queue=None` param; a `FakeQueue` records calls. Routes:
  - `GET /api/printers/{serial}/queue` → `{jobs, totals}`.
  - `POST /api/printers/{serial}/queue` body `{sd_path}` → fetches via `svc.list`… no: fetch via `sdcard.fetch_file(host, code)` — the route gets host/code from the **service** is wrong (service hides the code). Instead the route calls a small injected `fetch(serial, sd_path) -> bytes` provided by `__main__` (closes over the registry) OR the registry exposes it. To keep the access code out of the route, add `registry.fetch_sd_file(serial, path) -> bytes` (looks up cfg, calls `sdcard.fetch_file(cfg.host, cfg.access_code, path)`), mirroring how `list_files` already hides the code behind the service. Then the route: `data = registry.fetch_sd_file(serial, sd_path)`, `meta = threemf.parse_slice_info(data)`, build job (uuid id, name = basename, seconds/grams from meta, source `"3mf"` if seconds or grams else `"manual"`), `queue.add(serial, job)`. On `SdError`/parse-empty → still add with `source:"manual"`, null metrics (201). Return the job.
  - `DELETE /api/printers/{serial}/queue/{id}` → 204 / 404.
  - `PUT /api/printers/{serial}/queue` body `{ids}` → reorder → `{jobs, totals}`.
  Test: GET envelope; POST parses a fixture 3MF (monkeypatch `registry.fetch_sd_file` to return fixture bytes) → job has seconds/grams; POST when fetch raises SdError → job source "manual", null metrics, still 201; DELETE 204/404; PUT reorders. Confirm no `access_code` in any response.

- [ ] **Step 2–4:** implement the four routes in `server/main.py` (near the files route), add `registry.fetch_sd_file(serial, path)` to `server/registry.py` (+ a `MockPrinter`/mock path that returns fixture bytes so `--mock` works — the mock registry can return a tiny built-in fixture 3MF), wire `PrintQueue(QueueStore(runs_dir.parent / "queues.json"))` in `server/__main__.py` and pass `queue=` to `create_app`. Full suite green.

- [ ] **Step 5: Commit** — `git commit -m "feat(queue): queue API routes (GET/POST/DELETE/PUT) + registry.fetch_sd_file"`

---

### Task 5: Hardware verification (operator-run, non-destructive)

- [ ] Full suite green (`python -m pytest -q`). Then against the real printer (read-only): `POST /api/printers/<real-serial>/queue {"sd_path":"/smallCylinderPLA15m17s_stripped.gcode.3mf"}` and confirm the returned job has `seconds: 917`, `grams: 1.69`, `source: "3mf"`. Confirm the access code appears in no response. (The controller runs this with the real printer added.)

---

### Task 6: Frontend — api wrappers + Queue page

**Files:** Modify `frontend/src/api/printer.js`; Create `frontend/src/pages/Queue.jsx` + `frontend/src/components/queue/*`; Modify `frontend/src/app/pageRegistry.jsx`, `frontend/src/styles.css`.

- [ ] **Step 1:** `api/printer.js` — add `fetchQueue(serial)`, `addQueueJob(serial, sd_path)`, `removeQueueJob(serial, id)`, `reorderQueue(serial, ids)` (mirror the existing fetch-wrapper + `detail()` style).

- [ ] **Step 2:** Queue page (dedicated, per mockup 1). Reads the selected printer (like Dashboard/SD Files). Polls `fetchQueue(serial)` (queue state isn't on the WS; poll every ~3 s or refetch after each mutation). Renders: a header with an **"Add from SD"** button that opens a picker reusing the SD listing (`fetchFiles(serial, "/")` filtered to `.3mf`, clicking one calls `addQueueJob`); a job table (name, time `Nh Mm`, grams, remove ✕) with **up/down reorder** buttons (drag is optional — up/down is simpler and accessible) calling `reorderQueue`; a totals footer (`N jobs · total Xh Ym · Zg · finish ≈ H:MM`). Non-capture/absent printer handled with a hint like the other pages. Match existing components (`Card`/`Button`/`PageFrame`/`Columns`/`FileTable`) and `styles.css` conventions. Time formatting: seconds → `Xh Ym` (or `Ym` when <1h); `null` → "—".

- [ ] **Step 3:** `pageRegistry.jsx` — add `queue: { title: "Queue", group: "Monitor", component: Queue }` after `sdfiles`.

- [ ] **Step 4:** `styles.css` — queue table + totals bar classes (reuse `file-table`/`add-form` conventions where possible).

- [ ] **Step 5:** `cd frontend && npm run build` clean. Commit `feat(frontend): Queue page (add-from-SD, reorder, totals, projected finish)`.

---

### Task 7: Visual verification (operator-run)

- [ ] `npm run build` + `python -m server --mock`; open the **Queue** page for a mock printer: Add-from-SD lists `.3mf` files, adding one shows its time/grams (from the mock fixture 3MF), reorder + remove work, totals + `≈` finish update. Then (optional) with the real printer added, queue `smallCylinderPLA15m17s_stripped.gcode.3mf` and confirm 15m / 1.69 g render.

---

## Self-Review (completed while writing)

**Spec coverage:** threemf parse (T1), FTPS fetch (T2), per-serial queue + totals + persistence (T3), GET/POST/DELETE/PUT routes + fetch+parse orchestration hiding the access code behind `registry.fetch_sd_file` (T4), dedicated Queue page with add-from-SD + reorder + totals + `≈` finish (T6). Planner-only — no printer control. Hardware + visual verification (T5, T7).

**Secret hygiene:** the access code stays behind `registry.fetch_sd_file` (route never sees it), same pattern as `list_files`; jobs/queues.json hold only filenames + metrics.

**3MF grounded in real data:** `prediction`→seconds, `weight`→grams, summed across `<plate>`s; tolerant of every missing/corrupt part (→ manual entry).

**Not in scope:** auto-start / "Print next" (explicitly planner-only); no WS for queue state (polled/refetched — it's not high-frequency like detection).
