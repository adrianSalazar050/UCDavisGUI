"""Locate the newest captured frame under a runs/ directory.

capture.py writes runs/<ts>_<name>/frames/layer_NNNN.jpg. The dashboard
serves the newest of those instead of opening the webcam, so it can never
steal the camera from capture.py (Windows allows only one process per
camera device).
"""
from __future__ import annotations

import pathlib
import re
import time

FRAME_RE = re.compile(r"^layer_(\d{1,6})\.jpg$")
ACTIVE_WINDOW_S = 30 * 60  # a run is "active" if it wrote a frame this recently


def find_active_run(runs_dir: pathlib.Path,
                    now: float | None = None) -> pathlib.Path | None:
    """Run dir whose frames/ has the most recently modified layer_*.jpg,
    if that write is within ACTIVE_WINDOW_S. Else None."""
    now = time.time() if now is None else now
    best_dir, best_mtime = None, -1.0
    if not runs_dir.is_dir():
        return None
    for run in runs_dir.iterdir():
        frames = run / "frames"
        if not frames.is_dir():
            continue
        for f in frames.iterdir():
            if not FRAME_RE.match(f.name):
                continue
            mtime = f.stat().st_mtime
            if mtime > best_mtime:
                best_dir, best_mtime = run, mtime
    if best_dir is None or now - best_mtime > ACTIVE_WINDOW_S:
        return None
    return best_dir


def newest_frame(runs_dir: pathlib.Path, now: float | None = None) -> dict | None:
    """{"path": Path, "layer": int, "run": str} for the highest-numbered
    frame of the active run, or None."""
    run = find_active_run(runs_dir, now)
    if run is None:
        return None
    best: tuple[int, pathlib.Path] | None = None
    for f in (run / "frames").iterdir():
        m = FRAME_RE.match(f.name)
        if not m:
            continue
        layer = int(m.group(1))
        if best is None or layer > best[0]:
            best = (layer, f)
    if best is None:
        return None
    return {"path": best[1], "layer": best[0], "run": run.name}
