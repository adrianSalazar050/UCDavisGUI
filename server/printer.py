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
