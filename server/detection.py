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


def out_dir_for(root, serial: str) -> pathlib.Path:
    """Where one printer's detector writes status.json + latest.jpg.

    Per-serial, because there is one detector process per camera printer and
    they would otherwise overwrite each other's two files -- every printer
    would show whichever detector wrote last, which is the same class of lie
    the `capture` flag exists to prevent.

    The serial goes in the path unescaped on purpose: it comes from
    PrinterConfig, which requires a non-empty serial, and Bambu serials are
    alphanumeric. It is never taken from a URL.
    """
    return pathlib.Path(root) / serial


class DetectorSupervisor:
    """Keeps one detect.py subprocess per desired target, keyed by serial.
    Injectable spawn/clock make it testable with no real process.

    Was "exactly one subprocess" until 2026-08-05. Each printer has its own
    built-in camera on its own address, so N camera printers are N independent
    streams and N independent detectors; the one-process-per-*device* rule
    (section 2) is unaffected, since no two of these ever open the same device.
    """

    def __init__(self, out_dir, weights, *, python=sys.executable,
                 script=None, spawn=subprocess.Popen, clock=time.time,
                 backoff_s: float = 5.0, interval_s: float = None):
        self._out_dir = pathlib.Path(out_dir)
        self._weights = weights
        self._python = python
        self._script = script or str(
            pathlib.Path(__file__).resolve().parent.parent / "detect.py")
        self._spawn_fn = spawn
        self._clock = clock
        self._backoff_s = backoff_s
        self._interval_s = (DEFAULT_INTERVAL_S if interval_s is None
                            else interval_s)
        # serial -> the target dict it was spawned for / its Popen / when.
        # Three dicts rather than one dict of records because reconcile()
        # compares targets, polls procs and checks backoff independently.
        self._targets = {}
        self._procs = {}
        self._last_spawn = {}

    def build_argv(self, target) -> list:
        # NB: never the access code -- that goes in build_env for a1.
        argv = [self._python, self._script, "--source", target["camera_source"],
                "--conf", str(target["conf"]), "--weights", str(self._weights),
                "--out", str(out_dir_for(self._out_dir, target["serial"])),
                "--interval", str(self._interval_s)]
        if target["camera_source"] == "a1":
            argv += ["--host", target["host"]]
        else:
            argv += ["--camera", str(target["camera_index"])]
        roi = target.get("roi")
        if roi:
            argv += ["--roi", ",".join(str(v) for v in roi)]
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
        serial = target["serial"]
        self._procs[serial] = self._spawn_fn(self.build_argv(target),
                                             env=self.build_env(target))
        self._last_spawn[serial] = self._clock()

    def _stop_proc(self, serial) -> None:
        proc = self._procs.pop(serial, None)
        if proc is not None:
            try:
                proc.terminate()
                # terminate() only REQUESTS the exit. Until the process is
                # actually gone it still holds the camera, so a respawn that
                # does not wait finds the device busy, dies with "cannot open
                # camera index N", and gets respawned again -- a flapping loop
                # that looks like the camera reconnecting over and over.
                # (Also true of a printer's built-in camera, which accepts one
                # client at a time.)
                proc.wait(timeout=TERMINATE_TIMEOUT_S)
            except Exception as e:  # noqa: BLE001 - incl. TimeoutExpired
                log.warning("detector terminate failed for %s: %s", serial, e)

    def _drop(self, serial) -> None:
        self._stop_proc(serial)
        self._targets.pop(serial, None)
        self._last_spawn.pop(serial, None)

    def reconcile(self, targets) -> None:
        """Converge on `targets` (a list of target dicts, one per printer).

        Three cases per serial, each handled independently: gone (stop it),
        changed (stop and respawn with the new settings), unchanged but dead
        (respawn once the backoff has elapsed). A printer whose target did not
        change is never touched -- restarting one detector must not disturb the
        others, since each restart drops a camera connection and loses a frame.
        """
        wanted = {t["serial"]: t for t in (targets or [])}

        for serial in list(self._targets):
            if serial not in wanted:
                self._drop(serial)

        for serial, target in wanted.items():
            if self._targets.get(serial) != target:
                self._stop_proc(serial)
                self._targets[serial] = target
                self._spawn(target)
                continue
            proc = self._procs.get(serial)
            if proc is not None and proc.poll() is not None:
                if self._clock() - self._last_spawn.get(serial, 0.0) >= self._backoff_s:
                    log.warning("detector for %s exited; respawning", serial)
                    self._spawn(target)

    def stop(self) -> None:
        for serial in list(self._targets):
            self._drop(serial)


DETECT_SUBDIR = "_detect"
TICK_S = 0.5

# Seconds between detector captures. Kept in step with detect.DEFAULT_INTERVAL_S
# but defined here so importing the server never pulls in cv2/torch.
DEFAULT_INTERVAL_S = 5.0

# How long to wait for a terminated detector to actually exit and release the
# camera before giving up on it.
TERMINATE_TIMEOUT_S = 5.0

# status.json is only rewritten once per capture, so the freshness window has to
# span more than one interval or a healthy detector reads as "down" between
# every frame -- which would feed [] to the controller and silently disable
# auto-stop. Two missed captures plus a margin.
STALE_INTERVALS = 2.5
MIN_STALE_S = 3.0


class DetectionCoordinator:
    """Background thread: reconcile the detector, read status, run the
    controller, actuate the stop. Also serves detection snapshots to the API/WS.
    """

    def __init__(self, registry, runs_dir, runner, *, tick_s: float = TICK_S,
                 controller_factory=AutoStopController,
                 interval_s: float = DEFAULT_INTERVAL_S):
        self.registry = registry
        self.out_dir = pathlib.Path(runs_dir) / DETECT_SUBDIR
        self.runner = runner
        self._stale_after = max(MIN_STALE_S, interval_s * STALE_INTERVALS)
        # serial -> StatusReader / last status / AutoStopController. All three
        # are per camera printer: each detector writes its own status.json
        # under its own directory (out_dir_for), and each printer gets its own
        # arm state and fault timer, so arming one machine never arms another.
        self._readers = {}
        self._last_status = {}
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

    def _reader_for(self, serial):
        r = self._readers.get(serial)
        if r is None:
            r = StatusReader(out_dir_for(self.out_dir, serial),
                             stale_after=self._stale_after)
            self._readers[serial] = r
        return r

    def tick(self) -> None:
        self.runner.reconcile(self.registry.detection_targets())
        caps = set(self.registry.capture_serials())
        with self._lock:
            # Drop controllers and readers for any printer that is no longer a
            # camera printer. Arming and the fault timer are meaningful only
            # while a camera actually watches the machine; a controller left
            # frozen mid-fault would otherwise fire on the first frame after
            # the camera comes back (a stale fault_since bypassing the sustain
            # debounce). Coming back disarmed matches "arm is runtime-only" --
            # losing the camera is like a restart for that printer.
            for serial in list(self._controllers):
                if serial not in caps:
                    del self._controllers[serial]
            for serial in list(self._readers):
                if serial not in caps:
                    del self._readers[serial]
                    self._last_status.pop(serial, None)
        for cap in caps:
            self._tick_printer(cap)

    def _tick_printer(self, cap) -> None:
        """One camera printer's read -> decide -> actuate.

        Per printer, and deliberately tolerant: an exception raised for one
        machine must not stop the others from being checked on this tick, which
        is why the caller's loop calls this rather than inlining it.
        """
        cfg = self.registry.detection_config(cap)
        if cfg is None:
            return
        status = self._reader_for(cap).read()
        # NEVER act on stale/errored detections -- feed the controller [] so a
        # dead detector can't leave a stale 'spaghetti' ticking toward a stop.
        detections = status["detections"] if status["running"] else []
        svc = self.registry.get(cap)
        gstate = svc.summary().get("gcode_state") if svc else None
        with self._lock:
            self._last_status[cap] = status
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
        if serial not in self.registry.capture_serials():
            return None
        cfg = self.registry.detection_config(serial)
        if cfg is None:
            return None
        with self._lock:
            # A printer just marked as a camera printer has no status yet --
            # "down" is the honest answer until its detector's first write,
            # never another printer's status.
            status = self._last_status.get(serial) or self._reader_for(serial)._down()
            snap = self._controller_for(serial).snapshot()
        return {"running": status["running"], "fps": status["fps"],
                "camera_source": cfg["camera_source"],
                "camera_index": cfg["camera_index"], "conf": cfg["conf"],
                # roi was missing from this payload from the start, while
                # PUT /detection accepted it and PrinterConfig stored it. The
                # UI seeds its editor from d.roi, so a saved region never came
                # back: the four % inputs and the draggable box always showed
                # the hardcoded A1 default, "Use whole frame" (disabled on
                # !d.roi) was permanently dead, and -- worst -- the page tells
                # the operator the draggable box and the outline burned into
                # the JPEG "match once you hit Apply". They visibly did not,
                # so the fix was to re-Apply, overwriting a correct region
                # with the default. On an A1 mini that default crops the bed
                # out of frame entirely (section 4.1's silent false negative).
                "roi": cfg["roi"],
                "detect_enabled": cfg["detect_enabled"],
                "armed": snap["armed"], "armed_classes": cfg["armed_classes"],
                "detections": status["detections"] if status["running"] else [],
                "stopped_by_monitor": snap["stopped_by_monitor"],
                "seconds_to_stop": snap["seconds_to_stop"],
                "error": status["error"]}

    def arm(self, serial, value: bool) -> None:
        with self._lock:
            self._controller_for(serial).arm(value)

    def frame_path(self, serial):
        """The annotated JPEG for ONE printer, or None if it hasn't written yet.

        Takes a serial as of 2026-08-05. It used to ignore the one the route
        passed and return a single global path, which was harmless while only
        one printer could have a camera and would now serve whichever detector
        wrote last under every printer's name.
        """
        p = out_dir_for(self.out_dir, serial) / "latest.jpg"
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
    write a synthetic 'spaghetti' status.json + annotated frame so the
    arm->10s->stop loop AND the live camera view both work with no camera and
    no weights. Reuses detect.mock_infer/write_* for the same contracts.

    One writer thread per target, into the same per-serial directories the real
    supervisor uses, so --mock exercises the multi-camera paths too: mark two
    mock printers as camera printers and both show a live view.
    """

    def __init__(self, out_dir, *, period_s: float = 0.5):
        self.out_dir = pathlib.Path(out_dir)
        self._period = period_s
        # serial -> (thread, stop_event). One event per thread, not one shared:
        # stopping one printer's writer must not stop the others'.
        self._writers = {}

    def reconcile(self, targets) -> None:
        wanted = {t["serial"] for t in (targets or [])}
        for serial in list(self._writers):
            if serial not in wanted:
                self._halt(serial)
        for serial in wanted:
            if serial not in self._writers:
                stop = threading.Event()
                thread = threading.Thread(target=self._loop,
                                          args=(serial, stop), daemon=True)
                self._writers[serial] = (thread, stop)
                thread.start()

    def _loop(self, serial, stop) -> None:
        import detect  # root module; lazy so server imports don't need cv2 early
        import numpy as np
        out = out_dir_for(self.out_dir, serial)
        while not stop.wait(self._period):
            try:
                base = np.full((360, 640, 3), 40, np.uint8)
                dets, annotated = detect.mock_infer(base)  # spaghetti + red box
                detect.write_frame(out, annotated)
                detect.write_status(out, detect.build_status(
                    dets, ts=time.time(), fps=4.0, camera=0, conf=0.25))
            except Exception as e:  # noqa: BLE001 - no supervisor to respawn
                # this thread; one bad write (even after H1's retries are
                # exhausted) must not permanently kill the mock writer.
                log.warning("mock detector write failed for %s: %s", serial, e)

    def _halt(self, serial) -> None:
        entry = self._writers.pop(serial, None)
        if entry is None:
            return
        thread, stop = entry
        stop.set()
        if thread.is_alive():
            thread.join(timeout=2)

    def stop(self) -> None:
        for serial in list(self._writers):
            self._halt(serial)
