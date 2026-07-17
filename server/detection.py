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
