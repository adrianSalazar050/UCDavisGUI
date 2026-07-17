# Live Failure Detection + Auto-Stop (Backend) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Wire the trained YOLO failure detector into the server as a supervised camera process, and stop the print over MQTT when an *armed* failure class persists for 10 continuous seconds — all headless and fully testable under `--mock`, with no browser required.

**Architecture:** A separate `detect.py` process owns the webcam and writes `runs/_detect/status.json` + `latest.jpg` atomically. A server-side `DetectionCoordinator` (background thread, ~2 Hz) supervises that process for the capture printer, reads its status, runs a pure `AutoStopController` state machine, and calls a new `PrinterService.stop_print()` when the machine fires. Detection state is merged into the capture printer's WebSocket summary and exposed on new `/api/printers/{serial}/detection*` routes. This is the Phase-1 spec's backend half; the Detection/Dashboard UI is a separate follow-on plan.

**Tech Stack:** Python 3, FastAPI, OpenCV, Ultralytics YOLOv8, paho-mqtt, pytest. Frontend untouched here.

**Spec:** `docs/superpowers/specs/2026-07-17-failure-detection-autostop-queue-design.md`

**Run tests from the repo root with `python -m pytest`** (the `-m` puts the repo root on `sys.path`, so root modules `detect` and `bambu_link` import cleanly).

**Shared contracts (defined once, referenced by many tasks):**

- `status.json` payload: `{"ts": float, "fps": float, "camera": int, "conf": float, "detections": [{"cls": str, "conf": float, "box": [x,y,w,h]}], "error": str|null}`
- `CLASSES = ("blobs","cracks","over_extrusion","spaghetti","stringing","under_extrusion")` — the 6 model classes; the only accepted `armed_classes`.
- `AutoStopController.update(detections, gcode_state) -> "fire" | None` — pure; the caller actuates on `"fire"`.
- `registry.detection_target() -> {"serial","camera_index","conf"} | None` — the capture printer *iff* `detect_enabled`.
- Detection summary object: `{running, fps, camera_index, conf, detect_enabled, armed, armed_classes, detections, stopped_by_monitor, seconds_to_stop, error}`.

---

### Task 1: `PrinterConfig` detection fields

**Files:**
- Modify: `server/store.py` (dataclass + `from_dict`)
- Test: `server/tests/test_store.py`

- [ ] **Step 1: Write the failing tests** — append to `server/tests/test_store.py`:

```python
def test_detection_fields_default(tmp_path):
    c = PrinterConfig(serial="S1", host="1.2.3.4", access_code="c")
    assert c.camera_index == 0
    assert c.conf == 0.25
    assert c.armed_classes == ["spaghetti"]
    assert c.detect_enabled is False


def test_detection_fields_round_trip(tmp_path):
    p = tmp_path / "printers.json"
    store = PrinterStore(p)
    store.save([PrinterConfig(serial="S1", host="1.2.3.4", access_code="c",
                              camera_index=2, conf=0.4,
                              armed_classes=["spaghetti", "cracks"],
                              detect_enabled=True)])
    got = store.load()[0]
    assert got.camera_index == 2
    assert got.conf == 0.4
    assert got.armed_classes == ["spaghetti", "cracks"]
    assert got.detect_enabled is True


def test_detection_defaults_are_independent_lists():
    # A dataclass mutable default MUST use default_factory, or every config
    # shares one list and appending to one printer's classes mutates them all.
    a = PrinterConfig(serial="A", host="h", access_code="c")
    b = PrinterConfig(serial="B", host="h", access_code="c")
    a.armed_classes.append("cracks")
    assert b.armed_classes == ["spaghetti"]


def test_detect_enabled_wrong_type_defaults_false(tmp_path):
    p = tmp_path / "printers.json"
    p.write_text(json.dumps([
        {"serial": "S1", "host": "1.2.3.4", "access_code": "c",
         "detect_enabled": "true"},
    ]), encoding="utf-8")
    got = PrinterStore(p).load()
    assert got[0].detect_enabled is False


def test_armed_classes_wrong_type_defaults_to_spaghetti(tmp_path):
    p = tmp_path / "printers.json"
    p.write_text(json.dumps([
        {"serial": "S1", "host": "1.2.3.4", "access_code": "c",
         "armed_classes": "spaghetti"},   # a string, not a list
    ]), encoding="utf-8")
    got = PrinterStore(p).load()
    assert got[0].armed_classes == ["spaghetti"]


def test_unknown_armed_class_is_dropped(tmp_path):
    p = tmp_path / "printers.json"
    p.write_text(json.dumps([
        {"serial": "S1", "host": "1.2.3.4", "access_code": "c",
         "armed_classes": ["spaghetti", "banana"]},
    ]), encoding="utf-8")
    got = PrinterStore(p).load()
    assert got[0].armed_classes == ["spaghetti"]
```

- [ ] **Step 2: Run to verify it fails**

Run: `python -m pytest server/tests/test_store.py -k detection -v`
Expected: FAIL (`TypeError: unexpected keyword argument 'camera_index'`).

- [ ] **Step 3: Implement** — in `server/store.py`, add `import dataclasses` is already present; add a module constant and fields:

```python
# The 6 classes the failure detector emits (FAILURE_DETECTOR_REPORT.md). The
# only values accepted for armed_classes; anything else is dropped.
DETECTION_CLASSES = ("blobs", "cracks", "over_extrusion", "spaghetti",
                     "stringing", "under_extrusion")
```

In the `PrinterConfig` dataclass, add fields after `capture`:

```python
    camera_index: int = 0
    conf: float = 0.25
    armed_classes: list = dataclasses.field(
        default_factory=lambda: ["spaghetti"])
    detect_enabled: bool = False
```

In `from_dict`, before the final `return cls(...)`, add tolerant parsing:

```python
        camera_index = d.get("camera_index", 0)
        if not isinstance(camera_index, int) or isinstance(camera_index, bool):
            camera_index = 0
        conf = d.get("conf", 0.25)
        if not isinstance(conf, (int, float)) or isinstance(conf, bool):
            conf = 0.25
        conf = min(1.0, max(0.0, float(conf)))
        detect_enabled = d.get("detect_enabled", False)
        if not isinstance(detect_enabled, bool):
            detect_enabled = False
        raw_classes = d.get("armed_classes", ["spaghetti"])
        if not isinstance(raw_classes, list):
            raw_classes = ["spaghetti"]
        armed_classes = [c for c in raw_classes if c in DETECTION_CLASSES] \
            or ["spaghetti"]
```

and extend the constructor call:

```python
        return cls(serial=d["serial"], host=d["host"],
                   access_code=d["access_code"], name=name, capture=capture,
                   camera_index=camera_index, conf=conf,
                   armed_classes=armed_classes, detect_enabled=detect_enabled)
```

- [ ] **Step 4: Run to verify it passes**

Run: `python -m pytest server/tests/test_store.py -v`
Expected: PASS (all, including the pre-existing tests).

- [ ] **Step 5: Commit**

```bash
git add server/store.py server/tests/test_store.py
git commit -m "feat(detection): PrinterConfig gains camera_index/conf/armed_classes/detect_enabled"
```

---

### Task 2: `BambuLink.stop_print()`

**Files:**
- Modify: `bambu_link.py` (new method next to `send_gcode`)
- Test: `server/tests/test_bambu_link.py` (new)

- [ ] **Step 1: Write the failing test** — create `server/tests/test_bambu_link.py`:

```python
import bambu_link


class FakeClient:
    """Captures publishes instead of touching a socket."""
    def __init__(self, *a, **k):
        self.published = []
    def username_pw_set(self, *a, **k): pass
    def tls_set(self, *a, **k): pass
    def tls_insecure_set(self, *a, **k): pass
    def publish(self, topic, payload): self.published.append((topic, payload))


def link(monkeypatch):
    monkeypatch.setattr(bambu_link.mqtt, "Client",
                        lambda *a, **k: FakeClient())
    return bambu_link.BambuLink("h", "SER", "code")


def test_stop_print_publishes_stop_command(monkeypatch):
    import json
    lk = link(monkeypatch)
    lk.stop_print()
    topic, payload = lk.client.published[-1]
    assert topic == "device/SER/request"
    body = json.loads(payload)
    assert body["print"]["command"] == "stop"
    assert "sequence_id" in body["print"]


def test_stop_print_never_carries_the_access_code(monkeypatch):
    lk = link(monkeypatch)
    lk.stop_print()
    _, payload = lk.client.published[-1]
    assert "code" not in payload
```

- [ ] **Step 2: Run to verify it fails**

Run: `python -m pytest server/tests/test_bambu_link.py -v`
Expected: FAIL (`AttributeError: 'BambuLink' object has no attribute 'stop_print'`).

- [ ] **Step 3: Implement** — in `bambu_link.py`, add after `send_gcode`:

```python
    def stop_print(self) -> None:
        """Stop the running print. This is a Bambu *print command*, not
        G-code. Like send_gcode there is NO ack -- the caller must confirm the
        stop took by watching gcode_state (the AutoStopController does this and
        re-sends once). UNVERIFIED against real A1 mini hardware."""
        self._publish({"print": {"sequence_id": self._next_seq(),
                                 "command": "stop"}})
```

- [ ] **Step 4: Run to verify it passes**

Run: `python -m pytest server/tests/test_bambu_link.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add bambu_link.py server/tests/test_bambu_link.py
git commit -m "feat(detection): BambuLink.stop_print() publishes the MQTT stop command"
```

---

### Task 3: `stop_print()` on the services

**Files:**
- Modify: `server/printer.py` (`PrinterService.stop_print`, `MockPrinter.stop_print`)
- Test: `server/tests/test_services.py`

- [ ] **Step 1: Write the failing tests** — append to `server/tests/test_services.py`:

```python
def test_service_stop_print_delegates_to_link(monkeypatch):
    s = svc()
    called = []
    monkeypatch.setattr(s.link, "stop_print", lambda: called.append(True))
    s.stop_print()
    assert called == [True]


def test_mock_stop_print_marks_failed(tmp_path):
    mp = MockPrinter(tmp_path)
    mp._touch({"gcode_state": "RUNNING", "layer_num": 3})
    mp.stop_print()
    assert mp.summary()["gcode_state"] == "FAILED"
```

- [ ] **Step 2: Run to verify it fails**

Run: `python -m pytest server/tests/test_services.py -k stop_print -v`
Expected: FAIL (`AttributeError: ... 'stop_print'`).

- [ ] **Step 3: Implement** — in `server/printer.py`:

In `PrinterService`, add:

```python
    def stop_print(self) -> None:
        """Fire-and-verify: publishing can't fail loudly (no ack), so callers
        confirm via gcode_state. See BambuLink.stop_print."""
        self.link.stop_print()
```

In `MockPrinter`, add (lets the whole arm->10s->stop path run under --mock):

```python
    def stop_print(self) -> None:
        self._touch({"gcode_state": "FAILED"})
```

- [ ] **Step 4: Run to verify it passes**

Run: `python -m pytest server/tests/test_services.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add server/printer.py server/tests/test_services.py
git commit -m "feat(detection): PrinterService/MockPrinter gain stop_print()"
```

---

### Task 4: `detect.py` — atomic writers + result parsing

**Files:**
- Create: `detect.py` (repo root)
- Test: `server/tests/test_detect.py` (new)

- [ ] **Step 1: Write the failing tests** — create `server/tests/test_detect.py`:

```python
import json

import numpy as np

import detect


def test_write_status_round_trips_and_leaves_no_temp(tmp_path):
    payload = {"ts": 1.0, "fps": 4.0, "camera": 0, "conf": 0.25,
               "detections": [], "error": None}
    detect.write_status(tmp_path, payload)
    got = json.loads((tmp_path / "status.json").read_text())
    assert got == payload
    assert [p.name for p in tmp_path.iterdir()] == ["status.json"]


def test_write_frame_writes_a_jpeg(tmp_path):
    detect.write_frame(tmp_path, np.zeros((16, 16, 3), np.uint8))
    data = (tmp_path / "latest.jpg").read_bytes()
    assert data[:2] == b"\xff\xd8"  # JPEG magic


def test_build_status_shape():
    s = detect.build_status([{"cls": "spaghetti", "conf": 0.9, "box": [1, 2, 3, 4]}],
                            ts=5.0, fps=3.0, camera=1, conf=0.3)
    assert s["camera"] == 1 and s["conf"] == 0.3 and s["error"] is None
    assert s["detections"][0]["cls"] == "spaghetti"


class _Boxes:
    def __init__(self, xywh, cls, conf):
        self.xywh = _Arr(xywh); self.cls = _Arr(cls); self.conf = _Arr(conf)


class _Arr:
    def __init__(self, v): self._v = v
    def tolist(self): return self._v


class _Result:
    def __init__(self, boxes): self.boxes = boxes


def test_detections_from_result_parses_boxes():
    r = _Result(_Boxes(xywh=[[10.4, 20.6, 30.0, 40.0]], cls=[3.0], conf=[0.812]))
    names = {3: "spaghetti"}
    got = detect.detections_from_result(r, names)
    assert got == [{"cls": "spaghetti", "conf": 0.812, "box": [10, 21, 30, 40]}]


def test_detections_from_result_empty_when_no_boxes():
    assert detect.detections_from_result(_Result(None), {}) == []
```

- [ ] **Step 2: Run to verify it fails**

Run: `python -m pytest server/tests/test_detect.py -v`
Expected: FAIL (`ModuleNotFoundError: No module named 'detect'`).

- [ ] **Step 3: Implement** — create `detect.py` with the writers, parser, and builder (the YOLO import is deferred to Task 5 so this module imports without torch):

```python
"""Headless webcam failure detector for the bambu-monitor server.

Owns ONE USB camera, runs the trained YOLO failure detector on each frame, and
writes an annotated JPEG + a status.json into --out for the server to read. The
server never opens the camera (Windows allows one process per device); this is
the process that does. See run_camera_detection.py for the windowed variant.

    python detect.py --camera 0 --conf 0.25 --weights runs/.../best.pt --out runs/_detect
    python detect.py --mock --out runs/_detect        # no camera, no weights
"""
from __future__ import annotations

import argparse
import json
import os
import pathlib
import sys
import tempfile
import threading
import time

import cv2
import numpy as np


def _atomic_write_bytes(path: pathlib.Path, data: bytes) -> None:
    """temp + os.replace in the same dir -> a reader never sees a half file
    (the store.py pattern). prefix keeps the temp beside the target."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=path.name,
                              suffix=".tmp")
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    except BaseException:
        pathlib.Path(tmp).unlink(missing_ok=True)
        raise


def write_status(out_dir, payload: dict) -> None:
    _atomic_write_bytes(pathlib.Path(out_dir) / "status.json",
                        json.dumps(payload, separators=(",", ":")).encode())


def write_frame(out_dir, frame: np.ndarray) -> None:
    ok, buf = cv2.imencode(".jpg", frame, [cv2.IMWRITE_JPEG_QUALITY, 85])
    if not ok:
        return
    _atomic_write_bytes(pathlib.Path(out_dir) / "latest.jpg", buf.tobytes())


def build_status(detections, *, ts, fps, camera, conf, error=None) -> dict:
    return {"ts": ts, "fps": fps, "camera": camera, "conf": conf,
            "detections": detections, "error": error}


def detections_from_result(result, names: dict) -> list:
    """One ultralytics Result -> [{cls, conf, box:[x,y,w,h]}]. box is the
    xywh (center x, center y, width, height) the model returns, rounded."""
    boxes = getattr(result, "boxes", None)
    if boxes is None:
        return []
    out = []
    for (x, y, w, h), c, cf in zip(boxes.xywh.tolist(), boxes.cls.tolist(),
                                   boxes.conf.tolist()):
        out.append({"cls": names.get(int(c), str(int(c))),
                    "conf": round(float(cf), 3),
                    "box": [round(x), round(y), round(w), round(h)]})
    return out
```

- [ ] **Step 4: Run to verify it passes**

Run: `python -m pytest server/tests/test_detect.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add detect.py server/tests/test_detect.py
git commit -m "feat(detection): detect.py atomic status/frame writers + result parser"
```

---

### Task 5: `detect.py` — capture loop, mock inference, CLI

**Files:**
- Modify: `detect.py` (loop + `mock_infer` + `make_yolo_infer` + `open_camera` + `main`)
- Test: `server/tests/test_detect.py`

- [ ] **Step 1: Write the failing test** — append to `server/tests/test_detect.py`:

```python
def test_detection_loop_writes_status_and_frame(tmp_path):
    frames = [np.zeros((16, 16, 3), np.uint8) for _ in range(3)]
    grabbed = iter(frames)
    stop = detect.threading.Event()
    calls = {"n": 0}

    def grab():
        return next(grabbed, None)

    def infer(frame):
        calls["n"] += 1
        if calls["n"] >= 2:
            stop.set()   # end after two frames
        return ([{"cls": "spaghetti", "conf": 0.9, "box": [1, 1, 2, 2]}], frame)

    detect.detection_loop(grab, infer, tmp_path, camera=0, conf=0.25,
                          fps=0, stop_event=stop)
    status = json.loads((tmp_path / "status.json").read_text())
    assert status["detections"][0]["cls"] == "spaghetti"
    assert status["error"] is None
    assert (tmp_path / "latest.jpg").exists()


def test_detection_loop_records_camera_read_failure(tmp_path):
    stop = detect.threading.Event()

    def grab():          # a dead camera returns None
        stop.set()
        return None

    detect.detection_loop(grab, lambda f: ([], f), tmp_path, camera=3,
                          conf=0.25, fps=0, stop_event=stop)
    status = json.loads((tmp_path / "status.json").read_text())
    assert status["error"] is not None
    assert status["detections"] == []
```

- [ ] **Step 2: Run to verify it fails**

Run: `python -m pytest server/tests/test_detect.py -k loop -v`
Expected: FAIL (`AttributeError: module 'detect' has no attribute 'detection_loop'`).

- [ ] **Step 3: Implement** — append to `detect.py`:

```python
def open_camera(index: int, width: int = 1280, height: int = 720):
    cap = cv2.VideoCapture(index)
    if not cap.isOpened():
        raise RuntimeError(f"cannot open camera index {index}")
    cap.set(cv2.CAP_PROP_FRAME_WIDTH, width)
    cap.set(cv2.CAP_PROP_FRAME_HEIGHT, height)
    cap.set(cv2.CAP_PROP_BUFFERSIZE, 1)  # always a current frame, not a stale one
    return cap


def mock_infer(frame):
    """No camera, no weights: draw a box and always 'see' spaghetti, so the
    server-side arm->10s->stop path can be exercised end to end."""
    annotated = frame.copy()
    cv2.rectangle(annotated, (4, 4), (frame.shape[1] - 4, frame.shape[0] - 4),
                  (0, 0, 255), 2)
    return ([{"cls": "spaghetti", "conf": 0.9,
              "box": [frame.shape[1] // 2, frame.shape[0] // 2, 8, 8]}], annotated)


def make_yolo_infer(weights, conf, imgsz, device):
    """Build the real inference closure. YOLO/torch import is deferred to here
    so importing detect.py (and its unit tests) never needs a CUDA runtime."""
    from ultralytics import YOLO  # noqa: E402  (heavy, lazy on purpose)
    model = YOLO(str(weights))
    names = model.names if isinstance(model.names, dict) else dict(enumerate(model.names))

    def infer(frame):
        result = model.predict(frame, conf=conf, imgsz=imgsz, device=device,
                               verbose=False)[0]
        return detections_from_result(result, names), result.plot()

    return infer


def detection_loop(grab, infer, out_dir, *, camera, conf, fps, stop_event,
                   clock=time.time):
    """Grab -> infer -> write, until stop_event is set. A None grab (dead
    camera) writes an error status and stops. fps<=0 disables throttling
    (tests)."""
    period = (1.0 / fps) if fps and fps > 0 else 0.0
    while not stop_event.is_set():
        t0 = clock()
        frame = grab()
        if frame is None:
            write_status(out_dir, build_status([], ts=clock(), fps=0.0,
                                               camera=camera, conf=conf,
                                               error=f"camera {camera} read failed"))
            return
        detections, annotated = infer(frame)
        dt = max(clock() - t0, 1e-6)
        write_frame(out_dir, annotated)
        write_status(out_dir, build_status(detections, ts=clock(),
                                           fps=round(1.0 / dt, 1), camera=camera,
                                           conf=conf))
        if period:
            slack = period - (clock() - t0)
            if slack > 0:
                stop_event.wait(slack)


def main() -> int:
    import torch  # local: only the real run needs it
    repo = pathlib.Path(__file__).resolve().parent
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--camera", type=int, default=0)
    p.add_argument("--conf", type=float, default=0.25)
    p.add_argument("--imgsz", type=int, default=640)
    p.add_argument("--weights",
                   default=str(repo / "runs" / "train" / "failure_detector"
                               / "weights" / "best.pt"))
    p.add_argument("--out", type=pathlib.Path, default=repo / "runs" / "_detect")
    p.add_argument("--fps", type=float, default=4.0)
    p.add_argument("--device", default="0" if torch.cuda.is_available() else "cpu")
    p.add_argument("--mock", action="store_true")
    a = p.parse_args()

    stop = threading.Event()
    if a.mock:
        size = (480, 640, 3)
        grab = lambda: np.full(size, 40, np.uint8)  # noqa: E731
        infer = mock_infer
    else:
        if not pathlib.Path(a.weights).exists():
            print(f"weights not found: {a.weights}", file=sys.stderr)
            return 1
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
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
```

- [ ] **Step 4: Run to verify it passes**

Run: `python -m pytest server/tests/test_detect.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add detect.py server/tests/test_detect.py
git commit -m "feat(detection): detect.py capture loop + mock inference + CLI"
```

---

### Task 6: `StatusReader`

**Files:**
- Create: `server/detection.py` (module + `StatusReader`)
- Test: `server/tests/test_detection.py` (new)

- [ ] **Step 1: Write the failing tests** — create `server/tests/test_detection.py`:

```python
import json

from server.detection import StatusReader


def write(tmp_path, payload):
    (tmp_path / "status.json").write_text(json.dumps(payload))


def test_reader_missing_file_is_not_running(tmp_path):
    r = StatusReader(tmp_path, clock=lambda: 100.0).read()
    assert r["running"] is False
    assert r["detections"] == []


def test_reader_fresh_status_is_running(tmp_path):
    write(tmp_path, {"ts": 99.0, "fps": 4.0, "camera": 0, "conf": 0.25,
                     "detections": [{"cls": "spaghetti", "conf": 0.9}],
                     "error": None})
    r = StatusReader(tmp_path, stale_after=3.0, clock=lambda: 100.0).read()
    assert r["running"] is True
    assert r["detections"][0]["cls"] == "spaghetti"


def test_reader_stale_status_is_not_running(tmp_path):
    write(tmp_path, {"ts": 10.0, "fps": 4.0, "camera": 0, "conf": 0.25,
                     "detections": [], "error": None})
    r = StatusReader(tmp_path, stale_after=3.0, clock=lambda: 100.0).read()
    assert r["running"] is False


def test_reader_error_status_is_not_running(tmp_path):
    write(tmp_path, {"ts": 99.5, "fps": 0.0, "camera": 3, "conf": 0.25,
                     "detections": [], "error": "camera 3 read failed"})
    r = StatusReader(tmp_path, clock=lambda: 100.0).read()
    assert r["running"] is False
    assert r["error"] == "camera 3 read failed"


def test_reader_half_written_json_is_not_running(tmp_path):
    (tmp_path / "status.json").write_text('{"ts": 99.0, "detec')  # truncated
    r = StatusReader(tmp_path, clock=lambda: 100.0).read()
    assert r["running"] is False
```

- [ ] **Step 2: Run to verify it fails**

Run: `python -m pytest server/tests/test_detection.py -v`
Expected: FAIL (`ModuleNotFoundError: No module named 'server.detection'`).

- [ ] **Step 3: Implement** — create `server/detection.py`:

```python
"""Server-side detection: read the detector's status, decide, and actuate.

Three pure-ish units + one coordinator:
  StatusReader        - tolerant read of detect.py's status.json
  AutoStopController  - the arm/sustain/stop state machine (no I/O)
  DetectorSupervisor  - spawn/restart the detect.py subprocess
  DetectionCoordinator- ties them together on a background thread

The coordinator NEVER lets the detector command the printer: it reads
detections, runs the controller, and calls PrinterService.stop_print itself.
"""
from __future__ import annotations

import json
import logging
import pathlib
import subprocess
import sys
import threading
import time

from .store import DETECTION_CLASSES as CLASSES  # single source of truth

log = logging.getLogger("server.detection")


class StatusReader:
    def __init__(self, out_dir, *, stale_after: float = 3.0, clock=time.time):
        self.path = pathlib.Path(out_dir) / "status.json"
        self.stale_after = stale_after
        self._clock = clock

    def _down(self) -> dict:
        return {"running": False, "fps": None, "camera": None, "conf": None,
                "detections": [], "error": None, "age_s": None}

    def read(self) -> dict:
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return self._down()
        if not isinstance(data, dict):
            return self._down()
        ts = data.get("ts")
        age = (self._clock() - ts) if isinstance(ts, (int, float)) else None
        running = (age is not None and age <= self.stale_after
                   and not data.get("error"))
        return {"running": bool(running), "fps": data.get("fps"),
                "camera": data.get("camera"), "conf": data.get("conf"),
                "detections": data.get("detections") or [],
                "error": data.get("error"),
                "age_s": None if age is None else round(age, 2)}
```

- [ ] **Step 4: Run to verify it passes**

Run: `python -m pytest server/tests/test_detection.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add server/detection.py server/tests/test_detection.py
git commit -m "feat(detection): StatusReader tolerant status.json reader"
```

---

### Task 7: `AutoStopController` — the safety state machine

**Files:**
- Modify: `server/detection.py` (`AutoStopController`)
- Test: `server/tests/test_detection.py`

- [ ] **Step 1: Write the failing tests** — append to `server/tests/test_detection.py`:

```python
from server.detection import AutoStopController


class Clock:
    def __init__(self): self.t = 0.0
    def __call__(self): return self.t


SPAG = [{"cls": "spaghetti", "conf": 0.9, "box": [0, 0, 1, 1]}]


def armed_controller(clock):
    c = AutoStopController(sustain_s=10.0, verify_s=5.0, clock=clock)
    c.configure(["spaghetti"], 0.5)
    c.arm(True)
    return c


def test_fires_after_ten_sustained_seconds():
    clk = Clock()
    c = armed_controller(clk)
    assert c.update(SPAG, "RUNNING") is None      # t=0, fault starts
    clk.t = 9.9
    assert c.update(SPAG, "RUNNING") is None       # not yet
    clk.t = 10.0
    assert c.update(SPAG, "RUNNING") == "fire"     # sustained -> fire


def test_gap_resets_the_timer():
    clk = Clock()
    c = armed_controller(clk)
    c.update(SPAG, "RUNNING")                       # t=0 fault
    clk.t = 5.0
    c.update([], "RUNNING")                          # clears -> armed_idle
    clk.t = 6.0
    c.update(SPAG, "RUNNING")                         # new fault window
    clk.t = 15.0
    assert c.update(SPAG, "RUNNING") is None          # only 9s -> no fire
    clk.t = 16.0
    assert c.update(SPAG, "RUNNING") == "fire"


def test_below_threshold_never_fires():
    clk = Clock()
    c = armed_controller(clk)
    weak = [{"cls": "spaghetti", "conf": 0.4}]
    for t in (0, 11, 22):
        clk.t = t
        assert c.update(weak, "RUNNING") is None


def test_non_armed_class_ignored():
    clk = Clock()
    c = armed_controller(clk)
    other = [{"cls": "stringing", "conf": 0.99}]
    clk.t = 0; c.update(other, "RUNNING")
    clk.t = 20
    assert c.update(other, "RUNNING") is None


def test_disarmed_never_fires():
    clk = Clock()
    c = AutoStopController(clock=clk)
    c.configure(["spaghetti"], 0.5)  # not armed
    clk.t = 0; c.update(SPAG, "RUNNING")
    clk.t = 30
    assert c.update(SPAG, "RUNNING") is None


def test_fire_auto_disarms_and_latches():
    clk = Clock()
    c = armed_controller(clk)
    c.update(SPAG, "RUNNING"); clk.t = 10.0; c.update(SPAG, "RUNNING")
    snap = c.snapshot()
    assert snap["armed"] is False
    assert snap["stopped_by_monitor"] is True


def test_retries_once_then_gives_up_if_stop_ignored():
    clk = Clock()
    c = armed_controller(clk)
    c.update(SPAG, "RUNNING"); clk.t = 10.0
    assert c.update(SPAG, "RUNNING") == "fire"          # first stop
    clk.t = 15.0
    assert c.update(SPAG, "RUNNING") == "fire"          # not stopped -> retry
    clk.t = 20.0
    assert c.update(SPAG, "RUNNING") is None            # gave up, latched


def test_no_retry_when_stop_confirmed():
    clk = Clock()
    c = armed_controller(clk)
    c.update(SPAG, "RUNNING"); clk.t = 10.0
    assert c.update(SPAG, "RUNNING") == "fire"
    clk.t = 15.0
    assert c.update(SPAG, "FAILED") is None             # printer stopped
    assert c.snapshot()["state"] == "stopped"


def test_seconds_to_stop_counts_down():
    clk = Clock()
    c = armed_controller(clk)
    c.update(SPAG, "RUNNING")
    clk.t = 4.0
    assert c.snapshot()["seconds_to_stop"] == 6.0
```

- [ ] **Step 2: Run to verify it fails**

Run: `python -m pytest server/tests/test_detection.py -k "fire or reset or disarm or retry or seconds or threshold or ignored" -v`
Expected: FAIL (`ImportError: cannot import name 'AutoStopController'`).

- [ ] **Step 3: Implement** — append to `server/detection.py`:

```python
class AutoStopController:
    """arm -> a qualifying failure held for sustain_s -> 'fire'. Pure: it
    returns "fire" and the caller actuates. Firing auto-disarms; a sub-threshold
    gap resets the timer. In 'stopping' it re-sends once if gcode_state hasn't
    gone terminal within verify_s, then latches 'stopped'."""

    TERMINAL = ("FAILED", "IDLE", "FINISH")

    def __init__(self, *, sustain_s: float = 10.0, verify_s: float = 5.0,
                 clock=time.time):
        self._sustain_s = sustain_s
        self._verify_s = verify_s
        self._clock = clock
        self._classes: set = {"spaghetti"}
        self._threshold = 0.25
        self._armed = False
        self._state = "disarmed"
        self._fault_since = None
        self._stop_at = None
        self._stop_count = 0
        self._stopped_by_monitor = False

    def configure(self, armed_classes, threshold) -> None:
        self._classes = set(armed_classes)
        self._threshold = float(threshold)

    def arm(self, value: bool) -> None:
        if value:
            self._armed = True
            self._stopped_by_monitor = False
            self._state = "armed_idle"
            self._fault_since = None
        else:
            self._armed = False
            self._state = "disarmed"
            self._fault_since = None

    def _qualifying(self, detections) -> bool:
        return any(d.get("cls") in self._classes
                   and float(d.get("conf", 0.0)) >= self._threshold
                   for d in detections)

    def update(self, detections, gcode_state) -> str | None:
        now = self._clock()
        if self._state in ("disarmed", "stopped"):
            return None
        fault = self._qualifying(detections)

        if self._state == "armed_idle":
            if fault:
                self._state = "armed_faulting"
                self._fault_since = now
            return None

        if self._state == "armed_faulting":
            if not fault:
                self._state = "armed_idle"
                self._fault_since = None
                return None
            if now - self._fault_since >= self._sustain_s:
                self._armed = False
                self._stopped_by_monitor = True
                self._state = "stopping"
                self._stop_at = now
                self._stop_count = 1
                return "fire"
            return None

        if self._state == "stopping":
            if gcode_state in self.TERMINAL:
                self._state = "stopped"
                return None
            if now - self._stop_at >= self._verify_s:
                if self._stop_count < 2:
                    self._stop_count += 1
                    self._stop_at = now
                    return "fire"
                self._state = "stopped"   # gave up re-sending; stop trying
            return None
        return None

    def snapshot(self) -> dict:
        secs = None
        if self._state == "armed_faulting" and self._fault_since is not None:
            secs = round(max(0.0, self._sustain_s
                             - (self._clock() - self._fault_since)), 1)
        return {"armed": self._armed, "state": self._state,
                "seconds_to_stop": secs,
                "stopped_by_monitor": self._stopped_by_monitor}
```

- [ ] **Step 4: Run to verify it passes**

Run: `python -m pytest server/tests/test_detection.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add server/detection.py server/tests/test_detection.py
git commit -m "feat(detection): AutoStopController arm/sustain/stop state machine"
```

---

### Task 8: `DetectorSupervisor`

**Files:**
- Modify: `server/detection.py` (`DetectorSupervisor`)
- Test: `server/tests/test_detection.py`

- [ ] **Step 1: Write the failing tests** — append to `server/tests/test_detection.py`:

```python
from server.detection import DetectorSupervisor


class FakeProc:
    def __init__(self, argv): self.argv = argv; self._alive = True; self.terminated = False
    def poll(self): return None if self._alive else 1
    def terminate(self): self.terminated = True; self._alive = False
    def die(self): self._alive = False


def supervisor(tmp_path, clock):
    spawned = []
    def spawn(argv):
        p = FakeProc(argv); spawned.append(p); return p
    sup = DetectorSupervisor(tmp_path, "weights.pt", spawn=spawn,
                             clock=clock, backoff_s=5.0)
    return sup, spawned


T1 = {"serial": "S1", "camera_index": 0, "conf": 0.25}
T2 = {"serial": "S1", "camera_index": 2, "conf": 0.4}


def test_spawns_for_a_new_target(tmp_path):
    sup, spawned = supervisor(tmp_path, lambda: 0.0)
    sup.reconcile(T1)
    assert len(spawned) == 1
    assert "--camera" in spawned[0].argv and "0" in spawned[0].argv


def test_argv_never_contains_access_code(tmp_path):
    sup, spawned = supervisor(tmp_path, lambda: 0.0)
    sup.reconcile(T1)
    assert not any("code" in str(a) for a in spawned[0].argv)


def test_changed_target_restarts(tmp_path):
    sup, spawned = supervisor(tmp_path, lambda: 0.0)
    sup.reconcile(T1)
    sup.reconcile(T2)
    assert spawned[0].terminated is True
    assert len(spawned) == 2
    assert "2" in spawned[1].argv


def test_none_target_stops(tmp_path):
    sup, spawned = supervisor(tmp_path, lambda: 0.0)
    sup.reconcile(T1)
    sup.reconcile(None)
    assert spawned[0].terminated is True


def test_crash_respawns_after_backoff(tmp_path):
    clk = Clock()
    sup, spawned = supervisor(tmp_path, clk)
    sup.reconcile(T1)
    spawned[0].die()
    clk.t = 2.0
    sup.reconcile(T1)                 # within backoff -> no respawn
    assert len(spawned) == 1
    clk.t = 6.0
    sup.reconcile(T1)                 # backoff elapsed -> respawn
    assert len(spawned) == 2
```

- [ ] **Step 2: Run to verify it fails**

Run: `python -m pytest server/tests/test_detection.py -k "spawn or target or crash or argv" -v`
Expected: FAIL (`ImportError: cannot import name 'DetectorSupervisor'`).

- [ ] **Step 3: Implement** — append to `server/detection.py`:

```python
class DetectorSupervisor:
    """Keeps exactly one detect.py subprocess matching the desired target.
    Injectable spawn/clock make it testable with no real process."""

    def __init__(self, out_dir, weights, *, python=sys.executable,
                 script="detect.py", spawn=subprocess.Popen, clock=time.time,
                 backoff_s: float = 5.0, fps: float = 4.0):
        self._out_dir = pathlib.Path(out_dir)
        self._weights = weights
        self._python = python
        self._script = script
        self._spawn_fn = spawn
        self._clock = clock
        self._backoff_s = backoff_s
        self._fps = fps
        self._target = None
        self._proc = None
        self._last_spawn = 0.0

    def build_argv(self, target) -> list:
        # NB: no access code -- detect.py never talks MQTT.
        return [self._python, self._script,
                "--camera", str(target["camera_index"]),
                "--conf", str(target["conf"]),
                "--weights", str(self._weights),
                "--out", str(self._out_dir),
                "--fps", str(self._fps)]

    def _spawn(self, target) -> None:
        self._proc = self._spawn_fn(self.build_argv(target))
        self._last_spawn = self._clock()

    def _stop_proc(self) -> None:
        if self._proc is not None:
            try:
                self._proc.terminate()
            except Exception as e:  # noqa: BLE001
                log.warning("detector terminate failed: %s", e)
            self._proc = None

    def reconcile(self, target) -> None:
        if target != self._target:
            self._stop_proc()
            self._target = target
            if target is not None:
                self._spawn(target)
            return
        if target is None:
            return
        if self._proc is not None and self._proc.poll() is not None:
            if self._clock() - self._last_spawn >= self._backoff_s:
                log.warning("detector exited; respawning")
                self._spawn(target)

    def stop(self) -> None:
        self._stop_proc()
        self._target = None
```

- [ ] **Step 4: Run to verify it passes**

Run: `python -m pytest server/tests/test_detection.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add server/detection.py server/tests/test_detection.py
git commit -m "feat(detection): DetectorSupervisor keeps one detect.py per target"
```

---

### Task 9: Registry detection accessors

**Files:**
- Modify: `server/registry.py` (`capture_serial`, `detection_config`, `detection_target`, `update_detection`)
- Test: `server/tests/test_registry.py`

- [ ] **Step 1: Write the failing tests** — first inspect `server/tests/test_registry.py` to reuse its existing fake-service factory, then append. If it has no reusable factory, add this self-contained block:

```python
# --- detection accessors (Task 9) ---
from server.registry import PrinterRegistry
from server.store import MemoryStore, PrinterConfig


class _Svc:
    def __init__(self, cfg):
        self.serial = cfg.serial; self.host = cfg.host; self.name = cfg.name
        self.capture = cfg.capture
    def start(self): pass
    def stop(self): pass
    def summary(self): return {"serial": self.serial}
    def list_files(self, path="/"): return []


def _reg(*cfgs):
    store = MemoryStore(); store.save(list(cfgs))
    reg = PrinterRegistry(store, _Svc); reg.load(); return reg


def _cfg(serial, **kw):
    return PrinterConfig(serial=serial, host="1.2.3.4", access_code="c", **kw)


def test_capture_serial_returns_the_capture_printer():
    reg = _reg(_cfg("A"), _cfg("B", capture=True))
    assert reg.capture_serial() == "B"


def test_capture_serial_none_when_no_capture():
    assert _reg(_cfg("A")).capture_serial() is None


def test_detection_target_requires_detect_enabled():
    reg = _reg(_cfg("B", capture=True, detect_enabled=False))
    assert reg.detection_target() is None
    reg.update_detection("B", detect_enabled=True)
    assert reg.detection_target() == {"serial": "B", "camera_index": 0, "conf": 0.25}


def test_update_detection_persists_and_clamps():
    reg = _reg(_cfg("B", capture=True))
    assert reg.update_detection("B", camera_index=2, conf=0.6,
                                armed_classes=["spaghetti", "cracks"]) is True
    cfg = reg.detection_config("B")
    assert cfg["camera_index"] == 2 and cfg["conf"] == 0.6
    assert cfg["armed_classes"] == ["spaghetti", "cracks"]


def test_update_detection_unknown_serial_false():
    assert _reg(_cfg("A")).update_detection("ZZ", conf=0.9) is False
```

- [ ] **Step 2: Run to verify it fails**

Run: `python -m pytest server/tests/test_registry.py -k "detection or capture_serial" -v`
Expected: FAIL (`AttributeError: 'PrinterRegistry' object has no attribute 'capture_serial'`).

- [ ] **Step 3: Implement** — in `server/registry.py`, add to `PrinterRegistry` (import the class list for validation at top: `from .store import PrinterConfig, DETECTION_CLASSES`):

```python
    def capture_serial(self):
        with self._lock:
            for serial, cfg in self._configs.items():
                if cfg.capture:
                    return serial
        return None

    def detection_config(self, serial):
        with self._lock:
            cfg = self._configs.get(serial)
            if cfg is None:
                return None
            return {"camera_index": cfg.camera_index, "conf": cfg.conf,
                    "armed_classes": list(cfg.armed_classes),
                    "detect_enabled": cfg.detect_enabled}

    def detection_target(self):
        with self._lock:
            for serial, cfg in self._configs.items():
                if cfg.capture and cfg.detect_enabled:
                    return {"serial": serial, "camera_index": cfg.camera_index,
                            "conf": cfg.conf}
        return None

    def update_detection(self, serial, *, camera_index=None, conf=None,
                         armed_classes=None, detect_enabled=None) -> bool:
        with self._lock:
            cfg = self._configs.get(serial)
            if cfg is None:
                return False
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
git commit -m "feat(detection): registry detection_target/config/update accessors"
```

---

### Task 10: `DetectionCoordinator` + `MockDetectorRunner`

**Files:**
- Modify: `server/detection.py` (`DetectionCoordinator`, `MockDetectorRunner`)
- Test: `server/tests/test_detection.py`

- [ ] **Step 1: Write the failing tests** — append to `server/tests/test_detection.py`:

```python
from server.detection import DetectionCoordinator


class FakeRunner:
    def __init__(self): self.targets = []; self.stopped = False
    def reconcile(self, target): self.targets.append(target)
    def stop(self): self.stopped = True


class FakeReg:
    def __init__(self, target, gstate="RUNNING"):
        self._target = target
        self._gstate = gstate
        self.stopped = 0
    def detection_target(self): return self._target
    def capture_serial(self): return self._target["serial"] if self._target else None
    def detection_config(self, serial):
        return {"camera_index": 0, "conf": 0.5,
                "armed_classes": ["spaghetti"], "detect_enabled": True}
    def get(self, serial):
        reg = self
        class S:
            def summary(self_): return {"serial": serial, "gcode_state": reg._gstate}
            def stop_print(self_): reg.stopped += 1; reg._gstate = "FAILED"
        return S()


def test_tick_reconciles_the_runner(tmp_path):
    reg = FakeReg({"serial": "S1", "camera_index": 0, "conf": 0.5})
    runner = FakeRunner()
    co = DetectionCoordinator(reg, tmp_path, runner)
    co.tick()
    assert runner.targets[-1]["serial"] == "S1"


def test_tick_fires_stop_after_sustained_fault(tmp_path):
    reg = FakeReg({"serial": "S1", "camera_index": 0, "conf": 0.5})
    runner = FakeRunner()
    clk = Clock()
    co = DetectionCoordinator(reg, tmp_path, runner,
                              controller_factory=lambda: AutoStopController(clock=clk))
    (tmp_path / "_detect").mkdir()
    def status(ts):
        (tmp_path / "_detect" / "status.json").write_text(json.dumps(
            {"ts": ts, "fps": 4.0, "camera": 0, "conf": 0.5,
             "detections": [{"cls": "spaghetti", "conf": 0.9}], "error": None}))
    co.reader = StatusReader(tmp_path / "_detect", clock=lambda: clk.t)
    co.arm("S1", True)
    status(clk.t); co.tick()                 # t=0 fault begins
    clk.t = 10.0; status(clk.t); co.tick()   # sustained -> fire
    assert reg.stopped == 1


def test_stale_status_does_not_fire(tmp_path):
    reg = FakeReg({"serial": "S1", "camera_index": 0, "conf": 0.5})
    clk = Clock()
    co = DetectionCoordinator(reg, tmp_path, FakeRunner(),
                              controller_factory=lambda: AutoStopController(clock=clk))
    (tmp_path / "_detect").mkdir()
    (tmp_path / "_detect" / "status.json").write_text(json.dumps(
        {"ts": 0.0, "fps": 4.0, "camera": 0, "conf": 0.5,
         "detections": [{"cls": "spaghetti", "conf": 0.9}], "error": None}))
    co.reader = StatusReader(tmp_path / "_detect", stale_after=3.0, clock=lambda: clk.t)
    co.arm("S1", True)
    for t in (0, 11, 22, 33):                # status is always stale (ts=0)
        clk.t = float(t); co.tick()
    assert reg.stopped == 0                   # never acts on stale detections


def test_snapshot_none_for_non_capture(tmp_path):
    reg = FakeReg({"serial": "S1", "camera_index": 0, "conf": 0.5})
    co = DetectionCoordinator(reg, tmp_path, FakeRunner())
    assert co.snapshot("OTHER") is None
    snap = co.snapshot("S1")
    assert snap["armed_classes"] == ["spaghetti"]
```

- [ ] **Step 2: Run to verify it fails**

Run: `python -m pytest server/tests/test_detection.py -k "tick or snapshot or stale" -v`
Expected: FAIL (`ImportError: cannot import name 'DetectionCoordinator'`).

- [ ] **Step 3: Implement** — append to `server/detection.py`:

```python
DETECT_SUBDIR = "_detect"
TICK_S = 0.5


class DetectionCoordinator:
    """Background thread: reconcile the detector, read status, run the
    controller, actuate the stop. Also serves detection snapshots to the API/WS.
    """

    def __init__(self, registry, runs_dir, runner, *, tick_s: float = TICK_S,
                 controller_factory=AutoStopController):
        self.registry = registry
        self.out_dir = pathlib.Path(runs_dir) / DETECT_SUBDIR
        self.runner = runner
        self.reader = StatusReader(self.out_dir)
        self._factory = controller_factory
        self._controllers = {}
        self._tick_s = tick_s
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._loop, daemon=True)

    def _controller_for(self, serial):
        c = self._controllers.get(serial)
        if c is None:
            c = self._factory()
            self._controllers[serial] = c
        return c

    def tick(self) -> None:
        self.runner.reconcile(self.registry.detection_target())
        cap = self.registry.capture_serial()
        if cap is None:
            return
        cfg = self.registry.detection_config(cap)
        status = self.reader.read()
        # NEVER act on stale/errored detections -- feed the controller [] so a
        # dead detector can't leave a stale 'spaghetti' ticking toward a stop.
        detections = status["detections"] if status["running"] else []
        svc = self.registry.get(cap)
        gstate = svc.summary().get("gcode_state") if svc else None
        with self._lock:
            ctrl = self._controller_for(cap)
            ctrl.configure(cfg["armed_classes"], cfg["conf"])
            action = ctrl.update(detections, gstate)
        if action == "fire" and svc is not None:
            log.warning("auto-stop firing for %s", cap)
            try:
                svc.stop_print()
            except Exception as e:  # noqa: BLE001
                log.error("stop_print failed for %s: %s", cap, e)

    def snapshot(self, serial):
        if serial != self.registry.capture_serial():
            return None
        cfg = self.registry.detection_config(serial)
        if cfg is None:
            return None
        status = self.reader.read()
        with self._lock:
            snap = self._controller_for(serial).snapshot()
        return {"running": status["running"], "fps": status["fps"],
                "camera_index": cfg["camera_index"], "conf": cfg["conf"],
                "detect_enabled": cfg["detect_enabled"],
                "armed": snap["armed"], "armed_classes": cfg["armed_classes"],
                "detections": status["detections"] if status["running"] else [],
                "stopped_by_monitor": snap["stopped_by_monitor"],
                "seconds_to_stop": snap["seconds_to_stop"],
                "error": status["error"]}

    def arm(self, serial, value: bool) -> None:
        with self._lock:
            self._controller_for(serial).arm(value)

    def frame_path(self):
        p = self.out_dir / "latest.jpg"
        return p if p.exists() else None

    def _loop(self) -> None:
        while not self._stop.wait(self._tick_s):
            try:
                self.tick()
            except Exception as e:  # noqa: BLE001 - a bad tick must not kill the loop
                log.exception("detection tick failed: %s", e)

    def start(self) -> None:
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread.is_alive():
            self._thread.join(timeout=2)
        self.runner.stop()


class MockDetectorRunner:
    """--mock stand-in for DetectorSupervisor: instead of spawning detect.py,
    write a synthetic 'spaghetti' status.json so the arm->10s->stop loop runs
    with no camera and no weights. Reuses detect.write_status for the same
    atomic-write contract."""

    def __init__(self, out_dir, *, period_s: float = 0.5):
        self.out_dir = pathlib.Path(out_dir)
        self._period = period_s
        self._active = False
        self._stop = threading.Event()
        self._thread = None

    def reconcile(self, target) -> None:
        if target and not self._active:
            self._active = True
            self._stop.clear()
            self._thread = threading.Thread(target=self._loop, daemon=True)
            self._thread.start()
        elif not target and self._active:
            self._halt()

    def _loop(self) -> None:
        import detect  # root module; lazy so server imports don't need cv2 early
        while not self._stop.wait(self._period):
            detect.write_status(self.out_dir, detect.build_status(
                [{"cls": "spaghetti", "conf": 0.9, "box": [0, 0, 8, 8]}],
                ts=time.time(), fps=4.0, camera=0, conf=0.25))

    def _halt(self) -> None:
        self._stop.set()
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=2)
        self._active = False

    def stop(self) -> None:
        self._halt()
```

- [ ] **Step 4: Run to verify it passes**

Run: `python -m pytest server/tests/test_detection.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add server/detection.py server/tests/test_detection.py
git commit -m "feat(detection): DetectionCoordinator + MockDetectorRunner"
```

---

### Task 11: API routes + WebSocket merge

**Files:**
- Modify: `server/main.py` (`create_app` gains `detection=None`; 4 routes; WS merge)
- Test: `server/tests/test_api.py`

- [ ] **Step 1: Write the failing tests** — append to `server/tests/test_api.py`:

```python
class FakeDetection:
    def __init__(self, capture="S1"):
        self.capture = capture
        self.armed = {}
        self.updated = []
        self._frame = None
    def snapshot(self, serial):
        if serial != self.capture:
            return None
        return {"running": True, "fps": 4.0, "camera_index": 0, "conf": 0.25,
                "detect_enabled": True, "armed": self.armed.get(serial, False),
                "armed_classes": ["spaghetti"], "detections": [],
                "stopped_by_monitor": False, "seconds_to_stop": None,
                "error": None}
    def arm(self, serial, value): self.armed[serial] = value
    def frame_path(self): return self._frame
    def start(self): pass
    def stop(self): pass


class DetRegistry(FakeRegistry):
    def detection_config(self, serial):
        return {"camera_index": 0, "conf": 0.25,
                "armed_classes": ["spaghetti"], "detect_enabled": True}
    def update_detection(self, serial, **kw):
        if serial not in self._services:
            return False
        self.updated = getattr(self, "updated", [])
        self.updated.append(kw)
        return True


def det_client(tmp_path, detection, registry=None):
    from server.main import create_app
    reg = registry or DetRegistry([FakeService("S1")])
    return TestClient(create_app(reg, tmp_path, detection=detection)), reg


def test_get_detection_returns_snapshot(tmp_path):
    c, _ = det_client(tmp_path, FakeDetection())
    r = c.get("/api/printers/S1/detection")
    assert r.status_code == 200
    assert r.json()["armed_classes"] == ["spaghetti"]


def test_get_detection_404_for_non_capture(tmp_path):
    c, _ = det_client(tmp_path, FakeDetection(capture="OTHER"))
    assert c.get("/api/printers/S1/detection").status_code == 404


def test_put_detection_updates_and_returns_snapshot(tmp_path):
    det = FakeDetection()
    c, reg = det_client(tmp_path, det)
    r = c.put("/api/printers/S1/detection",
              json={"camera_index": 2, "conf": 0.4,
                    "armed_classes": ["spaghetti", "cracks"], "detect_enabled": True})
    assert r.status_code == 200
    assert reg.updated[-1]["camera_index"] == 2


def test_put_detection_rejects_unknown_class_400(tmp_path):
    c, _ = det_client(tmp_path, FakeDetection())
    r = c.put("/api/printers/S1/detection", json={"armed_classes": ["banana"]})
    assert r.status_code == 400


def test_arm_toggles_and_returns_snapshot(tmp_path):
    det = FakeDetection()
    c, _ = det_client(tmp_path, det)
    r = c.post("/api/printers/S1/detection/arm", json={"armed": True})
    assert r.status_code == 200
    assert det.armed["S1"] is True


def test_detection_frame_404_when_none(tmp_path):
    c, _ = det_client(tmp_path, FakeDetection())
    assert c.get("/api/printers/S1/detection/frame").status_code == 404


def test_detection_frame_served(tmp_path):
    det = FakeDetection()
    frame = tmp_path / "latest.jpg"
    frame.write_bytes(b"\xff\xd8\xff\xe0jpeg")
    det._frame = frame
    c, _ = det_client(tmp_path, det)
    r = c.get("/api/printers/S1/detection/frame")
    assert r.status_code == 200
    assert r.headers["content-type"] == "image/jpeg"


def test_ws_merges_detection_into_capture_summary(tmp_path):
    c, _ = det_client(tmp_path, FakeDetection())
    with c.websocket_connect("/ws") as ws:
        msg = ws.receive_json()
        p = next(p for p in msg["printers"] if p["serial"] == "S1")
        assert p["detection"]["running"] is True


def test_detection_routes_404_when_detection_disabled(tmp_path):
    # create_app(..., detection=None) -> the whole feature is inert.
    r = client(tmp_path).get("/api/printers/S1/detection")
    assert r.status_code == 404
```

- [ ] **Step 2: Run to verify it fails**

Run: `python -m pytest server/tests/test_api.py -k detection -v`
Expected: FAIL (routes 404/AttributeError; `create_app` has no `detection` kwarg).

- [ ] **Step 3: Implement** — in `server/main.py`:

Add near the top:

```python
from server.detection import CLASSES  # the 6 valid armed classes
```

Add request models next to `AddPrinter`:

```python
class DetectionUpdate(BaseModel):
    camera_index: int | None = None
    conf: float | None = None
    armed_classes: list[str] | None = None
    detect_enabled: bool | None = None


class ArmBody(BaseModel):
    armed: bool
```

Change the signature:

```python
def create_app(registry, runs_dir: pathlib.Path,
               frontend_dist: pathlib.Path | None = None,
               detection=None) -> FastAPI:
```

Add a small helper and the routes inside `create_app` (before the static mount):

```python
    def _require_detection_snapshot(serial):
        if detection is None:
            raise HTTPException(404, "detection not enabled on this server")
        snap = detection.snapshot(serial)
        if snap is None:
            raise HTTPException(404, "not the capture printer")
        return snap

    @app.get("/api/printers/{serial}/detection")
    def get_detection(serial: str):
        return _require_detection_snapshot(serial)

    @app.put("/api/printers/{serial}/detection")
    def put_detection(serial: str, body: DetectionUpdate):
        if detection is None:
            raise HTTPException(404, "detection not enabled on this server")
        if body.armed_classes is not None:
            bad = [c for c in body.armed_classes if c not in CLASSES]
            if bad:
                raise HTTPException(400, f"unknown class(es): {', '.join(bad)}")
        fields = {k: v for k, v in body.model_dump().items() if v is not None}
        if not registry.update_detection(serial, **fields):
            raise HTTPException(404, "unknown printer")
        # Config can be set on any printer, but a snapshot only exists for the
        # capture printer -- return it when present, else just confirm the save
        # (avoids a confusing 404 after a successful update).
        snap = detection.snapshot(serial)
        return snap if snap is not None else {"updated": True}

    @app.post("/api/printers/{serial}/detection/arm")
    def arm_detection(serial: str, body: ArmBody):
        snap = _require_detection_snapshot(serial)  # 404s if not capture
        detection.arm(serial, body.armed)
        return detection.snapshot(serial) or snap

    @app.get("/api/printers/{serial}/detection/frame")
    def detection_frame(serial: str):
        if detection is None:
            raise HTTPException(404, "detection not enabled on this server")
        path = detection.frame_path()
        if path is None:
            return JSONResponse({"error": "no detector frame"}, status_code=404)
        try:
            data = path.read_bytes()
        except OSError:
            return JSONResponse({"error": "no detector frame"}, status_code=404)
        return Response(content=data, media_type="image/jpeg",
                        headers={"Cache-Control": "no-store"})
```

Add a merge helper (top-level, next to `_comparable`):

```python
def _with_detection(printers: list[dict], detection) -> list[dict]:
    """Attach a `detection` object to each summary (None unless it's the
    capture printer). Detection state lives in detection.py, not the service."""
    if detection is None:
        return printers
    for p in printers:
        p["detection"] = detection.snapshot(p.get("serial"))
    return printers
```

In `list_printers` and the WebSocket handler, wrap the summaries. For `list_printers`:

```python
    @app.get("/api/printers")
    def list_printers():
        return {"printers": _with_detection(registry.summaries(), detection)}
```

In the `ws` coroutine, apply `_with_detection` everywhere `registry.summaries()` is read (both the first send and inside the loop), e.g.:

```python
            printers = _with_detection(registry.summaries(), detection)
```

- [ ] **Step 4: Run to verify it passes**

Run: `python -m pytest server/tests/test_api.py -v`
Expected: PASS (including all pre-existing route tests — `_with_detection` is a no-op when `detection is None`).

- [ ] **Step 5: Commit**

```bash
git add server/main.py server/tests/test_api.py
git commit -m "feat(detection): detection API routes + WebSocket summary merge"
```

---

### Task 12: Wire the coordinator into the app lifecycle + `--mock`

**Files:**
- Modify: `server/main.py` (start/stop the coordinator via lifespan when `detection` is provided)
- Modify: `server/__main__.py` (build the real coordinator; `--mock` builds `MockDetectorRunner`)
- Test: `server/tests/test_api.py` (lifespan no-crash), plus a documented manual `--mock` check

- [ ] **Step 1: Write the failing test** — append to `server/tests/test_api.py`:

```python
def test_lifespan_starts_and_stops_detection(tmp_path):
    events = []

    class LifecycleDetection(FakeDetection):
        def start(self): events.append("start")
        def stop(self): events.append("stop")

    c, _ = det_client(tmp_path, LifecycleDetection())
    with c:                      # triggers startup + shutdown
        pass
    assert events == ["start", "stop"]
```

- [ ] **Step 2: Run to verify it fails**

Run: `python -m pytest server/tests/test_api.py -k lifespan -v`
Expected: FAIL (no lifespan wired; `events` stays empty).

- [ ] **Step 3: Implement**

In `server/main.py`, wire a lifespan when detection is present. Add the import:

```python
from contextlib import asynccontextmanager
```

Build the app with a lifespan inside `create_app` (replace the bare `app = FastAPI(title="bambu-monitor")`):

```python
    @asynccontextmanager
    async def lifespan(_app):
        if detection is not None:
            detection.start()
        try:
            yield
        finally:
            if detection is not None:
                detection.stop()

    app = FastAPI(title="bambu-monitor", lifespan=lifespan)
```

In `server/__main__.py`, construct the coordinator and pass it to `create_app`. Locate where `create_app(registry, runs_dir, dist)` is called and the `--mock` branch, then:

```python
from .detection import DetectionCoordinator, DetectorSupervisor, MockDetectorRunner

# ... after runs_dir and registry are built, before create_app:
detect_out = runs_dir / "_detect"
weights = pathlib.Path(__file__).resolve().parent.parent / "runs" / "train" \
    / "failure_detector" / "weights" / "best.pt"
if args.mock:
    runner = MockDetectorRunner(detect_out)
else:
    runner = DetectorSupervisor(detect_out, weights)
coordinator = DetectionCoordinator(registry, runs_dir, runner)

app = create_app(registry, runs_dir, dist, detection=coordinator)
```

(Adapt `args.mock`, `runs_dir`, `dist` to the names already in `__main__.py`.)

- [ ] **Step 4: Run to verify it passes**

Run: `python -m pytest server/tests/test_api.py -v`
Expected: PASS.

- [ ] **Step 5: Run the whole suite**

Run: `python -m pytest -q`
Expected: PASS (all server tests, no regressions).

- [ ] **Step 6: Manual `--mock` end-to-end** (no hardware) — verifies the full loop:

```bash
# terminal 1
python -m server --mock
```

Then in terminal 2, drive it by API (replace SERIAL with a mock serial from GET /api/printers, and mark it capture if the mock doesn't already):

```bash
curl -s localhost:8000/api/printers | python -m json.tool          # find the capture serial
curl -s -X PUT localhost:8000/api/printers/<SERIAL>/detection \
     -H "Content-Type: application/json" \
     -d '{"detect_enabled": true}'
curl -s -X POST localhost:8000/api/printers/<SERIAL>/detection/arm \
     -H "Content-Type: application/json" -d '{"armed": true}'
# wait ~11s, then:
curl -s localhost:8000/api/printers | python -m json.tool          # gcode_state -> FAILED
```

Expected: within ~11 s of arming, the mock capture printer's `gcode_state` reads `FAILED` and its `detection.stopped_by_monitor` is `true`. This exercises detector-runner → status.json → StatusReader → AutoStopController (10 s) → `MockPrinter.stop_print()` with no camera and no printer.

> If `--mock` does not already mark one printer as `capture`, add that in `__main__.py`'s mock seeding (set `capture=True` on one seeded config) as part of this step, and note it in the commit.

- [ ] **Step 7: Commit**

```bash
git add server/main.py server/__main__.py
git commit -m "feat(detection): supervise detector in app lifespan; --mock drives the loop"
```

---

## Self-Review (completed while writing)

**Spec coverage — every Phase-1 backend requirement maps to a task:**
- Separate detector process, camera index/conf, atomic status+frame → Tasks 4–5
- Server supervises it for the capture printer → Tasks 8, 10, 12
- Disk handoff / tolerant read → Tasks 4, 6
- Auto-stop: off-by-default, per-class, 10 s sustained, verify+retry, auto-disarm → Task 7 (+ Task 10 wiring)
- `stop` command, mock → FAILED → Tasks 2, 3
- Config persisted (`camera_index/conf/armed_classes/detect_enabled`); `armed` runtime-only → Tasks 1, 9 (armed lives only in the controller)
- `detection` merged into the capture summary; GET/PUT/arm/frame routes → Task 11
- Detection state kept out of `PrinterService` (joined at the WS edge) → Task 11 `_with_detection`
- Fully mockable end-to-end → Tasks 10, 12
- Stale detector never triggers a stop → Task 10 (`detections if running else []`)

**Out of scope here (own plans):** the Detection page + Dashboard Auto-stop card (frontend, Phase-1b); the print queue (Phase-2).

**Type/name consistency:** `AutoStopController.update -> "fire"|None`; `detection_target()`/`detection_config()`/`update_detection()` names match across Tasks 9–12; the `detection` snapshot keys are identical in Tasks 10 (producer) and 11 (`FakeDetection`), matching the spec's shared contract.

**Placeholder scan:** none — every code step is complete.

## Hardware-deferred (cannot pass under `--mock`; verify on the real A1 mini)
- The printer actually honouring `{"print":{"command":"stop"}}` (Task 2). The verify+retry in Task 7 is the backstop if a firmware variant ignores it.
- A real webcam at `camera_index` and real YOLO inference (`make_yolo_infer`, Task 5) — the loop and writers are tested; the torch path is not.
