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


def open_camera(index: int, width: int = 1280, height: int = 720):
    cap = cv2.VideoCapture(index)
    if not cap.isOpened():
        raise RuntimeError(f"cannot open camera index {index}")
    cap.set(cv2.CAP_PROP_FRAME_WIDTH, width)
    cap.set(cv2.CAP_PROP_FRAME_HEIGHT, height)
    cap.set(cv2.CAP_PROP_BUFFERSIZE, 1)  # always a current frame, not a stale one
    return cap


def mock_infer(frame):
    """No camera, no weights: draw a box and always 'see' spaghetti, so the
    server-side arm->10s->stop path can be exercised end to end."""
    annotated = frame.copy()
    cv2.rectangle(annotated, (4, 4), (frame.shape[1] - 4, frame.shape[0] - 4),
                  (0, 0, 255), 2)
    return ([{"cls": "spaghetti", "conf": 0.9,
              "box": [frame.shape[1] // 2, frame.shape[0] // 2, 8, 8]}], annotated)


def make_yolo_infer(weights, conf, imgsz, device):
    """Build the real inference closure. YOLO/torch import is deferred to here
    so importing detect.py (and its unit tests) never needs a CUDA runtime."""
    from ultralytics import YOLO  # noqa: E402  (heavy, lazy on purpose)
    model = YOLO(str(weights))
    names = model.names if isinstance(model.names, dict) else dict(enumerate(model.names))

    def infer(frame):
        result = model.predict(frame, conf=conf, imgsz=imgsz, device=device,
                               verbose=False)[0]
        return detections_from_result(result, names), result.plot()

    return infer


def detection_loop(grab, infer, out_dir, *, camera, conf, fps, stop_event,
                   clock=time.time):
    """Grab -> infer -> write, until stop_event is set. A None grab (dead
    camera) writes an error status and stops. fps<=0 disables throttling
    (tests)."""
    period = (1.0 / fps) if fps and fps > 0 else 0.0
    while not stop_event.is_set():
        t0 = clock()
        frame = grab()
        if frame is None:
            write_status(out_dir, build_status([], ts=clock(), fps=0.0,
                                               camera=camera, conf=conf,
                                               error=f"camera {camera} read failed"))
            return
        detections, annotated = infer(frame)
        dt = max(clock() - t0, 1e-6)
        write_frame(out_dir, annotated)
        write_status(out_dir, build_status(detections, ts=clock(),
                                           fps=round(1.0 / dt, 1), camera=camera,
                                           conf=conf))
        if period:
            slack = period - (clock() - t0)
            if slack > 0:
                stop_event.wait(slack)


def main() -> int:
    import torch  # local: only the real run needs it
    repo = pathlib.Path(__file__).resolve().parent
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--camera", type=int, default=0)
    p.add_argument("--conf", type=float, default=0.25)
    p.add_argument("--imgsz", type=int, default=640)
    p.add_argument("--weights",
                   default=str(repo / "runs" / "train" / "failure_detector"
                               / "weights" / "best.pt"))
    p.add_argument("--out", type=pathlib.Path, default=repo / "runs" / "_detect")
    p.add_argument("--fps", type=float, default=4.0)
    p.add_argument("--device", default="0" if torch.cuda.is_available() else "cpu")
    p.add_argument("--mock", action="store_true")
    a = p.parse_args()

    stop = threading.Event()
    if a.mock:
        size = (480, 640, 3)
        grab = lambda: np.full(size, 40, np.uint8)  # noqa: E731
        infer = mock_infer
    else:
        if not pathlib.Path(a.weights).exists():
            print(f"weights not found: {a.weights}", file=sys.stderr)
            return 1
        cap = open_camera(a.camera)

        def grab():
            cap.read()                 # flush one stale buffered frame
            ok, frame = cap.read()
            return frame if ok else None

        infer = make_yolo_infer(a.weights, a.conf, a.imgsz, a.device)
    try:
        detection_loop(grab, infer, a.out, camera=a.camera, conf=a.conf,
                       fps=a.fps, stop_event=stop)
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
