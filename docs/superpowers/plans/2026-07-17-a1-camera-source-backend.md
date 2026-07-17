# A1 Mini Camera Source (backend) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Let the detector read frames from the A1 mini's built-in camera (not just a USB webcam), selectable per printer, with the printer's access code handed to the detector process **only via an environment variable** — never argv, logs, or the API.

**Architecture:** `detect.py` gains a `BambuCameraSource` (TCP 6000 → TLS → auth → `[header][JPEG]` frames, the protocol verified against the real printer) behind the same `grab()` interface the detection loop already uses. A per-printer `camera_source` config field flows registry → `detection_target()` → `DetectorSupervisor`, which puts `--source`/`--host` in argv and the access code in the child's `BAMBU_ACCESS_CODE` env. Everything downstream (`status.json`, auto-stop, API/WS) is unchanged.

**Tech Stack:** Python 3, OpenCV (`cv2.imdecode`), stdlib `socket`/`ssl`/`struct`, FastAPI, pytest. Builds directly on the shipped Phase-1 backend.

**Spec:** `docs/superpowers/specs/2026-07-17-failure-detection-autostop-queue-design.md` (see "Camera sources").

**Run tests from the repo root with `python -m pytest`.** Baseline before this plan: **194 passed**.

**Shared contracts (defined once):**
- `camera_source ∈ {"a1", "webcam"}`, default `"a1"`.
- `registry.detection_target()` → `{serial, camera_source, camera_index, conf, host, access_code}` (or `None`).
- `DetectorSupervisor.build_argv(target)` carries `--source` + (`--host` for a1 | `--camera` for webcam) and **never** the access code; `build_env(target)` returns `{**os.environ, "BAMBU_ACCESS_CODE": code}` for a1, else `None`.
- A1 frame protocol: 80-byte auth = `struct.pack("<IIII", 0x40, 0x3000, 0, 0)` + `b"bblp".ljust(32,b"\0")` + `code.encode().ljust(32,b"\0")`; then repeating `[16-byte header][JPEG]` where the header's first LE uint32 is the JPEG byte length.

---

### Task 1: `PrinterConfig.camera_source`

**Files:**
- Modify: `server/store.py`
- Test: `server/tests/test_store.py`

- [ ] **Step 1: Write the failing tests** — append to `server/tests/test_store.py`:

```python
def test_camera_source_defaults_to_a1():
    assert PrinterConfig(serial="S", host="h", access_code="c").camera_source == "a1"


def test_camera_source_round_trip(tmp_path):
    p = tmp_path / "printers.json"
    PrinterStore(p).save([PrinterConfig(serial="S", host="h", access_code="c",
                                        camera_source="webcam")])
    assert PrinterStore(p).load()[0].camera_source == "webcam"


def test_camera_source_invalid_defaults_to_a1(tmp_path):
    p = tmp_path / "printers.json"
    p.write_text(json.dumps([
        {"serial": "S", "host": "h", "access_code": "c", "camera_source": "usb"},
    ]), encoding="utf-8")
    assert PrinterStore(p).load()[0].camera_source == "a1"
```

- [ ] **Step 2: Run to verify it fails**

Run: `python -m pytest server/tests/test_store.py -k camera_source -v`
Expected: FAIL (`TypeError: unexpected keyword argument 'camera_source'`).

- [ ] **Step 3: Implement** — in `server/store.py`:

Add a module constant near `DETECTION_CLASSES`:

```python
CAMERA_SOURCES = ("a1", "webcam")
```

Add the field to `PrinterConfig` (immediately after `capture`, before `camera_index`):

```python
    camera_source: str = "a1"
```

In `from_dict`, before the `return cls(...)`, add tolerant parsing:

```python
        camera_source = d.get("camera_source", "a1")
        if camera_source not in CAMERA_SOURCES:
            camera_source = "a1"
```

and pass it in the constructor call:

```python
        return cls(serial=d["serial"], host=d["host"],
                   access_code=d["access_code"], name=name, capture=capture,
                   camera_source=camera_source, camera_index=camera_index,
                   conf=conf, armed_classes=armed_classes,
                   detect_enabled=detect_enabled)
```

- [ ] **Step 4: Run to verify it passes**

Run: `python -m pytest server/tests/test_store.py -v`
Expected: PASS (all).

- [ ] **Step 5: Commit**

```bash
git add server/store.py server/tests/test_store.py
git commit -m "feat(detection): PrinterConfig gains camera_source (a1|webcam)"
```

---

### Task 2: `BambuCameraSource` in `detect.py`

**Files:**
- Modify: `detect.py` (add imports + `BambuCameraSource` and helpers)
- Test: `server/tests/test_detect.py`

- [ ] **Step 1: Write the failing tests** — append to `server/tests/test_detect.py`:

```python
import struct

import cv2


class FakeSock:
    """A socket-like that hands out a fixed byte buffer and records sends."""
    def __init__(self, data=b""):
        self.buf = data
        self.sent = b""
        self.closed = False

    def settimeout(self, t):
        pass

    def sendall(self, b):
        self.sent += b

    def recv(self, n):
        if not self.buf:
            return b""   # peer closed
        out, self.buf = self.buf[:n], self.buf[n:]
        return out

    def close(self):
        self.closed = True


def _jpeg_bytes():
    ok, buf = cv2.imencode(".jpg", np.zeros((8, 8, 3), np.uint8))
    return buf.tobytes()


def _framed(jpeg):
    return struct.pack("<I", len(jpeg)) + b"\x00" * 12 + jpeg


def test_bambu_source_auths_and_reads_a_frame():
    jpeg = _jpeg_bytes()
    sock = FakeSock(_framed(jpeg))
    src = detect.BambuCameraSource("h", "MYCODE", connect=lambda host, t: sock)
    frame = src.grab()
    assert frame is not None and frame.shape == (8, 8, 3)
    # 80-byte auth: header + bblp + access code
    assert len(sock.sent) == 80
    assert sock.sent[:16] == struct.pack("<IIII", 0x40, 0x3000, 0, 0)
    assert sock.sent[16:20] == b"bblp"
    assert b"MYCODE" in sock.sent


def test_bambu_source_reconnects_once_on_drop():
    jpeg = _jpeg_bytes()
    socks = [FakeSock(b""), FakeSock(_framed(jpeg))]   # first is dead
    calls = []

    def connect(host, t):
        s = socks[len(calls)]
        calls.append(s)
        return s

    src = detect.BambuCameraSource("h", "C", connect=connect)
    assert src.grab() is not None
    assert len(calls) == 2   # reconnected exactly once


def test_bambu_source_returns_none_on_persistent_failure():
    def connect(host, t):
        raise OSError("connection refused")

    src = detect.BambuCameraSource("h", "C", connect=connect)
    assert src.grab() is None   # -> the loop writes an error status
```

- [ ] **Step 2: Run to verify it fails**

Run: `python -m pytest server/tests/test_detect.py -k bambu -v`
Expected: FAIL (`AttributeError: module 'detect' has no attribute 'BambuCameraSource'`).

- [ ] **Step 3: Implement** — in `detect.py`, add to the imports at the top (after the existing stdlib imports):

```python
import logging
import socket
import ssl
import struct
```

and a module logger near the top (after the imports):

```python
log = logging.getLogger("detect")

CAMERA_PORT = 6000
```

Then add the source (place it after `open_camera`):

```python
def _bambu_auth_packet(access_code: str) -> bytes:
    return (struct.pack("<IIII", 0x40, 0x3000, 0, 0)
            + b"bblp".ljust(32, b"\x00")
            + access_code.encode().ljust(32, b"\x00"))


def _default_connect(host: str, timeout: float):
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE   # printer's cert is self-signed, like MQTT
    raw = socket.create_connection((host, CAMERA_PORT), timeout=timeout)
    return ctx.wrap_socket(raw, server_hostname=host)


class BambuCameraSource:
    """Reads JPEG frames from a Bambu P1/A1-family camera (TCP 6000, TLS).

    grab() returns the next frame decoded to BGR, reconnecting once on a drop;
    returns None only if the reconnect also fails (the detection loop then
    writes an error status). The access code is passed in by the caller, NEVER
    read from argv.
    """

    def __init__(self, host: str, access_code: str, *, timeout: float = 8.0,
                 connect=_default_connect):
        self.host = host
        self._code = access_code
        self._timeout = timeout
        self._connect = connect
        self._sock = None

    def _open(self) -> None:
        self._close()
        s = self._connect(self.host, self._timeout)
        s.settimeout(self._timeout)
        s.sendall(_bambu_auth_packet(self._code))
        self._sock = s

    def _recv_exactly(self, n: int) -> bytes:
        buf = b""
        while len(buf) < n:
            chunk = self._sock.recv(n - len(buf))
            if not chunk:
                raise ConnectionError("camera stream closed")
            buf += chunk
        return buf

    def _read_frame(self):
        header = self._recv_exactly(16)
        size = int.from_bytes(header[:4], "little")
        if not (0 < size <= 20_000_000):
            raise ConnectionError(f"implausible frame size {size}")
        jpeg = self._recv_exactly(size)
        return cv2.imdecode(np.frombuffer(jpeg, np.uint8), cv2.IMREAD_COLOR)

    def grab(self):
        for attempt in (1, 2):    # try, then one reconnect
            try:
                if self._sock is None:
                    self._open()
                return self._read_frame()
            except (OSError, ConnectionError) as e:
                log.warning("A1 camera grab failed (attempt %d): %s", attempt, e)
                self._close()
        return None

    def _close(self) -> None:
        if self._sock is not None:
            try:
                self._sock.close()
            except OSError:
                pass
            self._sock = None

    def close(self) -> None:
        self._close()
```

- [ ] **Step 4: Run to verify it passes**

Run: `python -m pytest server/tests/test_detect.py -v`
Expected: PASS. Confirm `import detect` still pulls in no torch (the YOLO import stays lazy).

- [ ] **Step 5: Commit**

```bash
git add detect.py server/tests/test_detect.py
git commit -m "feat(detection): BambuCameraSource reads A1 camera frames (TCP 6000/TLS)"
```

---

### Task 3: `detect.py` `main()` — `--source`/`--host` + env access code

**Files:**
- Modify: `detect.py` (`main()` argparse + source selection)
- Test: manual (source selection is exercised end-to-end in Task 7; the units are covered by Task 2)

- [ ] **Step 1: Implement** — in `detect.py` `main()`:

Add arguments (near the existing `--camera`):

```python
    p.add_argument("--source", choices=("a1", "webcam"), default="a1",
                   help="frame source: the printer's built-in camera or a USB webcam")
    p.add_argument("--host", default=None,
                   help="printer IP, required for --source a1")
```

Replace the non-mock branch's camera setup so it picks the source. The branch currently opens `open_camera` and builds `grab`; make it:

```python
    else:
        if not pathlib.Path(a.weights).exists():
            print(f"weights not found: {a.weights}", file=sys.stderr)
            return 1
        cam = None
        if a.source == "a1":
            if not a.host:
                print("--source a1 requires --host", file=sys.stderr)
                return 1
            code = os.environ.get("BAMBU_ACCESS_CODE")
            if not code:
                print("--source a1 requires BAMBU_ACCESS_CODE in the environment",
                      file=sys.stderr)
                return 1
            cam = BambuCameraSource(a.host, code)
            grab = cam.grab
        else:
            cap = open_camera(a.camera)

            def grab():
                cap.read()                 # flush one stale buffered frame
                ok, frame = cap.read()
                return frame if ok else None

        infer = make_yolo_infer(a.weights, a.conf, a.imgsz, a.device)
        try:
            detection_loop(grab, infer, a.out, camera=a.camera, conf=a.conf,
                           fps=a.fps, stop_event=stop)
        except KeyboardInterrupt:
            pass
        finally:
            if cam is not None:
                cam.close()
            else:
                cap.release()
    return 0
```

(`os` is already imported. Keep the `--mock` branch exactly as it is.)

- [ ] **Step 2: Verify the CLI parses and the suite is green**

Run: `python detect.py --help`
Expected: shows `--source {a1,webcam}` and `--host`.
Run: `python -m pytest -q`
Expected: PASS (no regressions; the units for the source live in Task 2).

- [ ] **Step 3: Commit**

```bash
git add detect.py
git commit -m "feat(detection): detect.py --source a1|webcam + BAMBU_ACCESS_CODE env"
```

---

### Task 4: Registry — carry `camera_source`/`host`/`access_code`

**Files:**
- Modify: `server/registry.py` (`detection_config`, `detection_target`, `update_detection`)
- Test: `server/tests/test_registry.py`

- [ ] **Step 1: Write the failing tests** — append to the detection-accessor tests in `server/tests/test_registry.py`:

```python
def test_detection_target_includes_source_host_and_code():
    reg = _reg(_cfg("B", host="1.2.3.4", access_code="SEKRET", capture=True,
                    detect_enabled=True))
    t = reg.detection_target()
    assert t["camera_source"] == "a1"
    assert t["host"] == "1.2.3.4"
    assert t["access_code"] == "SEKRET"


def test_update_detection_sets_camera_source():
    reg = _reg(_cfg("B", capture=True))
    assert reg.update_detection("B", camera_source="webcam") is True
    assert reg.detection_config("B")["camera_source"] == "webcam"


def test_update_detection_ignores_bad_camera_source():
    reg = _reg(_cfg("B", capture=True, camera_source="a1"))
    reg.update_detection("B", camera_source="usb")
    assert reg.detection_config("B")["camera_source"] == "a1"
```

> Note: the existing `_cfg(...)` helper builds a `PrinterConfig`; it already forwards `**kw`, so `host=`/`access_code=`/`camera_source=` pass through. If your `_cfg` pins `access_code`, adjust it to accept an override.

- [ ] **Step 2: Run to verify it fails**

Run: `python -m pytest server/tests/test_registry.py -k "source or host_and_code" -v`
Expected: FAIL (`KeyError: 'camera_source'` / target lacks the keys).

- [ ] **Step 3: Implement** — in `server/registry.py`, import the sources list and update the three methods:

Change the store import to include `CAMERA_SOURCES`:

```python
from .store import CAMERA_SOURCES, DETECTION_CLASSES, PrinterConfig
```

`detection_config` — add `camera_source`:

```python
            return {"camera_source": cfg.camera_source,
                    "camera_index": cfg.camera_index, "conf": cfg.conf,
                    "armed_classes": list(cfg.armed_classes),
                    "detect_enabled": cfg.detect_enabled}
```

`detection_target` — add source/host/access_code:

```python
    def detection_target(self):
        with self._lock:
            for serial, cfg in self._configs.items():
                if cfg.capture and cfg.detect_enabled:
                    return {"serial": serial, "camera_source": cfg.camera_source,
                            "camera_index": cfg.camera_index, "conf": cfg.conf,
                            "host": cfg.host, "access_code": cfg.access_code}
        return None
```

`update_detection` — accept `camera_source` (add the keyword and the body):

```python
    def update_detection(self, serial, *, camera_source=None, camera_index=None,
                         conf=None, armed_classes=None, detect_enabled=None) -> bool:
        with self._lock:
            cfg = self._configs.get(serial)
            if cfg is None:
                return False
            if camera_source in CAMERA_SOURCES:
                cfg.camera_source = camera_source
            if camera_index is not None:
                cfg.camera_index = max(0, int(camera_index))
            if conf is not None:
                cfg.conf = min(1.0, max(0.0, float(conf)))
            if armed_classes is not None:
                cfg.armed_classes = [c for c in armed_classes
                                     if c in DETECTION_CLASSES] or ["spaghetti"]
            if detect_enabled is not None:
                cfg.detect_enabled = bool(detect_enabled)
        self._persist()
        return True
```

- [ ] **Step 4: Run to verify it passes**

Run: `python -m pytest server/tests/test_registry.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add server/registry.py server/tests/test_registry.py
git commit -m "feat(detection): registry detection_target carries source/host/access_code"
```

---

### Task 5: `DetectorSupervisor` — `--source`/`--host` in argv, code in env

**Files:**
- Modify: `server/detection.py` (`DetectorSupervisor.build_argv`, new `build_env`, `_spawn`)
- Test: `server/tests/test_detection.py`

- [ ] **Step 1: Update the existing fakes + write new tests** — in `server/tests/test_detection.py`:

First, the Task-8 `spawn` fake must accept `env`. Update the `supervisor(...)` helper's spawn function to `def spawn(argv, env=None):` (record `env` on the `FakeProc` if useful), and update the `T1`/`T2` target dicts used by the supervisor tests to the new shape:

```python
T1 = {"serial": "S1", "camera_source": "a1", "camera_index": 0, "conf": 0.25,
      "host": "1.2.3.4", "access_code": "CODE1"}
T2 = {"serial": "S1", "camera_source": "webcam", "camera_index": 2, "conf": 0.4,
      "host": "1.2.3.4", "access_code": "CODE1"}
```

Then append:

```python
def test_build_argv_a1_has_source_host_no_code(tmp_path):
    sup, _ = supervisor(tmp_path, lambda: 0.0)
    argv = sup.build_argv(T1)
    assert "--source" in argv and "a1" in argv
    assert "--host" in argv and "1.2.3.4" in argv
    assert "--camera" not in argv
    assert not any("CODE1" in str(a) for a in argv)   # secret never in argv


def test_build_argv_webcam_has_camera(tmp_path):
    sup, _ = supervisor(tmp_path, lambda: 0.0)
    argv = sup.build_argv(T2)
    assert "--source" in argv and "webcam" in argv
    assert "--camera" in argv and "2" in argv
    assert "--host" not in argv


def test_build_env_carries_code_for_a1_only(tmp_path):
    sup, _ = supervisor(tmp_path, lambda: 0.0)
    a1_env = sup.build_env(T1)
    assert a1_env["BAMBU_ACCESS_CODE"] == "CODE1"
    assert sup.build_env(T2) is None    # webcam inherits the parent env
```

(The existing `test_changed_target_restarts` uses `"2" in spawned[1].argv` — still true, since webcam argv has `--camera 2`.)

- [ ] **Step 2: Run to verify it fails**

Run: `python -m pytest server/tests/test_detection.py -k "build_argv or build_env" -v`
Expected: FAIL (`build_env` missing; a1 argv lacks `--source`/`--host`).

- [ ] **Step 3: Implement** — in `server/detection.py`, add `import os` to the imports, then update `DetectorSupervisor`:

```python
    def build_argv(self, target) -> list:
        # NB: never the access code -- that goes in build_env for a1.
        argv = [self._python, self._script, "--source", target["camera_source"],
                "--conf", str(target["conf"]), "--weights", str(self._weights),
                "--out", str(self._out_dir), "--fps", str(self._fps)]
        if target["camera_source"] == "a1":
            argv += ["--host", target["host"]]
        else:
            argv += ["--camera", str(target["camera_index"])]
        return argv

    def build_env(self, target):
        """a1 needs the access code to auth to the camera -> pass it in the
        child's env, never argv. webcam inherits the parent env (None)."""
        if target["camera_source"] == "a1":
            env = dict(os.environ)
            env["BAMBU_ACCESS_CODE"] = target["access_code"]
            return env
        return None

    def _spawn(self, target) -> None:
        self._proc = self._spawn_fn(self.build_argv(target),
                                    env=self.build_env(target))
        self._last_spawn = self._clock()
```

- [ ] **Step 4: Run to verify it passes**

Run: `python -m pytest server/tests/test_detection.py -v`
Expected: PASS (including the pre-existing supervisor tests with their updated target shape).

- [ ] **Step 5: Commit**

```bash
git add server/detection.py server/tests/test_detection.py
git commit -m "feat(detection): supervisor passes --source/--host in argv, access code in env"
```

---

### Task 6: Surface `camera_source` in the snapshot + PUT

**Files:**
- Modify: `server/detection.py` (`DetectionCoordinator.snapshot`), `server/main.py` (`DetectionUpdate` + PUT validation)
- Test: `server/tests/test_detection.py`, `server/tests/test_api.py`

- [ ] **Step 1: Write the failing tests**

In `server/tests/test_detection.py`, the coordinator's `FakeReg.detection_config` must return `camera_source`; update it to include `"camera_source": "a1"`, then append:

```python
def test_snapshot_includes_camera_source(tmp_path):
    reg = FakeReg({"serial": "S1", "camera_index": 0, "conf": 0.5})
    co = DetectionCoordinator(reg, tmp_path, FakeRunner())
    assert co.snapshot("S1")["camera_source"] == "a1"
```

In `server/tests/test_api.py`, the `FakeDetection.snapshot` must include `"camera_source": "a1"`; update it, then append:

```python
def test_put_detection_accepts_camera_source(tmp_path):
    det = FakeDetection()
    c, reg = det_client(tmp_path, det)
    r = c.put("/api/printers/S1/detection", json={"camera_source": "webcam"})
    assert r.status_code == 200
    assert reg.updated[-1]["camera_source"] == "webcam"


def test_put_detection_rejects_bad_camera_source(tmp_path):
    c, _ = det_client(tmp_path, FakeDetection())
    r = c.put("/api/printers/S1/detection", json={"camera_source": "usb"})
    assert r.status_code == 400
```

- [ ] **Step 2: Run to verify it fails**

Run: `python -m pytest server/tests/test_detection.py server/tests/test_api.py -k "camera_source" -v`
Expected: FAIL (`KeyError: 'camera_source'` in snapshot; PUT accepts `usb`).

- [ ] **Step 3: Implement**

In `server/detection.py`, `DetectionCoordinator.snapshot`, add `camera_source` to the returned dict (it comes from `cfg`):

```python
        return {"running": status["running"], "fps": status["fps"],
                "camera_source": cfg["camera_source"],
                "camera_index": cfg["camera_index"], "conf": cfg["conf"],
                "detect_enabled": cfg["detect_enabled"],
                "armed": snap["armed"], "armed_classes": cfg["armed_classes"],
                "detections": status["detections"] if status["running"] else [],
                "stopped_by_monitor": snap["stopped_by_monitor"],
                "seconds_to_stop": snap["seconds_to_stop"],
                "error": status["error"]}
```

In `server/main.py`, add `camera_source` to `DetectionUpdate` and import the sources list:

```python
from server.detection import CLASSES  # existing
from server.store import CAMERA_SOURCES
```

```python
class DetectionUpdate(BaseModel):
    camera_source: str | None = None
    camera_index: int | None = None
    conf: float | None = None
    armed_classes: list[str] | None = None
    detect_enabled: bool | None = None
```

In `put_detection`, validate `camera_source` before calling the registry (alongside the existing class check):

```python
        if body.camera_source is not None and body.camera_source not in CAMERA_SOURCES:
            raise HTTPException(400, f"camera_source must be one of {CAMERA_SOURCES}")
```

- [ ] **Step 4: Run to verify it passes**

Run: `python -m pytest -q`
Expected: PASS (all).

- [ ] **Step 5: Commit**

```bash
git add server/detection.py server/main.py server/tests/test_detection.py server/tests/test_api.py
git commit -m "feat(detection): expose camera_source in snapshot + validate it on PUT"
```

---

### Task 7: Hardware verification against the real printer (operator-run, non-destructive)

**Files:** none (verification only)

> This task is run by the operator (not a fresh subagent) because it needs the live printer + the access code and human judgment on the frames. It is **read-only** — it opens the camera and runs inference; it does NOT print or stop anything.

- [ ] **Step 1: Confirm the full suite is green**

Run: `python -m pytest -q`
Expected: PASS (~207: 194 baseline + this plan's new tests).

- [ ] **Step 2: Run the real A1 source headless for ~30s** (weights must exist at `runs/train/failure_detector/weights/best.pt`):

```bash
BAMBU_ACCESS_CODE=<code> python detect.py --source a1 --host 192.168.137.108 \
    --out runs/_detect --fps 2
```
(Windows PowerShell: `$env:BAMBU_ACCESS_CODE="<code>"; python detect.py --source a1 --host 192.168.137.108 --out runs/_detect --fps 2`)

Expected: `runs/_detect/status.json` updates with a real `fps` (~0.4) and any detections; `runs/_detect/latest.jpg` is a real annotated A1 frame. Ctrl-C to stop. Confirm no access code appears in the process list or any written file (`grep` the code across `runs/_detect/` — expect nothing).

- [ ] **Step 3: Server end-to-end (non-destructive)** — with the real printer added and marked capture, `PUT {"camera_source":"a1","detect_enabled":true}` then confirm `GET /api/printers` shows `detection.running:true` and a live `latest.jpg` via `/detection/frame`. Do **not** arm here (arming is exercised destructively in the separate, gated stop-verification step).

- [ ] **Step 4: Record the result** in the PR/branch notes: real fps observed, whether YOLO produced sensible boxes on the A1's fisheye view (informs whether the model needs printer-specific retraining later — out of scope here).

---

## Self-Review (completed while writing)

**Spec coverage** (the "Camera sources" revision): `camera_source` config (T1) ✅; A1 frame protocol + reconnect (T2) ✅; `--source`/`--host`/env code (T3) ✅; registry carries source/host/code (T4) ✅; supervisor argv-without-secret + env-with-secret (T5) ✅; snapshot + PUT expose/validate source (T6) ✅; hardware verification now-possible (T7) ✅.

**Secret hygiene:** the access code appears only in `build_env` (T5) and `BambuCameraSource` (T2, passed in), asserted absent from argv (T5) and never added to the snapshot/status. `--mock` uses no real source, so it needs no code.

**Type/name consistency:** `camera_source` string everywhere; `detection_target()` shape `{serial, camera_source, camera_index, conf, host, access_code}` matches `build_argv`/`build_env` (T4↔T5) and the updated supervisor test targets; `CAMERA_SOURCES` is the single source of truth (store.py), imported by registry (T4) and main (T6).

**Placeholder scan:** none — every step has concrete code.

**Not in scope (own plan):** the Detection-page UI (source selector, etc.) is Plan 1b-B; the destructive `stop`-command verification is a separate gated hardware step.
