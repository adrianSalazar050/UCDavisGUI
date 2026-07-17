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
