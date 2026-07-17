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
