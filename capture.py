"""
Layer-indexed capture logger for a Bambu A1 / A1 mini.

What it does, and nothing more:
  * subscribes to MQTT telemetry, appends every report to telemetry.jsonl
  * on each layer_num increase, waits --settle seconds, grabs a burst of
    frames from an EXTERNAL webcam, keeps the sharpest, writes
    frames/layer_NNNN.jpg
  * starts a new run directory whenever gcode_state transitions to RUNNING

Why the settle delay: with Smooth timelapse enabled the toolhead parks at the
same place every layer. MQTT tells you the layer changed, but not that the
park is complete. --settle is the fudge. Tune it once by eyeballing the first
run's frames, then never touch it again.

Why the burst + sharpness pick: the A1 is a bed-slinger, the bed is still
settling, and one unlucky frame is a smear. Three frames and a Laplacian
variance pick costs ~150 ms and removes most of that.

Why NOT the built-in camera: ~0.5 fps, toolhead-mounted, fixed focus at 15 cm.
It is in the right place for CAXTON-style nozzle monitoring and the wrong
place for everything else, and its frame rate makes it useless for both.
Use a $25 USB webcam on a fixed mount.

Usage:
    python capture.py --host 192.168.1.42 --serial 0309xxxxxxxx \
                      --access-code 12345678 --camera 0 --out runs/

    python capture.py --mock --out runs/      # no hardware, tests the pipeline
"""

from __future__ import annotations

import argparse
import csv
import json
import logging
import pathlib
import re
import sys
import threading
import time
from datetime import datetime, timezone

import cv2
import numpy as np

sys.path.insert(0, str(pathlib.Path(__file__).parent))
from bambu_link import BambuLink, decode_hms  # noqa: E402

log = logging.getLogger("capture")


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


def slug(s: str | None) -> str:
    if not s:
        return "unknown"
    return re.sub(r"[^A-Za-z0-9_.-]+", "_", s)[:48].strip("_") or "unknown"


def sharpness(frame: np.ndarray) -> float:
    """Variance of Laplacian. Higher = crisper. Only meaningful for comparing
    frames of the SAME scene, which is exactly what we use it for."""
    gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
    return float(cv2.Laplacian(gray, cv2.CV_64F).var())


# --------------------------------------------------------------------------
# cameras
# --------------------------------------------------------------------------

class Webcam:
    def __init__(self, index: int, width: int = 1920, height: int = 1080):
        self.cap = cv2.VideoCapture(index)
        if not self.cap.isOpened():
            raise RuntimeError(f"cannot open camera index {index}")
        self.cap.set(cv2.CAP_PROP_FRAME_WIDTH, width)
        self.cap.set(cv2.CAP_PROP_FRAME_HEIGHT, height)
        # Small buffer so we get a CURRENT frame, not a stale queued one.
        self.cap.set(cv2.CAP_PROP_BUFFERSIZE, 1)

    def grab(self) -> np.ndarray | None:
        self.cap.read()  # flush one stale frame
        ok, frame = self.cap.read()
        return frame if ok else None

    def close(self):
        self.cap.release()


class MockCamera:
    """Draws a synthetic 'print' that grows with layer number, plus noise.

    Enough to exercise the whole pipeline -- directory layout, burst pick,
    CSV, registration check -- with no printer and no webcam.
    """

    def __init__(self):
        self.layer = 0

    def grab(self) -> np.ndarray:
        h, w = 480, 640
        img = np.full((h, w, 3), 40, np.uint8)
        cv2.rectangle(img, (180, 380), (460, 400), (90, 90, 95), -1)  # "bed"
        ph = min(self.layer * 3, 240)
        if ph > 0:
            cv2.rectangle(img, (270, 380 - ph), (370, 380), (30, 110, 200), -1)
        img = cv2.add(img, np.random.randint(0, 12, img.shape, dtype=np.uint8))
        return img

    def close(self):
        pass


# --------------------------------------------------------------------------
# run directory
# --------------------------------------------------------------------------

class Run:
    def __init__(self, root: pathlib.Path, name: str, meta: dict):
        ts = datetime.now().strftime("%Y%m%dT%H%M%S")
        self.dir = root / f"{ts}_{slug(name)}"
        (self.dir / "frames").mkdir(parents=True, exist_ok=True)
        self.telemetry = open(self.dir / "telemetry.jsonl", "a", buffering=1)
        self.frames_csv = open(self.dir / "frames.csv", "a", newline="")
        self.writer = csv.writer(self.frames_csv)
        self.writer.writerow(["layer", "iso_time", "unix_time", "path",
                              "sharpness", "gcode_state", "nozzle_temper",
                              "bed_temper"])
        self.frames_csv.flush()
        (self.dir / "meta.json").write_text(json.dumps(meta, indent=2))
        log.info("run -> %s", self.dir)

    def log_report(self, patch: dict) -> None:
        self.telemetry.write(json.dumps(
            {"t": now_iso(), "patch": patch}, separators=(",", ":")) + "\n")

    def log_frame(self, row: list) -> None:
        self.writer.writerow(row)
        self.frames_csv.flush()

    def close(self):
        self.telemetry.close()
        self.frames_csv.close()


# --------------------------------------------------------------------------
# recorder
# --------------------------------------------------------------------------

class Recorder:
    def __init__(self, camera, out: pathlib.Path, settle: float,
                 burst: int, camera_desc: str):
        self.camera = camera
        self.out = out
        self.settle = settle
        self.burst = burst
        self.camera_desc = camera_desc
        self.run: Run | None = None
        self.lock = threading.Lock()
        self._last_gcode_state: str | None = None

    def on_state(self, state: dict, patch: dict) -> None:
        gs = state.get("gcode_state")

        # New print -> new run dir. FINISH/FAILED -> close it.
        if gs != self._last_gcode_state:
            log.info("gcode_state: %s -> %s", self._last_gcode_state, gs)
            if gs in ("RUNNING",) and self.run is None:
                self.run = Run(self.out,
                               state.get("subtask_name") or "print",
                               {"started": now_iso(),
                                "camera": self.camera_desc,
                                "settle_s": self.settle,
                                "burst": self.burst,
                                "gcode_file": state.get("gcode_file"),
                                "subtask_name": state.get("subtask_name"),
                                "total_layer_num": state.get("total_layer_num")})
            elif gs in ("FINISH", "FAILED", "IDLE") and self.run is not None:
                log.info("closing run (%s)", gs)
                self.run.close()
                self.run = None
            self._last_gcode_state = gs

        if self.run:
            self.run.log_report(patch)

        for h in patch.get("hms", []) or []:
            log.warning("HMS %s", decode_hms(h.get("attr", 0), h.get("code", 0)))

    def on_layer(self, layer: int, state: dict) -> None:
        if self.run is None:
            return
        threading.Thread(target=self._capture, args=(layer, state),
                         daemon=True).start()

    def _capture(self, layer: int, state: dict) -> None:
        time.sleep(self.settle)
        if isinstance(self.camera, MockCamera):
            self.camera.layer = layer
        with self.lock:  # one grabber at a time; layers can arrive fast
            best, best_s = None, -1.0
            for _ in range(self.burst):
                f = self.camera.grab()
                if f is None:
                    continue
                s = sharpness(f)
                if s > best_s:
                    best, best_s = f, s
            if best is None:
                log.error("layer %d: no frame", layer)
                return
            run = self.run
            if run is None:
                return
            rel = f"frames/layer_{layer:04d}.jpg"
            cv2.imwrite(str(run.dir / rel), best,
                        [cv2.IMWRITE_JPEG_QUALITY, 95])
            run.log_frame([layer, now_iso(), f"{time.time():.3f}", rel,
                           f"{best_s:.1f}", state.get("gcode_state"),
                           state.get("nozzle_temper"),
                           state.get("bed_temper")])
        log.info("layer %4d captured (sharpness %.0f)", layer, best_s)


# --------------------------------------------------------------------------
# mock driver
# --------------------------------------------------------------------------

def run_mock(rec: Recorder, layers: int, period: float) -> None:
    """Fake a print so the pipeline can be exercised with no hardware."""
    log.info("MOCK: synthesising a %d-layer print", layers)
    rec.on_state({"gcode_state": "RUNNING", "subtask_name": "mock_benchy",
                  "gcode_file": "mock.gcode", "total_layer_num": layers},
                 {"gcode_state": "RUNNING"})
    for n in range(1, layers + 1):
        st = {"gcode_state": "RUNNING", "layer_num": n,
              "total_layer_num": layers,
              "mc_percent": int(100 * n / layers),
              "nozzle_temper": 220.0 + np.random.randn(),
              "bed_temper": 60.0 + np.random.randn() * 0.3}
        rec.on_state(st, {"layer_num": n})
        rec.on_layer(n, st)
        time.sleep(period)
    time.sleep(rec.settle + 0.5)
    rec.on_state({"gcode_state": "FINISH"}, {"gcode_state": "FINISH"})


# --------------------------------------------------------------------------

def main() -> int:
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--host", help="printer IP")
    p.add_argument("--serial", help="printer serial (on the screen / in Studio)")
    p.add_argument("--access-code", help="8-char LAN access code")
    p.add_argument("--camera", type=int, default=0, help="USB camera index")
    p.add_argument("--out", type=pathlib.Path, default=pathlib.Path("runs"))
    p.add_argument("--settle", type=float, default=1.5,
                   help="seconds to wait after layer change before grabbing")
    p.add_argument("--burst", type=int, default=3,
                   help="frames per layer; sharpest is kept")
    p.add_argument("--mock", action="store_true",
                   help="no printer, no camera: synthesise a print")
    p.add_argument("--mock-layers", type=int, default=12)
    p.add_argument("--mock-period", type=float, default=0.15)
    a = p.parse_args()

    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)-7s %(message)s",
                        datefmt="%H:%M:%S")
    a.out.mkdir(parents=True, exist_ok=True)

    if a.mock:
        rec = Recorder(MockCamera(), a.out, settle=0.05, burst=a.burst,
                       camera_desc="mock")
        run_mock(rec, a.mock_layers, a.mock_period)
        return 0

    missing = [f for f in ("host", "serial", "access_code")
               if not getattr(a, f)]
    if missing:
        p.error("need --" + ", --".join(m.replace("_", "-") for m in missing)
                + "  (or use --mock)")

    cam = Webcam(a.camera)
    rec = Recorder(cam, a.out, a.settle, a.burst, f"usb:{a.camera}")
    link = BambuLink(a.host, a.serial, a.access_code,
                     on_state=rec.on_state, on_layer=rec.on_layer)

    if not link.connect():
        log.error("no connect. Check: LAN-only Mode ON, Developer Mode ON, "
                  "access code correct, port 8883 reachable.")
        return 1
    log.info("logging. ctrl-c to stop.")
    try:
        while True:
            time.sleep(5)
    except KeyboardInterrupt:
        pass
    finally:
        link.disconnect()
        cam.close()
        if rec.run:
            rec.run.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
