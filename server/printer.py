"""Printer state services for the dashboard.

PrinterService wraps BambuLink: keeps its merged state, timestamps reports,
reconnects in the background. MockPrinter fakes the same interface with an
endless synthetic print and writes real frame JPEGs so the whole dashboard
works with no hardware.

Both are duck-typed to the same interface, which is what lets registry.py
(Task 5) hold a `{serial: service}` map without caring which kind of service
a given entry is: start(), stop(), summary() -> dict, list_files(path) ->
list[dict], fetch_file(path) -> bytes, plus the identity attributes serial,
host, name, capture.
"""
from __future__ import annotations

import io
import logging
import pathlib
import sys
import threading
import time
import zipfile
from datetime import datetime

import cv2
import numpy as np

# bambu_link.py lives at the repo root, one level above this package.
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))
from bambu_link import BambuLink, decode_hms  # noqa: E402

from . import sdcard  # noqa: E402
from .sdcard import SdError  # noqa: E402
from .threemf import SLICE_INFO_PATH  # noqa: E402

log = logging.getLogger("server.printer")

STALE_S = 15.0   # connected but no report for this long -> "stale"
RETRY_S = 10.0   # MQTT reconnect attempt interval

# gcode_state values that mean "there is already a print on this machine".
# Starting another would be ignored at best; refuse instead of publishing
# blind into silence. A *stopped* print reports FAILED (verified on hardware),
# and FINISH/IDLE are likewise free -- none of those are busy.
BUSY_STATES = ("RUNNING", "PREPARE", "PAUSE", "PAUSED", "SLICING")


class PrinterBusy(RuntimeError):
    """Refused a command because the printer is disconnected or already
    printing. The API layer maps this to 409."""

SUMMARY_FIELDS = (
    "gcode_state", "layer_num", "total_layer_num", "mc_percent",
    "mc_remaining_time", "nozzle_temper", "nozzle_target_temper",
    "bed_temper", "bed_target_temper", "spd_lvl", "spd_mag",
    "print_error", "fail_reason", "subtask_name", "gcode_file",
)

ERR_UNREACHABLE = ("Unreachable — check the IP, and that LAN-only Mode is on")
ERR_NO_CONNACK = ("No response — the access code may be wrong (it rotates on "
                  "firmware updates), or Developer Mode is off")

# What --mock pretends is on the card. Mirrors the real layout: model files at
# the root, timelapse/ and cache/ subdirectories.
MOCK_TREE: dict[str, list[dict]] = {
    "/": [
        {"name": "timelapse", "is_dir": True, "size": None, "mtime": None},
        {"name": "cache", "is_dir": True, "size": None, "mtime": None},
        {"name": "Benchy.3mf", "is_dir": False, "size": 1048576,
         "mtime": "2026-07-16T13:05:00"},
        {"name": "calibration_cube.gcode.3mf", "is_dir": False, "size": 204800,
         "mtime": "2026-07-15T09:12:00"},
    ],
    "/timelapse": [
        {"name": "video_2026-07-16.mp4", "is_dir": False, "size": 8388608,
         "mtime": "2026-07-16T14:00:00"},
    ],
    "/cache": [
        {"name": "Benchy.gcode.3mf", "is_dir": False, "size": 1048576,
         "mtime": "2026-07-16T13:04:00"},
    ],
}

# What --mock hands back for ANY queue "Add from SD" fetch (MockPrinter.
# fetch_file, below) -- there is no per-file mock data, just one small
# in-memory .gcode.3mf, built once at import time. The numbers echo the real
# printer sample confirmed in the print-queue plan (smallCylinderPLA15m17s ->
# 917s / 1.69g) purely so a mock screenshot reads the same as a real one.
_MOCK_SLICE_INFO = (
    '<?xml version="1.0" encoding="UTF-8"?>'
    '<config><plate>'
    '<metadata key="index" value="1"/>'
    '<metadata key="prediction" value="917"/>'
    '<metadata key="weight" value="1.69"/>'
    '<filament id="1" type="PLA" color="#000000" used_g="1.69" used_m="0.57"/>'
    '</plate></config>'
)


def _build_mock_3mf() -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr(SLICE_INFO_PATH, _MOCK_SLICE_INFO)
    return buf.getvalue()


MOCK_3MF_BYTES = _build_mock_3mf()


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

    def __init__(self, host: str, serial: str, access_code: str,
                 name: str = "", capture: bool = False):
        self.host = host
        self.serial = serial
        self.access_code = access_code
        self.name = name or host
        self.capture = capture
        self.link = BambuLink(host, serial, access_code,
                              on_state=self._on_state)
        self._last_report: float | None = None
        self._last_error: str | None = None
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
        #
        # Which of those two happened is the ONLY signal distinguishing a bad
        # access code from an unplugged printer, so it is recorded rather than
        # only logged.
        while not self._stop.is_set():
            try:
                if self.link.connect(timeout=5):
                    self._last_error = None
                    return
                self._last_error = ERR_NO_CONNACK
                log.warning(
                    "MQTT reached %s but no CONNACK within 5s (wrong access "
                    "code, or Developer Mode off?). paho keeps retrying in "
                    "the background.", self.host)
                return
            except Exception as e:
                self._last_error = ERR_UNREACHABLE
                log.warning("MQTT connect to %s failed: %s (retry in %ss)",
                            self.host, e, RETRY_S)
            self._stop.wait(RETRY_S)

    def list_files(self, path: str = "/") -> list[dict]:
        """Blocking FTPS call. MUST NOT be called from the event loop — the
        route that uses it is a sync def, so FastAPI runs it on a threadpool."""
        return sdcard.list_dir(self.host, self.access_code, path)

    def fetch_file(self, path: str) -> bytes:
        """Blocking FTPS call, same posture as list_files: MUST NOT be
        called from the event loop. Thin delegation only -- access_code
        never leaves this method, exactly like list_files."""
        return sdcard.fetch_file(self.host, self.access_code, path)

    def summary(self) -> dict:
        age = (None if self._last_report is None
               else time.time() - self._last_report)
        connected = self.link.connected.is_set()
        return build_summary(
            self._snapshot, age, connected, self.host,
            serial=self.serial, name=self.name, capture=self.capture,
            last_error=None if connected else self._last_error)

    def stop_print(self) -> None:
        """Fire-and-verify: publishing can't fail loudly (no ack), so callers
        confirm via gcode_state. See BambuLink.stop_print."""
        self.link.stop_print()

    def start_print(self, sd_path: str, *, plate: int = 1, **options) -> None:
        """Start an SD-card file. Guards first, because MQTT has no ack: if we
        published blind into a disconnected link or on top of a running print,
        the caller would get silence and assume success.

        Raises PrinterBusy so the API layer can turn it into a 409.
        """
        if not self.link.connected.is_set():
            raise PrinterBusy(f"{self.name} is not connected")
        gstate = (self._snapshot.get("gcode_state") or "").upper()
        if gstate in BUSY_STATES:
            raise PrinterBusy(f"{self.name} is already printing ({gstate})")
        self.link.start_print(sd_path, plate=plate, **options)


class MockPrinter:
    """Endless fake print for developing the GUI with no hardware.

    mode="running": RUNNING (one layer every LAYER_PERIOD_S, temps wander, an
    HMS code appears during HMS_LAYERS) -> FINISH -> IDLE_S -> new run. Frames
    are written as real JPEGs into a real run directory so /api/frame/latest is
    exercised too.
    mode="stale":   reports once, long ago, then never again -> "stale".
    mode="offline": never connects -> "disconnected" + last_error.

    The three modes exist so --mock can seed an Overview grid that actually
    shows all three states.
    """

    LAYERS = 30
    LAYER_PERIOD_S = 2.0
    IDLE_S = 10.0
    HMS_LAYERS = range(12, 17)

    def __init__(self, runs_dir: pathlib.Path, serial: str = "MOCK0000000000",
                 host: str = "MOCK", name: str = "", capture: bool = False,
                 mode: str = "running"):
        self.runs_dir = runs_dir
        self.serial = serial
        self.host = host
        self.name = name or host
        self.capture = capture
        self.mode = mode
        self.state: dict = {"gcode_state": "IDLE"}
        self._last_report: float | None = None
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._loop, daemon=True)

    def start(self) -> None:
        if self.mode == "running":
            self._thread.start()
        elif self.mode == "stale":
            self._touch({"gcode_state": "RUNNING", "subtask_name": "stalled",
                         "layer_num": 7, "total_layer_num": 30,
                         "mc_percent": 23})
            # Backdate the report so it reads as stale immediately.
            self._last_report = time.time() - (STALE_S + 5)
        # "offline": never connects, nothing to start.

    def stop(self) -> None:
        self._stop.set()
        if self._thread.is_alive():
            self._thread.join(timeout=2)

    def summary(self) -> dict:
        age = (None if self._last_report is None
               else time.time() - self._last_report)
        connected = self.mode != "offline"
        return build_summary(
            self.state, age, connected, self.host,
            serial=self.serial, name=self.name, capture=self.capture,
            last_error=None if connected else ERR_UNREACHABLE)

    def stop_print(self) -> None:
        self._touch({"gcode_state": "FAILED"})

    def start_print(self, sd_path: str, *, plate: int = 1, **options) -> None:
        """Same guards and the same observable transition as the real service,
        so --mock exercises the queue's start -> verify -> dequeue path end to
        end. Goes straight to RUNNING; the real printer passes through PREPARE
        first, and the API's verify accepts either."""
        if self.mode == "offline":
            raise PrinterBusy(f"{self.name} is not connected")
        gstate = (self.state.get("gcode_state") or "").upper()
        if gstate in BUSY_STATES:
            raise PrinterBusy(f"{self.name} is already printing ({gstate})")
        self._touch({"gcode_state": "RUNNING", "layer_num": 0,
                     "mc_percent": 0,
                     "subtask_name": sd_path.rsplit("/", 1)[-1].split(".")[0]})

    def list_files(self, path: str = "/") -> list[dict]:
        target = sdcard.normalize_path(path)  # raises SdError on traversal
        try:
            entries = MOCK_TREE[target]
        except KeyError:
            raise SdError(f"Could not list {target} on {self.host}: "
                          "no such directory") from None
        # Copy each entry, not just the outer list: MOCK_TREE is a shared
        # module-level constant, and a caller mutating a returned dict must
        # not corrupt it for every other printer/instance for the rest of the
        # process's life.
        return sdcard.sort_entries([dict(e) for e in entries])

    def fetch_file(self, path: str) -> bytes:
        """No per-file mock data -- every mock SD path fetches the same
        built-in fixture 3mf (MOCK_3MF_BYTES), so --mock's queue "Add from
        SD" flow yields real-looking time/grams with zero hardware."""
        sdcard.normalize_path(path)  # traversal guard, same posture as list_files
        return MOCK_3MF_BYTES

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
