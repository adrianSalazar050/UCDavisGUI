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
                  connected: bool, printer: str, *,
                  serial: str = "", name: str = "", capture: bool = False,
                  last_error: str | None = None) -> dict:
    """Curate the merged printer state into the payload the UI consumes.

    Fields the printer hasn't reported yet are null — it sends partial
    updates, so early in a session most fields are unknown.

    Identity is keyword-only so the v1 positional call signature still reads
    the same. `access_code` is deliberately NOT a parameter: nothing about the
    password should be able to reach a payload by accident.
    """
    out = {k: state.get(k) for k in SUMMARY_FIELDS}
    hms_codes = []
    for h in state.get("hms") or []:
        # hms comes from printer-controlled MQTT JSON; one malformed entry
        # must not take down every future summary() call.
        if not isinstance(h, dict):
            continue
        try:
            hms_codes.append(decode_hms(int(h.get("attr", 0)),
                                        int(h.get("code", 0))))
        except (TypeError, ValueError):
            continue
    out["hms"] = hms_codes
    if not connected:
        conn = "disconnected"
    elif report_age is None or report_age > STALE_S:
        conn = "stale"
    else:
        conn = "ok"
    out["connection"] = conn
    out["report_age_s"] = None if report_age is None else round(report_age, 1)
    out["printer"] = printer
    out["serial"] = serial
    out["name"] = name
    out["capture"] = capture
    out["last_error"] = last_error
    return out


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
        self._snapshot: dict = {}
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._connect_loop,
                                        daemon=True)

    def _on_state(self, state: dict, patch: dict) -> None:
        # `state` is the deep-copied snapshot BambuLink built under its lock;
        # keeping it (instead of re-reading link.state later) means summary()
        # never races the MQTT thread's deep_merge writes.
        self._snapshot = state
        self._last_report = time.time()

    def start(self) -> None:
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        self.link.disconnect()
        if self._thread.is_alive():
            self._thread.join(timeout=2)

    def _connect_loop(self) -> None:
        # BambuLink.connect() raising means paho's network loop never
        # started -> retrying is ours to do. connect() returning False means
        # loop_start() ran and paho now retries forever on its own;
        # re-driving connect() from this thread would race paho's thread on
        # the same socket, so we log and hand off.
        while not self._stop.is_set():
            try:
                if self.link.connect(timeout=5):
                    return
                log.warning(
                    "MQTT reached %s but no CONNACK within 5s (wrong access "
                    "code, or Developer Mode off?). paho keeps retrying in "
                    "the background.", self.host)
                return
            except Exception as e:
                log.warning("MQTT connect to %s failed: %s (retry in %ss)",
                            self.host, e, RETRY_S)
            self._stop.wait(RETRY_S)

    def summary(self) -> dict:
        age = (None if self._last_report is None
               else time.time() - self._last_report)
        return build_summary(self._snapshot, age,
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
        if self._thread.is_alive():
            self._thread.join(timeout=2)

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
