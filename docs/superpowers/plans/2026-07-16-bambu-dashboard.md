# Bambu Monitor Dashboard Implementation Plan

> **STATUS: SHIPPED (2026-07-16), then SUPERSEDED** by the multi-printer plan.
>
> Historical record, not maintained. **`master.md` is authoritative wherever this file disagrees with it.**
>
> Task checkboxes below were never ticked during execution; read the status line above, not the boxes.
>
> The `--host/--serial/--access-code` invocation near the end no longer exists; printers are added in the browser.
>
> Stale throughout, and not corrected in place:
> - any **test count** (this tree quotes 194, 228, 316, ~100 — run `python -m pytest -q` instead)
> - the printer: an **A1 mini** (`0300CA633005010`, `192.168.137.2`) until 2026-07-19, an **A1** (`03919D531805572`) since 2026-07-21

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** A live browser dashboard for the Bambu A1 mini rig — printer state, temps, layer/progress, HMS errors, and the newest captured frame — per the approved spec in `docs/superpowers/specs/2026-07-16-bambu-dashboard-design.md`.

**Architecture:** A FastAPI backend (`server/`) wraps the existing `bambu_link.py` MQTT client, pushes a curated state summary over a WebSocket, and serves the newest `runs/**/frames/layer_*.jpg` over HTTP (never touching the webcam). A Vite + React frontend (`frontend/`) built per `FRONTEND-STACK-GUIDE.md` (design tokens, hand-rolled ui kit, page registry, dark sidebar shell) renders it. A `--mock` mode fakes an endless print so everything runs with zero hardware.

**Tech Stack:** Python 3.11+, FastAPI, uvicorn, paho-mqtt (existing), OpenCV/numpy (existing, mock frames only), pytest + httpx (tests); React 19, Vite 6, plain JSX, one `styles.css`.

**Environment notes for the engineer:**
- Windows machine, PowerShell. Do NOT chain commands with `&&` (PowerShell 5.1 rejects it); run commands one at a time.
- The repo root is `c:\Users\adria\OneDrive\Escritorio\GUI_UCDavis`. Run all `pytest` commands from the repo root.
- `bambu_link.py`, `capture.py`, `check_registration.py`, `probe_gcode.py` must NOT be modified.
- The printer sends PARTIAL MQTT updates; `BambuLink` (in `bambu_link.py`) already deep-merges them and exposes `.state`, `.connected` (a `threading.Event`), and callbacks. You only consume it.

---

## File structure (what gets created)

```
.gitignore                       # Task 1
requirements.txt                 # Task 1
server/
  __init__.py                    # Task 1 (empty)
  runs.py                        # Task 2 — active-run + newest-frame discovery
  printer.py                     # Task 3 (build_summary) + Task 4 (services)
  main.py                        # Task 5 — FastAPI app factory
  __main__.py                    # Task 6 — CLI entry (python -m server)
  tests/
    __init__.py                  # Task 1 (empty)
    test_runs.py                 # Task 2
    test_summary.py              # Task 3
    test_services.py             # Task 4
    test_api.py                  # Task 5
frontend/
  package.json                   # Task 7
  vite.config.js                 # Task 7
  index.html                     # Task 7
  src/
    main.jsx                     # Task 7
    styles.css                   # Task 7 — ALL styling (tokens + ui-* classes)
    App.jsx                      # Task 7 placeholder, real shell in Task 9
    app/pageRegistry.jsx         # Task 9
    api/printer.js               # Task 9
    hooks/usePrinter.js          # Task 9
    components/ui/               # Task 8 — Button, Card, Section, PageFrame,
                                 #   Stack, Columns, StatTile, StatusPill, NavGroup
    components/dashboard/        # Task 10 — CameraCard, PrintInfoCard, HmsCard
    pages/Dashboard.jsx          # Task 10
```

One deliberate addition to the spec payload: a `"printer"` field (`"<host>"` or `"MOCK"`) so the topbar can show which printer it's talking to without a second endpoint.

---

### Task 1: Repo scaffolding (git, ignore, deps, packages)

**Files:**
- Create: `.gitignore`, `requirements.txt`, `server/__init__.py`, `server/tests/__init__.py`

- [ ] **Step 1: Initialize git** (the repo is not yet under version control; the user was told and approved proceeding)

Run: `git init`
Expected: `Initialized empty Git repository in .../GUI_UCDavis/.git/`

- [ ] **Step 2: Create `.gitignore`**

```gitignore
__pycache__/
*.pyc
.venv/
venv/
.pytest_cache/

node_modules/
frontend/dist/

runs/
runs-mock/
```

- [ ] **Step 3: Create `requirements.txt`**

```text
# runtime
paho-mqtt>=2.0
opencv-python>=4.9
numpy>=1.26
fastapi>=0.110
uvicorn[standard]>=0.29

# tests
pytest>=8.0
httpx>=0.27
```

- [ ] **Step 4: Install dependencies**

Run: `pip install -r requirements.txt`
Expected: exits 0. (paho-mqtt/opencv/numpy are likely already present.)

- [ ] **Step 5: Create empty package markers**

Create `server/__init__.py` and `server/tests/__init__.py` as empty (zero-byte) files. Both MUST exist, or `import server.runs` inside the tests will fail — pytest walks up past directories containing `__init__.py` to find the import root.

- [ ] **Step 6: Sanity-check pytest wiring**

Run: `pytest server/tests -v`
Expected: `no tests ran` (exit code 5) — that is success at this stage; it proves pytest resolves the directory.

- [ ] **Step 7: Commit everything that already exists plus the scaffolding**

```bash
git add -A
git commit -m "chore: scaffold server package, deps, gitignore (existing scripts included as-is)"
```

---

### Task 2: `server/runs.py` — newest-frame discovery

The dashboard never opens the webcam. It serves the newest `layer_NNNN.jpg` that `capture.py` wrote. "Active run" = the run directory whose `frames/` holds the most recently modified `layer_*.jpg`, provided that mtime is under 30 minutes old. "Newest frame" = the highest layer number within that run.

**Files:**
- Create: `server/runs.py`
- Test: `server/tests/test_runs.py`

- [ ] **Step 1: Write the failing tests**

Create `server/tests/test_runs.py`:

```python
import os
import time

from server.runs import ACTIVE_WINDOW_S, find_active_run, newest_frame


def make_frame(runs_dir, run, layer, age_s=0.0):
    frames = runs_dir / run / "frames"
    frames.mkdir(parents=True, exist_ok=True)
    p = frames / f"layer_{layer:04d}.jpg"
    p.write_bytes(b"\xff\xd8fake-jpeg")
    t = time.time() - age_s
    os.utime(p, (t, t))
    return p


def test_missing_dir_is_none(tmp_path):
    assert find_active_run(tmp_path / "nope") is None
    assert newest_frame(tmp_path / "nope") is None


def test_empty_dir_is_none(tmp_path):
    assert newest_frame(tmp_path) is None


def test_picks_run_with_most_recent_frame(tmp_path):
    make_frame(tmp_path, "20260101T000000_old", 50, age_s=600)
    make_frame(tmp_path, "20260716T120000_new", 3, age_s=5)
    assert find_active_run(tmp_path).name == "20260716T120000_new"


def test_stale_run_is_not_active(tmp_path):
    make_frame(tmp_path, "20260101T000000_old", 50, age_s=ACTIVE_WINDOW_S + 60)
    assert find_active_run(tmp_path) is None
    assert newest_frame(tmp_path) is None


def test_newest_frame_is_highest_layer_of_active_run(tmp_path):
    # layer 2 written most recently, but layer 10 is the highest layer number
    make_frame(tmp_path, "20260716T120000_a", 10, age_s=30)
    make_frame(tmp_path, "20260716T120000_a", 2, age_s=1)
    info = newest_frame(tmp_path)
    assert info["layer"] == 10
    assert info["run"] == "20260716T120000_a"
    assert info["path"].name == "layer_0010.jpg"


def test_ignores_non_frame_files(tmp_path):
    make_frame(tmp_path, "20260716T120000_a", 1, age_s=1)
    junk = tmp_path / "20260716T120000_a" / "frames" / "thumbs.db"
    junk.write_bytes(b"junk")
    info = newest_frame(tmp_path)
    assert info["layer"] == 1
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest server/tests/test_runs.py -v`
Expected: FAIL / error with `ModuleNotFoundError: No module named 'server.runs'`

- [ ] **Step 3: Implement `server/runs.py`**

```python
"""Locate the newest captured frame under a runs/ directory.

capture.py writes runs/<ts>_<name>/frames/layer_NNNN.jpg. The dashboard
serves the newest of those instead of opening the webcam, so it can never
steal the camera from capture.py (Windows allows only one process per
camera device).
"""
from __future__ import annotations

import pathlib
import re
import time

FRAME_RE = re.compile(r"^layer_(\d{1,6})\.jpg$")
ACTIVE_WINDOW_S = 30 * 60  # a run is "active" if it wrote a frame this recently


def find_active_run(runs_dir: pathlib.Path,
                    now: float | None = None) -> pathlib.Path | None:
    """Run dir whose frames/ has the most recently modified layer_*.jpg,
    if that write is within ACTIVE_WINDOW_S. Else None."""
    now = time.time() if now is None else now
    best_dir, best_mtime = None, -1.0
    if not runs_dir.is_dir():
        return None
    for run in runs_dir.iterdir():
        frames = run / "frames"
        if not frames.is_dir():
            continue
        for f in frames.iterdir():
            if not FRAME_RE.match(f.name):
                continue
            mtime = f.stat().st_mtime
            if mtime > best_mtime:
                best_dir, best_mtime = run, mtime
    if best_dir is None or now - best_mtime > ACTIVE_WINDOW_S:
        return None
    return best_dir


def newest_frame(runs_dir: pathlib.Path, now: float | None = None) -> dict | None:
    """{"path": Path, "layer": int, "run": str} for the highest-numbered
    frame of the active run, or None."""
    run = find_active_run(runs_dir, now)
    if run is None:
        return None
    best: tuple[int, pathlib.Path] | None = None
    for f in (run / "frames").iterdir():
        m = FRAME_RE.match(f.name)
        if not m:
            continue
        layer = int(m.group(1))
        if best is None or layer > best[0]:
            best = (layer, f)
    if best is None:
        return None
    return {"path": best[1], "layer": best[0], "run": run.name}
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest server/tests/test_runs.py -v`
Expected: 6 passed

- [ ] **Step 5: Commit**

```bash
git add server/runs.py server/tests/test_runs.py
git commit -m "feat(server): newest-frame discovery over runs/ directories"
```

---

### Task 3: `server/printer.py` — `build_summary()`

The curated payload pushed to the browser. Pure function so it's trivially testable. Uses `decode_hms` from the existing `bambu_link.py`.

**Files:**
- Create: `server/printer.py` (this task adds the module header + `build_summary`; Task 4 appends the service classes to the same file)
- Test: `server/tests/test_summary.py`

- [ ] **Step 1: Write the failing tests**

Create `server/tests/test_summary.py`:

```python
from server.printer import STALE_S, build_summary


def test_empty_state_disconnected():
    s = build_summary({}, None, False, "MOCK")
    assert s["connection"] == "disconnected"
    assert s["report_age_s"] is None
    assert s["hms"] == []
    assert s["layer_num"] is None
    assert s["gcode_state"] is None
    assert s["printer"] == "MOCK"


def test_running_state_ok():
    st = {
        "gcode_state": "RUNNING", "layer_num": 3, "total_layer_num": 100,
        "mc_percent": 3, "nozzle_temper": 219.8,
        "hms": [{"attr": 0x03000100, "code": 0x00010007}],
    }
    s = build_summary(st, 1.23, True, "192.168.1.42")
    assert s["connection"] == "ok"
    assert s["report_age_s"] == 1.2
    assert s["hms"] == ["0300_0100_0001_0007"]
    assert s["layer_num"] == 3
    assert s["printer"] == "192.168.1.42"


def test_stale_when_report_old_or_absent():
    assert build_summary({}, STALE_S + 5, True, "x")["connection"] == "stale"
    assert build_summary({}, None, True, "x")["connection"] == "stale"


def test_hms_none_is_empty_list():
    assert build_summary({"hms": None}, 0.1, True, "x")["hms"] == []
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest server/tests/test_summary.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'server.printer'`

- [ ] **Step 3: Implement the module header and `build_summary` in `server/printer.py`**

```python
"""Printer state services for the dashboard.

PrinterService wraps BambuLink: keeps its merged state, timestamps reports,
reconnects in the background. MockPrinter fakes the same interface with an
endless synthetic print and writes real frame JPEGs so the whole dashboard
works with no hardware.

Both expose: start(), stop(), summary() -> dict.
"""
from __future__ import annotations

import logging
import pathlib
import sys
import threading
import time
from datetime import datetime

import cv2
import numpy as np

# bambu_link.py lives at the repo root, one level above this package.
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))
from bambu_link import BambuLink, decode_hms  # noqa: E402

log = logging.getLogger("server.printer")

STALE_S = 15.0   # connected but no report for this long -> "stale"
RETRY_S = 10.0   # MQTT reconnect attempt interval

SUMMARY_FIELDS = (
    "gcode_state", "layer_num", "total_layer_num", "mc_percent",
    "mc_remaining_time", "nozzle_temper", "nozzle_target_temper",
    "bed_temper", "bed_target_temper", "spd_lvl", "spd_mag",
    "print_error", "fail_reason", "subtask_name", "gcode_file",
)


def build_summary(state: dict, report_age: float | None,
                  connected: bool, printer: str) -> dict:
    """Curate the merged printer state into the payload the UI consumes.

    Fields the printer hasn't reported yet are null — it sends partial
    updates, so early in a session most fields are unknown.
    """
    out = {k: state.get(k) for k in SUMMARY_FIELDS}
    out["hms"] = [decode_hms(h.get("attr", 0), h.get("code", 0))
                  for h in state.get("hms") or []]
    if not connected:
        conn = "disconnected"
    elif report_age is None or report_age > STALE_S:
        conn = "stale"
    else:
        conn = "ok"
    out["connection"] = conn
    out["report_age_s"] = None if report_age is None else round(report_age, 1)
    out["printer"] = printer
    return out
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest server/tests/test_summary.py -v`
Expected: 4 passed

- [ ] **Step 5: Commit**

```bash
git add server/printer.py server/tests/test_summary.py
git commit -m "feat(server): curated summary payload builder"
```

---

### Task 4: `PrinterService` + `MockPrinter` (append to `server/printer.py`)

**Files:**
- Modify: `server/printer.py` (append below `build_summary`)
- Test: `server/tests/test_services.py`

- [ ] **Step 1: Write the failing tests**

Create `server/tests/test_services.py`:

```python
from server.printer import MockPrinter, PrinterService


def test_service_summary_before_connect():
    # 192.0.2.1 is TEST-NET; constructing does NOT open a socket.
    svc = PrinterService("192.0.2.1", "0309TESTSERIAL", "12345678")
    s = svc.summary()
    assert s["connection"] == "disconnected"
    assert s["printer"] == "192.0.2.1"
    assert s["report_age_s"] is None


def test_mock_frame_shape(tmp_path):
    mp = MockPrinter(tmp_path)
    img = mp._frame(5)
    assert img.shape == (480, 640, 3)


def test_mock_touch_updates_summary(tmp_path):
    mp = MockPrinter(tmp_path)
    assert mp.summary()["connection"] == "stale"  # no report yet
    mp._touch({"gcode_state": "RUNNING", "layer_num": 2})
    s = mp.summary()
    assert s["layer_num"] == 2
    assert s["gcode_state"] == "RUNNING"
    assert s["connection"] == "ok"
    assert s["printer"] == "MOCK"
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest server/tests/test_services.py -v`
Expected: FAIL with `ImportError: cannot import name 'MockPrinter'`

- [ ] **Step 3: Append the two classes to `server/printer.py`**

```python
class PrinterService:
    """Real printer: owns a BambuLink, retries MQTT in the background.

    Startup must not die if the printer is off — we start disconnected and
    keep retrying every RETRY_S. Once paho has connected ONCE, its network
    loop auto-reconnects on drops, so we only drive the initial connect.
    """

    def __init__(self, host: str, serial: str, access_code: str):
        self.host = host
        self.link = BambuLink(host, serial, access_code,
                              on_state=self._on_state)
        self._last_report: float | None = None
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._connect_loop,
                                        daemon=True)

    def _on_state(self, state: dict, patch: dict) -> None:
        self._last_report = time.time()

    def start(self) -> None:
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        self.link.disconnect()

    def _connect_loop(self) -> None:
        ever_connected = False
        while not self._stop.is_set():
            if not ever_connected and not self.link.connected.is_set():
                try:
                    ever_connected = self.link.connect(timeout=5)
                except OSError as e:
                    log.warning("MQTT connect to %s failed: %s (retry in %ss)",
                                self.host, e, RETRY_S)
            self._stop.wait(RETRY_S)

    def summary(self) -> dict:
        age = (None if self._last_report is None
               else time.time() - self._last_report)
        return build_summary(self.link.state, age,
                             self.link.connected.is_set(), self.host)


class MockPrinter:
    """Endless fake print for developing the GUI with no hardware.

    Lifecycle per cycle: RUNNING (one layer every LAYER_PERIOD_S, temps
    wander, an HMS code appears during HMS_LAYERS) -> FINISH -> IDLE_S of
    idle -> new run. Frames are written as real JPEGs into a real run
    directory so the /api/frame/latest path is exercised too.
    """

    LAYERS = 30
    LAYER_PERIOD_S = 2.0
    IDLE_S = 10.0
    HMS_LAYERS = range(12, 17)

    def __init__(self, runs_dir: pathlib.Path):
        self.runs_dir = runs_dir
        self.state: dict = {"gcode_state": "IDLE"}
        self._last_report: float | None = None
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._loop, daemon=True)

    def start(self) -> None:
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()

    def summary(self) -> dict:
        age = (None if self._last_report is None
               else time.time() - self._last_report)
        return build_summary(self.state, age, True, "MOCK")

    def _touch(self, patch: dict) -> None:
        self.state.update(patch)
        self._last_report = time.time()

    def _frame(self, layer: int) -> np.ndarray:
        # Same idea as capture.py's MockCamera: a synthetic print that grows.
        img = np.full((480, 640, 3), 40, np.uint8)
        cv2.rectangle(img, (180, 380), (460, 400), (90, 90, 95), -1)
        ph = min(layer * 8, 300)
        if ph:
            cv2.rectangle(img, (270, 380 - ph), (370, 380), (30, 110, 200), -1)
        img = cv2.add(img, np.random.randint(0, 12, img.shape, dtype=np.uint8))
        cv2.putText(img, f"layer {layer}", (12, 30),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.8, (220, 220, 220), 2)
        return img

    def _loop(self) -> None:
        while not self._stop.is_set():
            ts = datetime.now().strftime("%Y%m%dT%H%M%S")
            frames = self.runs_dir / f"{ts}_mock_benchy" / "frames"
            frames.mkdir(parents=True, exist_ok=True)
            self._touch({
                "gcode_state": "RUNNING", "subtask_name": "mock_benchy",
                "gcode_file": "mock.gcode",
                "total_layer_num": self.LAYERS,
                "nozzle_target_temper": 220.0, "bed_target_temper": 60.0,
                "spd_lvl": 2, "spd_mag": 100, "print_error": 0, "hms": [],
            })
            for n in range(1, self.LAYERS + 1):
                if self._stop.wait(self.LAYER_PERIOD_S):
                    return
                mins_left = int((self.LAYERS - n) * self.LAYER_PERIOD_S / 60) + 1
                self._touch({
                    "layer_num": n,
                    "mc_percent": int(100 * n / self.LAYERS),
                    "mc_remaining_time": mins_left,
                    "nozzle_temper": 220.0 + float(np.random.randn()),
                    "bed_temper": 60.0 + float(np.random.randn()) * 0.3,
                    "hms": ([{"attr": 0x03000100, "code": 0x00010007}]
                            if n in self.HMS_LAYERS else []),
                })
                cv2.imwrite(str(frames / f"layer_{n:04d}.jpg"), self._frame(n))
            self._touch({"gcode_state": "FINISH", "hms": []})
            if self._stop.wait(self.IDLE_S):
                return
            self._touch({"gcode_state": "IDLE", "layer_num": 0,
                         "mc_percent": 0})
```

- [ ] **Step 4: Run the whole server test suite**

Run: `pytest server/tests -v`
Expected: 13 passed (6 runs + 4 summary + 3 services)

- [ ] **Step 5: Commit**

```bash
git add server/printer.py server/tests/test_services.py
git commit -m "feat(server): PrinterService (real MQTT) and MockPrinter (fake feed + frames)"
```

---

### Task 5: `server/main.py` — FastAPI app factory

**Files:**
- Create: `server/main.py`
- Test: `server/tests/test_api.py`

- [ ] **Step 1: Write the failing tests**

Create `server/tests/test_api.py`:

```python
from fastapi.testclient import TestClient

from server.main import create_app


class FakeService:
    def __init__(self, payload=None):
        self.payload = payload or {"gcode_state": "IDLE", "connection": "ok"}

    def summary(self):
        return dict(self.payload)


def make_frame(runs_dir, run="20260716T000000_x", layer=7):
    frames = runs_dir / run / "frames"
    frames.mkdir(parents=True, exist_ok=True)
    (frames / f"layer_{layer:04d}.jpg").write_bytes(b"\xff\xd8\xff\xe0fake")


def client(tmp_path, payload=None):
    return TestClient(create_app(FakeService(payload), tmp_path))


def test_status_returns_summary(tmp_path):
    r = client(tmp_path).get("/api/status")
    assert r.status_code == 200
    assert r.json()["gcode_state"] == "IDLE"


def test_frame_404_when_no_run(tmp_path):
    r = client(tmp_path).get("/api/frame/latest")
    assert r.status_code == 404
    assert r.json() == {"error": "no active run"}


def test_frame_served_with_headers(tmp_path):
    make_frame(tmp_path, layer=7)
    r = client(tmp_path).get("/api/frame/latest")
    assert r.status_code == 200
    assert r.headers["content-type"] == "image/jpeg"
    assert r.headers["x-frame-layer"] == "7"
    assert r.headers["x-frame-run"] == "20260716T000000_x"
    assert r.headers["cache-control"] == "no-store"


def test_ws_sends_summary_immediately(tmp_path):
    with client(tmp_path).websocket_connect("/ws") as ws:
        msg = ws.receive_json()
        assert msg["gcode_state"] == "IDLE"


def test_root_hint_when_no_dist(tmp_path):
    r = client(tmp_path).get("/")
    assert r.status_code == 200
    assert "npm run build" in r.text
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest server/tests/test_api.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'server.main'`

- [ ] **Step 3: Implement `server/main.py`**

```python
"""FastAPI app: /api/status, /api/frame/latest, /ws, static frontend."""
from __future__ import annotations

import asyncio
import json
import logging
import pathlib
import time

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, JSONResponse, PlainTextResponse
from fastapi.staticfiles import StaticFiles

from . import runs

log = logging.getLogger("server.main")

WS_POLL_S = 0.25      # summary sampled at 4 Hz -> at most ~4 pushes/s
WS_HEARTBEAT_S = 5.0  # push even when unchanged, keeps report_age_s fresh


def create_app(service, runs_dir: pathlib.Path,
               frontend_dist: pathlib.Path | None = None) -> FastAPI:
    """`service` is anything with a summary() -> dict (PrinterService,
    MockPrinter, or a test fake)."""
    app = FastAPI(title="bambu-monitor")

    @app.get("/api/status")
    def status():
        return service.summary()

    @app.get("/api/frame/latest")
    def frame_latest():
        info = runs.newest_frame(runs_dir)
        if info is None:
            return JSONResponse({"error": "no active run"}, status_code=404)
        return FileResponse(
            info["path"], media_type="image/jpeg",
            headers={"X-Frame-Layer": str(info["layer"]),
                     "X-Frame-Run": info["run"],
                     "Cache-Control": "no-store"})

    @app.websocket("/ws")
    async def ws(sock: WebSocket):
        await sock.accept()
        payload = service.summary()
        await sock.send_text(json.dumps(payload))
        last_sent, last_time = payload, time.monotonic()
        try:
            while True:
                await asyncio.sleep(WS_POLL_S)
                now = time.monotonic()
                payload = service.summary()
                # report_age_s ticks every sample; ignore it when deciding
                # whether the state meaningfully changed.
                changed = ({k: v for k, v in payload.items()
                            if k != "report_age_s"}
                           != {k: v for k, v in last_sent.items()
                               if k != "report_age_s"})
                if changed or now - last_time >= WS_HEARTBEAT_S:
                    await sock.send_text(json.dumps(payload))
                    last_sent, last_time = payload, now
        except WebSocketDisconnect:
            pass

    if frontend_dist is not None and (frontend_dist / "index.html").exists():
        app.mount("/", StaticFiles(directory=frontend_dist, html=True),
                  name="static")
    else:
        @app.get("/")
        def hint():
            return PlainTextResponse(
                "bambu-monitor server is running.\n"
                "Frontend not built yet: run `npm run build` in frontend/,\n"
                "or use the Vite dev server (`npm run dev`) on port 5173.\n")

    return app
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest server/tests -v`
Expected: 18 passed

- [ ] **Step 5: Commit**

```bash
git add server/main.py server/tests/test_api.py
git commit -m "feat(server): FastAPI app with status, frame, websocket, static routes"
```

---

### Task 6: `server/__main__.py` — CLI entry + mock smoke test

**Files:**
- Create: `server/__main__.py`

- [ ] **Step 1: Implement `server/__main__.py`**

```python
"""CLI entry: python -m server [--mock | --host ... --serial ... --access-code ...]"""
from __future__ import annotations

import argparse
import logging
import pathlib

import uvicorn

from .main import create_app
from .printer import MockPrinter, PrinterService


def main() -> int:
    p = argparse.ArgumentParser(
        prog="python -m server",
        description="Dashboard backend for the bambu_monitor rig.")
    p.add_argument("--host", help="printer IP")
    p.add_argument("--serial", help="printer serial")
    p.add_argument("--access-code", help="8-char LAN access code")
    p.add_argument("--mock", action="store_true",
                   help="no printer: synthesise an endless fake print")
    p.add_argument("--runs-dir", type=pathlib.Path, default=None,
                   help="capture output dir (default runs/, or runs-mock/ "
                        "with --mock)")
    p.add_argument("--port", type=int, default=8000)
    a = p.parse_args()

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s %(message)s",
        datefmt="%H:%M:%S")

    if a.mock:
        runs_dir = a.runs_dir or pathlib.Path("runs-mock")
        service = MockPrinter(runs_dir)
    else:
        missing = [f for f in ("host", "serial", "access_code")
                   if not getattr(a, f)]
        if missing:
            p.error("need --" + ", --".join(
                m.replace("_", "-") for m in missing) + "  (or use --mock)")
        runs_dir = a.runs_dir or pathlib.Path("runs")
        service = PrinterService(a.host, a.serial, a.access_code)

    runs_dir.mkdir(parents=True, exist_ok=True)
    service.start()

    dist = pathlib.Path(__file__).resolve().parent.parent / "frontend" / "dist"
    app = create_app(service, runs_dir, dist)
    try:
        uvicorn.run(app, host="127.0.0.1", port=a.port)
    finally:
        service.stop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
```

- [ ] **Step 2: Smoke-test mock mode**

Start in one terminal (or as a background process): `python -m server --mock`
Expected log: uvicorn banner, `Uvicorn running on http://127.0.0.1:8000`.

Then:

Run: `curl http://127.0.0.1:8000/api/status`
Expected: JSON with `"printer": "MOCK"`, `"connection": "ok"`, and after ~2 s a growing `"layer_num"`.

Run: `curl -o nul -w "%{http_code} %{content_type}" http://127.0.0.1:8000/api/frame/latest`
Expected: `200 image/jpeg` (give the mock a few seconds to write its first frame).

Run: `curl http://127.0.0.1:8000/`
Expected: the "Frontend not built yet" hint text.

Stop the server (Ctrl-C / kill the background process). Verify `runs-mock/` now contains a `*_mock_benchy/frames/` folder with JPEGs.

- [ ] **Step 3: Commit**

```bash
git add server/__main__.py
git commit -m "feat(server): CLI entry with --mock mode"
```

---

### Task 7: Frontend scaffold — Vite, tokens, all CSS

All styling for the entire app is written here, once, so later tasks only compose class names (per FRONTEND-STACK-GUIDE.md: "all real styling lives in styles.css").

**Files:**
- Create: `frontend/package.json`, `frontend/vite.config.js`, `frontend/index.html`, `frontend/src/main.jsx`, `frontend/src/App.jsx` (placeholder), `frontend/src/styles.css`

- [ ] **Step 1: Create `frontend/package.json`**

```json
{
  "name": "bambu-monitor-frontend",
  "private": true,
  "version": "0.1.0",
  "type": "module",
  "scripts": {
    "dev": "vite",
    "build": "vite build",
    "preview": "vite preview"
  },
  "dependencies": {
    "react": "^19.0.0",
    "react-dom": "^19.0.0"
  },
  "devDependencies": {
    "@vitejs/plugin-react": "^4.3.4",
    "vite": "^6.0.0"
  }
}
```

- [ ] **Step 2: Create `frontend/vite.config.js`**

```js
import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

export default defineConfig({
  plugins: [react()],
  server: {
    proxy: {
      "/api": "http://127.0.0.1:8000",
      "/ws": { target: "ws://127.0.0.1:8000", ws: true },
    },
  },
});
```

- [ ] **Step 3: Create `frontend/index.html`**

```html
<!doctype html>
<html lang="en">
  <head>
    <meta charset="UTF-8" />
    <meta name="viewport" content="width=device-width, initial-scale=1.0" />
    <title>bambu monitor</title>
  </head>
  <body>
    <div id="root"></div>
    <script type="module" src="/src/main.jsx"></script>
  </body>
</html>
```

- [ ] **Step 4: Create `frontend/src/main.jsx`**

```jsx
import React from "react";
import { createRoot } from "react-dom/client";
import App from "./App.jsx";
import "./styles.css";

createRoot(document.getElementById("root")).render(
  <React.StrictMode>
    <App />
  </React.StrictMode>
);
```

- [ ] **Step 5: Create placeholder `frontend/src/App.jsx`** (replaced in Task 9)

```jsx
export default function App() {
  return <div className="ui-pageframe">bambu monitor — shell coming in Task 9</div>;
}
```

- [ ] **Step 6: Create `frontend/src/styles.css`** — tokens verbatim from FRONTEND-STACK-GUIDE.md §3.1, then every class the app uses:

```css
/* ================= design tokens — "Slate Daylight" =================
   Verbatim from FRONTEND-STACK-GUIDE.md §3.1. Do not invent new colors;
   status colors are for machine state ONLY. */
:root {
  color-scheme: light;

  /* surfaces */
  --bg: #f6f8fb;
  --surface: #ffffff;
  --surface-2: #eceff4;
  --line: #d6dce6;
  --line-strong: #cdd5e0;

  /* text */
  --text: #2b3340;
  --text-body: #33404f;
  --text-muted: #5d6b7d;
  --text-faint: #8b97a8;

  /* one hero color for ALL primary interaction */
  --primary: #4a5d8a;
  --primary-hover: #3f5179;
  --primary-soft: #e9edf4;
  --focus: rgba(74, 93, 138, 0.35);
  --on-primary: #ffffff;

  /* machine-state colors — status ONLY, never decoration */
  --ok-text: #3d7256;   --ok-bg: #dde9e2;   --ok-dot: #4f9e76;
  --warn-text: #8a6420; --warn-bg: #f3e6cf; --warn-dot: #c8922f;
  --danger-text: #b23b34; --danger-bg: #f5dedc; --danger-dot: #c0564f;

  /* spacing / radius / control scale */
  --sp-1: 4px;  --sp-2: 8px;  --sp-3: 12px; --sp-4: 16px;
  --sp-5: 20px; --sp-6: 24px; --sp-7: 32px;
  --r-control: 6px; --r-card: 10px; --r-pill: 999px;
  --ctl-h: 36px; --ctl-h-sm: 30px;
  --shadow-sm: 0 1px 2px rgba(43, 51, 64, 0.06);
  --shadow-md: 0 12px 30px rgba(43, 51, 64, 0.08);
}

/* ================= base ================= */
* { box-sizing: border-box; }
html, body, #root { height: 100%; }
body {
  margin: 0;
  background: var(--bg);
  color: var(--text-body);
  font-family: Inter, ui-sans-serif, system-ui, -apple-system,
    BlinkMacSystemFont, "Segoe UI", sans-serif;
  font-size: 14px;
}
a { color: var(--primary); }

/* ================= app shell ================= */
.shell { display: grid; grid-template-columns: 260px 1fr; height: 100%; }

.sidebar {
  background: #111821;
  color: #cbd5e1;
  display: flex;
  flex-direction: column;
  gap: var(--sp-6);
  padding: var(--sp-4);
}
.sidebar__brand {
  color: #ffffff;
  font-weight: 600;
  font-size: 15px;
  letter-spacing: 0.3px;
  padding: var(--sp-2);
}

.main { display: flex; flex-direction: column; min-width: 0; overflow: auto; }

.topbar {
  position: sticky;
  top: 0;
  z-index: 10;
  flex: 0 0 auto;
  height: 56px;
  display: flex;
  align-items: center;
  gap: var(--sp-4);
  padding: 0 var(--sp-6);
  background: var(--surface);
  border-bottom: 1px solid var(--line);
}
.topbar__title { font-weight: 600; color: var(--text); margin-right: auto; }
.topbar__host { color: var(--text-muted); font-size: 12px; }

.dimmed { opacity: 0.55; }

/* ================= ui kit ================= */
/* nav */
.ui-navgroup { display: flex; flex-direction: column; gap: var(--sp-1); }
.ui-navgroup__label {
  font-size: 11px;
  text-transform: uppercase;
  letter-spacing: 0.08em;
  color: #7c8796;
  padding: var(--sp-1) var(--sp-2);
}
.ui-navgroup__item {
  display: block;
  width: 100%;
  text-align: left;
  border: 0;
  background: transparent;
  color: #cbd5e1;
  padding: var(--sp-2) var(--sp-3);
  border-radius: var(--r-control);
  font: inherit;
  cursor: pointer;
}
.ui-navgroup__item:hover { background: rgba(255, 255, 255, 0.06); }
.ui-navgroup__item--active { background: var(--primary); color: var(--on-primary); }
.ui-navgroup__item:focus-visible { outline: 3px solid var(--focus); outline-offset: 1px; }

/* page frame */
.ui-pageframe {
  width: 100%;
  max-width: 1200px;
  margin: 0 auto;
  padding: var(--sp-6);
  display: flex;
  flex-direction: column;
  gap: var(--sp-6);
}

/* section */
.ui-section { display: flex; flex-direction: column; gap: var(--sp-4); }
.ui-section__title { margin: 0; font-size: 15px; font-weight: 600; color: var(--text); }

/* card */
.ui-card {
  background: var(--surface);
  border: 1px solid var(--line);
  border-radius: var(--r-card);
  box-shadow: var(--shadow-sm);
  padding: var(--sp-5);
}
.ui-card__title {
  margin: 0 0 var(--sp-4);
  font-size: 13px;
  font-weight: 600;
  color: var(--text);
}

/* layout helpers */
.ui-stack { display: flex; flex-direction: column; }
.ui-columns { display: grid; align-items: start; }

/* stat tile */
.ui-stattile {
  background: var(--surface);
  border: 1px solid var(--line);
  border-radius: var(--r-card);
  box-shadow: var(--shadow-sm);
  padding: var(--sp-4);
}
.ui-stattile__label {
  font-size: 11px;
  text-transform: uppercase;
  letter-spacing: 0.06em;
  color: var(--text-faint);
  margin-bottom: var(--sp-1);
}
.ui-stattile__value {
  font-size: 20px;
  font-weight: 600;
  color: var(--text);
  font-variant-numeric: tabular-nums;
}
.ui-stattile__sub { font-size: 12px; color: var(--text-muted); }

/* status pill — the ONLY place status colors appear */
.ui-pill {
  display: inline-flex;
  align-items: center;
  gap: 6px;
  height: 24px;
  padding: 0 10px;
  border-radius: var(--r-pill);
  font-size: 12px;
  font-weight: 500;
  white-space: nowrap;
}
.ui-pill__dot { width: 8px; height: 8px; border-radius: 50%; flex: 0 0 auto; }
.ui-pill--ok { background: var(--ok-bg); color: var(--ok-text); }
.ui-pill--ok .ui-pill__dot { background: var(--ok-dot); }
.ui-pill--warn { background: var(--warn-bg); color: var(--warn-text); }
.ui-pill--warn .ui-pill__dot { background: var(--warn-dot); }
.ui-pill--danger { background: var(--danger-bg); color: var(--danger-text); }
.ui-pill--danger .ui-pill__dot { background: var(--danger-dot); }

/* button */
.ui-btn {
  height: var(--ctl-h);
  padding: 0 var(--sp-4);
  border-radius: var(--r-control);
  border: 1px solid transparent;
  font: inherit;
  font-weight: 500;
  cursor: pointer;
}
.ui-btn--sm { height: var(--ctl-h-sm); padding: 0 var(--sp-3); }
.ui-btn--primary { background: var(--primary); color: var(--on-primary); }
.ui-btn--primary:hover { background: var(--primary-hover); }
.ui-btn--secondary {
  background: var(--surface);
  border-color: var(--line-strong);
  color: var(--text);
}
.ui-btn--secondary:hover { background: var(--surface-2); }
.ui-btn--ghost { background: transparent; color: var(--primary); }
.ui-btn--ghost:hover { background: var(--primary-soft); }
.ui-btn--danger { background: var(--danger-text); color: var(--on-primary); }
.ui-btn:focus-visible { outline: 3px solid var(--focus); outline-offset: 1px; }
.ui-btn:disabled { opacity: 0.55; cursor: default; }

/* ================= dashboard ================= */
.tile-row {
  display: grid;
  grid-template-columns: repeat(6, 1fr);
  gap: var(--sp-4);
}
@media (max-width: 1100px) {
  .tile-row { grid-template-columns: repeat(3, 1fr); }
}

.camera-frame {
  display: block;
  width: 100%;
  border: 1px solid var(--line);
  border-radius: var(--r-control);
  background: var(--surface-2);
}
.camera-placeholder {
  aspect-ratio: 4 / 3;
  display: flex;
  align-items: center;
  justify-content: center;
  text-align: center;
  padding: var(--sp-5);
  color: var(--text-faint);
  background: var(--surface-2);
  border: 1px dashed var(--line-strong);
  border-radius: var(--r-control);
}
.camera-caption { margin-top: var(--sp-2); font-size: 12px; color: var(--text-muted); }

.kv {
  display: grid;
  grid-template-columns: 140px 1fr;
  row-gap: var(--sp-2);
  margin: 0;
  font-size: 13px;
}
.kv dt { color: var(--text-muted); }
.kv dd { margin: 0; color: var(--text); }
```

- [ ] **Step 7: Install and verify the dev server boots**

Run (in `frontend/`): `npm install`
Expected: exits 0, `node_modules/` created.

Run (in `frontend/`): `npm run build`
Expected: `vite build` succeeds, `dist/` created. (Building the placeholder proves the toolchain before any real UI exists.)

- [ ] **Step 8: Commit**

```bash
git add frontend/package.json frontend/package-lock.json frontend/vite.config.js frontend/index.html frontend/src
git commit -m "feat(frontend): Vite+React scaffold with full Slate Daylight stylesheet"
```

---

### Task 8: The ui kit (9 components)

Each component only composes class names from `styles.css` onto semantic HTML — no styling in JSX beyond token-driven `gap`/grid templates (which are data, not style).

**Files:**
- Create: `frontend/src/components/ui/Button.jsx`, `Card.jsx`, `Section.jsx`, `PageFrame.jsx`, `Stack.jsx`, `Columns.jsx`, `StatTile.jsx`, `StatusPill.jsx`, `NavGroup.jsx`

- [ ] **Step 1: Create `frontend/src/components/ui/Button.jsx`**

```jsx
export default function Button({ variant = "secondary", size = "md",
                                 busy = false, children, ...rest }) {
  const cls = ["ui-btn", `ui-btn--${variant}`];
  if (size === "sm") cls.push("ui-btn--sm");
  return (
    <button className={cls.join(" ")} disabled={busy || rest.disabled} {...rest}>
      {busy ? "…" : children}
    </button>
  );
}
```

- [ ] **Step 2: Create `frontend/src/components/ui/Card.jsx`**

```jsx
export default function Card({ title, children, className = "" }) {
  return (
    <div className={`ui-card ${className}`.trim()}>
      {title && <h3 className="ui-card__title">{title}</h3>}
      {children}
    </div>
  );
}
```

- [ ] **Step 3: Create `frontend/src/components/ui/Section.jsx`**

```jsx
export default function Section({ title, children }) {
  return (
    <section className="ui-section">
      {title && <h2 className="ui-section__title">{title}</h2>}
      {children}
    </section>
  );
}
```

- [ ] **Step 4: Create `frontend/src/components/ui/PageFrame.jsx`**

```jsx
export default function PageFrame({ children }) {
  return <div className="ui-pageframe">{children}</div>;
}
```

- [ ] **Step 5: Create `frontend/src/components/ui/Stack.jsx`**

```jsx
export default function Stack({ gap = 4, children, className = "" }) {
  return (
    <div className={`ui-stack ${className}`.trim()}
         style={{ gap: `var(--sp-${gap})` }}>
      {children}
    </div>
  );
}
```

- [ ] **Step 6: Create `frontend/src/components/ui/Columns.jsx`**

```jsx
export default function Columns({ template = "1fr 1fr", gap = 5, children }) {
  return (
    <div className="ui-columns"
         style={{ gridTemplateColumns: template, gap: `var(--sp-${gap})` }}>
      {children}
    </div>
  );
}
```

- [ ] **Step 7: Create `frontend/src/components/ui/StatTile.jsx`**

```jsx
export default function StatTile({ label, value, sub }) {
  return (
    <div className="ui-stattile">
      <div className="ui-stattile__label">{label}</div>
      <div className="ui-stattile__value">{value ?? "—"}</div>
      {sub && <div className="ui-stattile__sub">{sub}</div>}
    </div>
  );
}
```

- [ ] **Step 8: Create `frontend/src/components/ui/StatusPill.jsx`**

```jsx
export default function StatusPill({ status = "ok", children }) {
  return (
    <span className={`ui-pill ui-pill--${status}`}>
      <span className="ui-pill__dot" />
      {children}
    </span>
  );
}
```

- [ ] **Step 9: Create `frontend/src/components/ui/NavGroup.jsx`**

```jsx
export default function NavGroup({ label, items, activeKey, onSelect }) {
  return (
    <nav className="ui-navgroup">
      <div className="ui-navgroup__label">{label}</div>
      {items.map(({ key, title }) => (
        <button
          key={key}
          className={`ui-navgroup__item${key === activeKey ? " ui-navgroup__item--active" : ""}`}
          onClick={() => onSelect(key)}
        >
          {title}
        </button>
      ))}
    </nav>
  );
}
```

- [ ] **Step 10: Verify it still builds**

Run (in `frontend/`): `npm run build`
Expected: succeeds. Note: since nothing imports these components yet, the build only proves the scaffold is intact — the components themselves are exercised (and any syntax error surfaces) in Task 9 Step 6, when the shell imports them.

- [ ] **Step 11: Commit**

```bash
git add frontend/src/components/ui
git commit -m "feat(frontend): hand-rolled ui kit (9 primitives, styles in styles.css)"
```

---

### Task 9: Data layer + app shell

**Files:**
- Create: `frontend/src/api/printer.js`, `frontend/src/hooks/usePrinter.js`, `frontend/src/app/pageRegistry.jsx`, `frontend/src/pages/Dashboard.jsx` (stub, completed in Task 10)
- Modify: `frontend/src/App.jsx` (replace placeholder entirely)

- [ ] **Step 1: Create `frontend/src/api/printer.js`**

```js
// Fetch wrappers for the dashboard backend. WebSocket lives in usePrinter.

export async function fetchStatus() {
  const res = await fetch("/api/status");
  if (!res.ok) throw new Error(`status ${res.status}`);
  return res.json();
}

// Returns { url, layer, run } (url is an object URL the caller must revoke)
// or null when there is no active run (HTTP 404) or on network error.
export async function fetchLatestFrame() {
  let res;
  try {
    res = await fetch(`/api/frame/latest?t=${Date.now()}`, { cache: "no-store" });
  } catch {
    return null;
  }
  if (!res.ok) return null;
  const blob = await res.blob();
  return {
    url: URL.createObjectURL(blob),
    layer: res.headers.get("X-Frame-Layer"),
    run: res.headers.get("X-Frame-Run"),
  };
}
```

- [ ] **Step 2: Create `frontend/src/hooks/usePrinter.js`**

```js
import { useEffect, useState } from "react";

const MAX_BACKOFF_MS = 10000;

// Live printer summary over /ws with auto-reconnect.
// Returns { summary, wsUp }: summary is the last received payload (or null),
// wsUp is whether the socket is currently open.
export function usePrinter() {
  const [summary, setSummary] = useState(null);
  const [wsUp, setWsUp] = useState(false);

  useEffect(() => {
    let ws = null;
    let timer = null;
    let alive = true;
    let delay = 1000;

    const connect = () => {
      const proto = window.location.protocol === "https:" ? "wss" : "ws";
      ws = new WebSocket(`${proto}://${window.location.host}/ws`);
      ws.onopen = () => {
        setWsUp(true);
        delay = 1000;
      };
      ws.onmessage = (e) => setSummary(JSON.parse(e.data));
      ws.onclose = () => {
        setWsUp(false);
        if (alive) {
          timer = setTimeout(connect, delay);
          delay = Math.min(delay * 2, MAX_BACKOFF_MS);
        }
      };
    };

    connect();
    return () => {
      alive = false;
      clearTimeout(timer);
      if (ws) ws.close();
    };
  }, []);

  return { summary, wsUp };
}
```

- [ ] **Step 3: Create `frontend/src/pages/Dashboard.jsx` as a stub** (real page in Task 10)

```jsx
import PageFrame from "../components/ui/PageFrame.jsx";

export default function Dashboard({ summary }) {
  return (
    <PageFrame>
      <pre>{JSON.stringify(summary, null, 2)}</pre>
    </PageFrame>
  );
}
```

- [ ] **Step 4: Create `frontend/src/app/pageRegistry.jsx`**

```jsx
import Dashboard from "../pages/Dashboard.jsx";

// Every page: key -> { title, group, component }. The sidebar and topbar
// are derived from this — add future pages (runs browser, print control)
// here and nowhere else.
export const pages = {
  dashboard: { title: "Dashboard", group: "Monitor", component: Dashboard },
};

export function navGroups() {
  const groups = {};
  for (const [key, page] of Object.entries(pages)) {
    (groups[page.group] ??= []).push({ key, title: page.title });
  }
  return groups;
}
```

- [ ] **Step 5: Replace `frontend/src/App.jsx` entirely**

```jsx
import { useState } from "react";
import { navGroups, pages } from "./app/pageRegistry.jsx";
import NavGroup from "./components/ui/NavGroup.jsx";
import StatusPill from "./components/ui/StatusPill.jsx";
import { usePrinter } from "./hooks/usePrinter.js";

const CONN = {
  ok: { status: "ok", label: "Connected" },
  stale: { status: "warn", label: "Stale" },
  disconnected: { status: "danger", label: "Printer offline" },
};
const SERVER_DOWN = { status: "danger", label: "Server offline" };

export default function App() {
  const [active, setActive] = useState("dashboard");
  const { summary, wsUp } = usePrinter();

  const Page = pages[active].component;
  const conn = wsUp ? (CONN[summary?.connection] ?? CONN.stale) : SERVER_DOWN;

  return (
    <div className="shell">
      <aside className="sidebar">
        <div className="sidebar__brand">bambu monitor</div>
        {Object.entries(navGroups()).map(([label, items]) => (
          <NavGroup key={label} label={label} items={items}
                    activeKey={active} onSelect={setActive} />
        ))}
      </aside>
      <div className="main">
        <header className="topbar">
          <span className="topbar__title">{pages[active].title}</span>
          <span className="topbar__host">{summary?.printer ?? ""}</span>
          <StatusPill status={conn.status}>{conn.label}</StatusPill>
        </header>
        <div className={conn.status === "danger" ? "dimmed" : ""}>
          <Page summary={summary} />
        </div>
      </div>
    </div>
  );
}
```

- [ ] **Step 6: Verify against the mock server**

Terminal 1: `python -m server --mock` (from repo root)
Terminal 2 (in `frontend/`): `npm run dev`

Open `http://localhost:5173`. Expected:
- dark sidebar with "Monitor → Dashboard", Dashboard highlighted in `--primary`
- topbar shows "Dashboard", "MOCK", and a green "Connected" pill
- the JSON stub updates (layer_num climbing every ~2 s)
- kill the python server → pill flips to red "Server offline", page dims, JSON freezes
- restart the python server → within ~10 s the pill returns to green (auto-reconnect)

- [ ] **Step 7: Commit**

```bash
git add frontend/src
git commit -m "feat(frontend): app shell, page registry, websocket hook with reconnect"
```

---

### Task 10: Dashboard page + camera/info/HMS cards

**Files:**
- Create: `frontend/src/components/dashboard/CameraCard.jsx`, `PrintInfoCard.jsx`, `HmsCard.jsx`
- Modify: `frontend/src/pages/Dashboard.jsx` (replace stub entirely)

- [ ] **Step 1: Create `frontend/src/components/dashboard/CameraCard.jsx`**

```jsx
import { useEffect, useState } from "react";
import { fetchLatestFrame } from "../../api/printer.js";
import Card from "../ui/Card.jsx";

const POLL_MS = 2000;

export default function CameraCard() {
  const [frame, setFrame] = useState(null);

  useEffect(() => {
    let alive = true;
    let currentUrl = null;

    const tick = async () => {
      const f = await fetchLatestFrame();
      if (!alive) {
        if (f) URL.revokeObjectURL(f.url);
        return;
      }
      if (currentUrl) URL.revokeObjectURL(currentUrl);
      currentUrl = f ? f.url : null;
      setFrame(f); // null -> placeholder (no active run)
    };

    tick();
    const id = setInterval(tick, POLL_MS);
    return () => {
      alive = false;
      clearInterval(id);
      if (currentUrl) URL.revokeObjectURL(currentUrl);
    };
  }, []);

  return (
    <Card title="Camera">
      {frame ? (
        <>
          <img className="camera-frame" src={frame.url}
               alt={`Print at layer ${frame.layer}`} />
          <div className="camera-caption">
            Layer {frame.layer} — {frame.run}
          </div>
        </>
      ) : (
        <div className="camera-placeholder">
          No active capture run — start capture.py
        </div>
      )}
    </Card>
  );
}
```

- [ ] **Step 2: Create `frontend/src/components/dashboard/PrintInfoCard.jsx`**

```jsx
import { Fragment } from "react";
import Card from "../ui/Card.jsx";

export default function PrintInfoCard({ summary }) {
  const s = summary ?? {};
  const rows = [
    ["G-code file", s.gcode_file],
    ["Job", s.subtask_name],
    ["Speed", s.spd_lvl != null ? `level ${s.spd_lvl} (${s.spd_mag ?? "?"}%)` : null],
  ];
  if (s.print_error) rows.push(["Print error", String(s.print_error)]);
  if (s.fail_reason) rows.push(["Fail reason", String(s.fail_reason)]);
  return (
    <Card title="Print">
      <dl className="kv">
        {rows.map(([k, v]) => (
          <Fragment key={k}>
            <dt>{k}</dt>
            <dd>{v ?? "—"}</dd>
          </Fragment>
        ))}
      </dl>
    </Card>
  );
}
```

- [ ] **Step 3: Create `frontend/src/components/dashboard/HmsCard.jsx`**

```jsx
import Card from "../ui/Card.jsx";
import Stack from "../ui/Stack.jsx";
import StatusPill from "../ui/StatusPill.jsx";

// General HMS lookup page (same one referenced in bambu_link.py).
const HMS_WIKI =
  "https://wiki.bambulab.com/en/x1/troubleshooting/how-to-enter-hms-code";

export default function HmsCard({ summary }) {
  const codes = summary?.hms ?? [];
  return (
    <Card title="HMS errors">
      {codes.length === 0 ? (
        <div className="ui-stattile__sub">No errors</div>
      ) : (
        <Stack gap={2}>
          {codes.map((code) => (
            <a key={code} href={HMS_WIKI} target="_blank" rel="noreferrer">
              <StatusPill status="danger">{code}</StatusPill>
            </a>
          ))}
        </Stack>
      )}
    </Card>
  );
}
```

- [ ] **Step 4: Replace `frontend/src/pages/Dashboard.jsx` entirely**

```jsx
import CameraCard from "../components/dashboard/CameraCard.jsx";
import HmsCard from "../components/dashboard/HmsCard.jsx";
import PrintInfoCard from "../components/dashboard/PrintInfoCard.jsx";
import Columns from "../components/ui/Columns.jsx";
import PageFrame from "../components/ui/PageFrame.jsx";
import Stack from "../components/ui/Stack.jsx";
import StatTile from "../components/ui/StatTile.jsx";

const deg = (v) => (v == null ? "—" : `${Number(v).toFixed(0)}°`);

export default function Dashboard({ summary }) {
  const s = summary ?? {};
  return (
    <PageFrame>
      <div className="tile-row">
        <StatTile label="State" value={s.gcode_state} />
        <StatTile label="Layer"
                  value={s.layer_num != null
                    ? `${s.layer_num} / ${s.total_layer_num ?? "?"}` : null} />
        <StatTile label="Progress"
                  value={s.mc_percent != null ? `${s.mc_percent}%` : null} />
        <StatTile label="Remaining"
                  value={s.mc_remaining_time != null
                    ? `${s.mc_remaining_time} min` : null} />
        <StatTile label="Nozzle" value={deg(s.nozzle_temper)}
                  sub={`target ${deg(s.nozzle_target_temper)}`} />
        <StatTile label="Bed" value={deg(s.bed_temper)}
                  sub={`target ${deg(s.bed_target_temper)}`} />
      </div>
      <Columns template="3fr 2fr">
        <CameraCard />
        <Stack gap={5}>
          <PrintInfoCard summary={summary} />
          <HmsCard summary={summary} />
        </Stack>
      </Columns>
    </PageFrame>
  );
}
```

- [ ] **Step 5: Verify the full lifecycle against the mock**

With `python -m server --mock` and `npm run dev` both running, open `http://localhost:5173` and watch one full mock cycle (~70 s). Expected, in order:
1. Tiles fill in: State RUNNING, Layer n/30 climbing, Progress %, Remaining min, Nozzle ~220°/target 220°, Bed ~60°/target 60°.
2. CameraCard shows the synthetic print growing, caption "Layer N — <ts>_mock_benchy".
3. Between layers 12–16: HmsCard shows a red pill `0300_0100_0001_0007` linking to the Bambu wiki; it disappears at layer 17.
4. At the end: State FINISH, then IDLE, then a new run starts.
5. Kill the server → pill "Server offline", content dims; restart → recovers by itself.

- [ ] **Step 6: Commit**

```bash
git add frontend/src
git commit -m "feat(frontend): live dashboard page (tiles, camera, print info, HMS)"
```

---

### Task 11: Production build, single-process serving, README

**Files:**
- Modify: `README (1).md` (append a section)
- Create: `frontend/dist/` (build output, gitignored)

- [ ] **Step 1: Build the frontend**

Run (in `frontend/`): `npm run build`
Expected: `dist/index.html` + assets produced.

- [ ] **Step 2: Verify single-process serving**

Run (repo root): `python -m server --mock`
Open `http://127.0.0.1:8000` (note: 8000, not 5173).
Expected: the full dashboard, identical behavior to Task 10 Step 5. This proves the StaticFiles mount and that `/ws` and `/api` work same-origin.

- [ ] **Step 3: Append to `README (1).md`** (do not modify existing content; add at the end)

```markdown
## 4. `server/` + `frontend/` — the dashboard

Live monitoring GUI (state, temps, layer, HMS, newest captured frame).
It never opens the webcam — it serves the newest frame `capture.py` wrote —
so it is always safe to run alongside a capture.

```bash
pip install -r requirements.txt

# once, or after frontend changes:
cd frontend; npm install; npm run build; cd ..

# with the printer:
python -m server --host 192.168.1.42 --serial 0309xxxxxxxx --access-code 12345678

# without any hardware (endless fake print into runs-mock/):
python -m server --mock
```

Then open http://127.0.0.1:8000. Frontend dev loop: `npm run dev` in
`frontend/` (Vite on :5173, proxies to :8000).

Design/spec: `docs/superpowers/specs/2026-07-16-bambu-dashboard-design.md`.
Look & feel follows `FRONTEND-STACK-GUIDE.md` (Slate Daylight tokens).
```

- [ ] **Step 4: Run the entire test suite one last time**

Run: `pytest server/tests -v`
Expected: 18 passed

- [ ] **Step 5: Commit**

```bash
git add "README (1).md" docs/
git commit -m "docs: dashboard usage section, spec and plan documents"
```

---

## Spec exit criterion (verify before calling this done)

From the spec: with `--mock`, the dashboard must show a full fake print lifecycle (IDLE → RUNNING with ticking layers/temps/frames → HMS appearing and clearing → FINISH) and survive killing/restarting the server (reconnect works, pills correct). Task 10 Step 5 and Task 11 Step 2 together cover this. The real-hardware check (one actual print alongside `capture.py`) happens whenever the printer is next available — not a blocker for merging the code.
