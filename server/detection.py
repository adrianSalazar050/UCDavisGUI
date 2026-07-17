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
import os
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
        except (OSError, UnicodeDecodeError, json.JSONDecodeError):
            # UnicodeDecodeError: a torn write caught mid multi-byte char
            # (OneDrive sync, a read racing detect.py's os.replace) -- degrade
            # to "down" like any other bad read (matches store.py's tolerance).
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
        def _conf(d):
            try:
                return float(d.get("conf", 0.0))
            except (TypeError, ValueError):
                return 0.0  # malformed conf (null/non-numeric) -> fail-safe
        return any(d.get("cls") in self._classes and _conf(d) >= self._threshold
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
                 script=None, spawn=subprocess.Popen, clock=time.time,
                 backoff_s: float = 5.0, fps: float = 4.0):
        self._out_dir = pathlib.Path(out_dir)
        self._weights = weights
        self._python = python
        self._script = script or str(
            pathlib.Path(__file__).resolve().parent.parent / "detect.py")
        self._spawn_fn = spawn
        self._clock = clock
        self._backoff_s = backoff_s
        self._fps = fps
        self._target = None
        self._proc = None
        self._last_spawn = 0.0

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
        self._last_status = self.reader._down()
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
        with self._lock:
            # Drop controllers for any printer that is no longer the capture
            # printer. Arming and the fault timer are meaningful only for the
            # printer the single camera currently watches; a controller left
            # frozen mid-fault would otherwise fire on the first frame after the
            # camera is pointed back at it (a stale fault_since bypassing the
            # sustain debounce). Coming back disarmed matches "arm is runtime-
            # only" -- a capture switch is like a restart for that printer.
            for serial in list(self._controllers):
                if serial != cap:
                    del self._controllers[serial]
        if cap is None:
            return
        cfg = self.registry.detection_config(cap)
        if cfg is None:
            return
        status = self.reader.read()
        # NEVER act on stale/errored detections -- feed the controller [] so a
        # dead detector can't leave a stale 'spaghetti' ticking toward a stop.
        detections = status["detections"] if status["running"] else []
        svc = self.registry.get(cap)
        gstate = svc.summary().get("gcode_state") if svc else None
        with self._lock:
            self._last_status = status
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
        with self._lock:
            status = self._last_status
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
            try:
                detect.write_status(self.out_dir, detect.build_status(
                    [{"cls": "spaghetti", "conf": 0.9, "box": [0, 0, 8, 8]}],
                    ts=time.time(), fps=4.0, camera=0, conf=0.25))
            except Exception as e:  # noqa: BLE001 - no supervisor to respawn
                # this thread; one bad write (even after H1's retries are
                # exhausted) must not permanently kill the mock writer.
                log.warning("mock detector write_status failed: %s", e)

    def _halt(self) -> None:
        self._stop.set()
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=2)
        self._active = False

    def stop(self) -> None:
        self._halt()
