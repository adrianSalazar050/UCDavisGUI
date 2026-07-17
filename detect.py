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
import logging
import socket
import ssl
import struct

import cv2
import numpy as np

log = logging.getLogger("detect")

CAMERA_PORT = 6000

REPLACE_RETRIES = 5
REPLACE_RETRY_S = 0.05


def _atomic_write_bytes(path: pathlib.Path, data: bytes) -> None:
    """temp + os.replace in the same dir -> a reader never sees a half file
    (the store.py pattern). On Windows os.replace raises PermissionError if the
    destination is momentarily open by a reader (the server's StatusReader) or
    held by OneDrive sync, so retry the rename a few times before giving up."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=path.name,
                              suffix=".tmp")
    try:
        try:
            f = os.fdopen(fd, "wb")
        except BaseException:
            os.close(fd)
            raise
        with f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        last = None
        for _ in range(REPLACE_RETRIES):
            try:
                os.replace(tmp, path)
                return
            except PermissionError as e:  # Windows sharing violation, transient
                last = e
                time.sleep(REPLACE_RETRY_S)
        raise last
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


def _bambu_auth_packet(access_code: str) -> bytes:
    return (struct.pack("<IIII", 0x40, 0x3000, 0, 0)
            + b"bblp".ljust(32, b"\x00")
            + access_code.encode().ljust(32, b"\x00"))


def _default_connect(host: str, timeout: float):
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE   # printer's cert is self-signed, like MQTT
    raw = socket.create_connection((host, CAMERA_PORT), timeout=timeout)
    return ctx.wrap_socket(raw, server_hostname=host)


class BambuCameraSource:
    """Reads JPEG frames from a Bambu P1/A1-family camera (TCP 6000, TLS).

    grab() returns the next frame decoded to BGR, reconnecting once on a drop;
    returns None only if the reconnect also fails (the detection loop then
    writes an error status). The access code is passed in by the caller, NEVER
    read from argv.
    """

    def __init__(self, host: str, access_code: str, *, timeout: float = 8.0,
                 connect=_default_connect):
        self.host = host
        self._code = access_code
        self._timeout = timeout
        self._connect = connect
        self._sock = None

    def _open(self) -> None:
        self._close()
        s = self._connect(self.host, self._timeout)
        self._sock = s            # track before auth send so a handshake/auth
        s.settimeout(self._timeout)  # failure is still reachable by _close()
        s.sendall(_bambu_auth_packet(self._code))

    def _recv_exactly(self, n: int) -> bytes:
        buf = b""
        while len(buf) < n:
            chunk = self._sock.recv(n - len(buf))
            if not chunk:
                raise ConnectionError("camera stream closed")
            buf += chunk
        return buf

    def _read_frame(self):
        header = self._recv_exactly(16)
        size = int.from_bytes(header[:4], "little")
        if not (0 < size <= 20_000_000):
            raise ConnectionError(f"implausible frame size {size}")
        jpeg = self._recv_exactly(size)
        img = cv2.imdecode(np.frombuffer(jpeg, np.uint8), cv2.IMREAD_COLOR)
        if img is None:
            raise ConnectionError("failed to decode JPEG frame")
        return img

    def grab(self):
        for attempt in (1, 2):    # try, then one reconnect
            try:
                if self._sock is None:
                    self._open()
                return self._read_frame()
            except (OSError, ConnectionError) as e:
                log.warning("A1 camera grab failed (attempt %d): %s", attempt, e)
                self._close()
        return None

    def _close(self) -> None:
        if self._sock is not None:
            try:
                self._sock.close()
            except OSError:
                pass
            self._sock = None

    def close(self) -> None:
        self._close()


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
    p.add_argument("--source", choices=("a1", "webcam"), default="a1",
                   help="frame source: the printer's built-in camera or a USB webcam")
    p.add_argument("--host", default=None,
                   help="printer IP, required for --source a1")
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
        try:
            detection_loop(grab, infer, a.out, camera=a.camera, conf=a.conf,
                           fps=a.fps, stop_event=stop)
        except KeyboardInterrupt:
            pass
    else:
        if not pathlib.Path(a.weights).exists():
            print(f"weights not found: {a.weights}", file=sys.stderr)
            return 1
        cam = None
        if a.source == "a1":
            if not a.host:
                print("--source a1 requires --host", file=sys.stderr)
                return 1
            code = os.environ.get("BAMBU_ACCESS_CODE")
            if not code:
                print("--source a1 requires BAMBU_ACCESS_CODE in the environment",
                      file=sys.stderr)
                return 1
            cam = BambuCameraSource(a.host, code)
            grab = cam.grab
        else:
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
        finally:
            if cam is not None:
                cam.close()
            else:
                cap.release()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
