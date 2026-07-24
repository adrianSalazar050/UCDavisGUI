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

# Seconds between captures. One frame every 5 s is plenty for spotting a print
# failure (spaghetti takes tens of seconds to develop) and it keeps USB
# bandwidth and GPU load near zero.
DEFAULT_INTERVAL_S = 5.0

# A USB webcam drops the occasional frame -- bandwidth contention, an
# auto-exposure re-lock, a driver hiccup while YOLO holds the CPU. That is
# NORMAL and must not be mistaken for an unplugged camera: give up only after
# this many consecutive misses, and retry quickly in between.
MAX_READ_FAILURES = 3
READ_RETRY_S = 0.5


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


def _safe_write(write_fn, out_dir, payload) -> None:
    """Call a write_* function, tolerating a transient FS error -- notably the
    Windows/OneDrive os.replace sharing-violation when the repo is in a synced
    folder. A dropped frame/status is fine (the next tick rewrites); a
    *persistent* failure surfaces as a stale status (the server marks the
    detector down) instead of crash-looping the process."""
    try:
        write_fn(out_dir, payload)
    except OSError as e:
        log.warning("detector write to %s failed (skipped): %s", out_dir, e)


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


# Colour of the ROI outline drawn on the served frame (BGR).
ROI_COLOR = (0, 200, 255)


def parse_roi(text):
    """"x,y,w,h" as fractions of the frame -> (x, y, w, h), or None.

    Fractions, not pixels, so the region survives a camera resolution change.
    Anything malformed, degenerate, or out of bounds degrades to None ("use the
    whole frame") -- a bad ROI must never crash the detector or, worse, silently
    crop the print out of view.
    """
    if not text:
        return None
    parts = str(text).split(",")
    if len(parts) != 4:
        return None
    try:
        x, y, w, h = (float(p) for p in parts)
    except (TypeError, ValueError):
        return None
    if w <= 0 or h <= 0:
        return None
    if x < 0 or y < 0 or x + w > 1 or y + h > 1:
        return None
    return (x, y, w, h)


def crop_to_roi(frame, roi):
    """frame, (x,y,w,h) fractions -> (crop, x0, y0) in pixels.

    roi=None returns the frame untouched at origin (0, 0), so callers need no
    special case. The crop is clamped to at least 1x1: an empty array would
    raise inside cv2/YOLO rather than simply detecting nothing.
    """
    if roi is None:
        return frame, 0, 0
    fh, fw = frame.shape[:2]
    x, y, w, h = roi
    x0, y0 = int(x * fw), int(y * fh)
    x1, y1 = min(fw, x0 + max(1, int(w * fw))), min(fh, y0 + max(1, int(h * fh)))
    x0, y0 = min(x0, fw - 1), min(y0, fh - 1)
    return frame[y0:y1, x0:x1], x0, y0


def offset_detections(dets, x0: int, y0: int) -> list:
    """Shift crop-relative boxes back into full-frame coordinates.

    box is xywh with a CENTER point, so only the centre moves; width and height
    are unchanged by a translation. Returns new dicts -- the caller's list is
    not mutated.
    """
    if not x0 and not y0:
        return dets
    out = []
    for d in dets:
        b = d.get("box") or [0, 0, 0, 0]
        out.append({**d, "box": [b[0] + x0, b[1] + y0, b[2], b[3]]})
    return out


def compose_frame(frame, annotated_crop, x0: int, y0: int, roi):
    """Full frame with the annotated crop pasted back and the ROI outlined.

    The operator keeps the whole view (so the ROI can be tuned by eye and
    nothing is hidden), while the model only ever saw the crop.
    """
    if roi is None:
        return annotated_crop
    out = frame.copy()
    ch, cw = annotated_crop.shape[:2]
    out[y0:y0 + ch, x0:x0 + cw] = annotated_crop
    cv2.rectangle(out, (x0, y0), (x0 + cw - 1, y0 + ch - 1), ROI_COLOR, 2)
    return out


def open_camera(index: int, width: int = 1280, height: int = 720):
    cap = cv2.VideoCapture(index)
    if not cap.isOpened():
        raise RuntimeError(f"cannot open camera index {index}")
    cap.set(cv2.CAP_PROP_FRAME_WIDTH, width)
    cap.set(cv2.CAP_PROP_FRAME_HEIGHT, height)
    cap.set(cv2.CAP_PROP_BUFFERSIZE, 1)  # always a current frame, not a stale one
    return cap


class WebcamSource:
    """Reads frames from a USB webcam, recovering from a dropped frame in
    process instead of letting one bad read kill the detector.

    Same contract as BambuCameraSource.grab(): a frame, or None only when the
    device is genuinely gone. Recovery is deliberately staged cheapest-first --
    read again, and only if that also fails release and reopen the device.
    Reopening is what the user SEES (Windows plays the USB chime, the webcam LED
    blinks), so it must be a last resort, never the routine path.
    """

    def __init__(self, index: int, *, width: int = 1280, height: int = 720,
                 open_fn=None):
        self.index = index
        self._width = width
        self._height = height
        self._open_fn = open_fn or open_camera
        self._cap = None

    def _open(self) -> None:
        self._close()
        self._cap = self._open_fn(self.index, self._width, self._height)

    def _read(self):
        self._cap.read()                 # flush one stale buffered frame
        ok, frame = self._cap.read()
        if not ok or frame is None:
            raise ConnectionError("camera read returned no frame")
        return frame

    def grab(self):
        # 1: plain read. 2: read again (rides out a single dropped frame).
        # 3: read from a freshly reopened device.
        for attempt in (1, 2, 3):
            try:
                if self._cap is None:
                    self._open()
                return self._read()
            except (OSError, ConnectionError, RuntimeError, cv2.error) as e:
                log.warning("webcam %d grab failed (attempt %d): %s",
                            self.index, attempt, e)
                if attempt >= 2:
                    self._close()        # force a reopen on the next attempt
        return None

    def _close(self) -> None:
        if self._cap is not None:
            try:
                self._cap.release()
            except Exception as e:  # noqa: BLE001 - release must never raise out
                log.warning("webcam release failed: %s", e)
            self._cap = None

    def close(self) -> None:
        self._close()


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


# --- ONNX backend ----------------------------------------------------------
# Runs the detector without PyTorch, so it works on a machine with no GPU and
# no 4.7 GB CUDA install (onnxruntime is 44 MB). A BACKEND SWAP, NOT A MODEL
# CHANGE -- everything master.md 12 says about detector quality still stands.
#
# THE ONE TRAP, measured 2026-07-23. ultralytics' predict() uses RECT inference:
# it letterboxes to a stride-aligned, aspect-preserving size (640x416 for a
# 1680x1080 frame). An exported ONNX graph has a fixed SQUARE input. Same
# detection, same weights: 0.312 on the torch path, 0.521 here. The runtimes
# agree to 0.001 when fed identical geometry -- the gap is the geometry. So a
# `conf` threshold tuned on one backend DOES NOT TRANSFER to the other, which
# matters because the deployed threshold is 0.25 and that example straddles it.

LETTERBOX_GREY = 114        # what ultralytics pads with; keep them identical
# ultralytics' own NMS default (DEFAULT_CFG.iou), not the 0.45 that most YOLO
# examples use. Measured 2026-07-23: at 0.45 this backend suppressed a second,
# overlapping box on a frame where the torch path kept it -- a lower IoU
# threshold suppresses MORE. Matching the value makes the two agree.
NMS_IOU = 0.7


def letterbox(frame, size: int, colour: int = LETTERBOX_GREY):
    """Resize `frame` into a square `size`x`size` canvas, preserving aspect
    ratio and padding the short axis. -> (padded, ratio, pad_x, pad_y), which
    is everything scale_boxes_back needs to undo it."""
    h, w = frame.shape[:2]
    ratio = min(size / h, size / w)
    nw, nh = round(w * ratio), round(h * ratio)
    resized = cv2.resize(frame, (nw, nh), interpolation=cv2.INTER_LINEAR)
    dw, dh = (size - nw) / 2, (size - nh) / 2
    top, bottom = round(dh - 0.1), round(dh + 0.1)
    left, right = round(dw - 0.1), round(dw + 0.1)
    padded = cv2.copyMakeBorder(resized, top, bottom, left, right,
                                cv2.BORDER_CONSTANT, value=(colour,) * 3)
    return padded, ratio, left, top


def decode_yolo_output(raw, conf: float):
    """YOLOv8's raw head -> (xywh, scores, class_ids) above `conf`.

    `raw` is (1, 4+num_classes, num_anchors): the first four rows are box
    centre-x, centre-y, width, height (in letterboxed pixels), the rest are
    per-class scores. Transposed here so each row is one candidate.
    """
    pred = np.asarray(raw)[0].T                  # (anchors, 4+nc)
    xywh, scores = pred[:, :4], pred[:, 4:]
    class_ids = scores.argmax(1)
    best = scores.max(1)
    keep = best >= conf
    return xywh[keep], best[keep], class_ids[keep]


def scale_boxes_back(xywh, ratio: float, pad_x: int, pad_y: int):
    """Undo letterbox(): letterboxed coordinates -> original-image pixels."""
    out = np.asarray(xywh, dtype=np.float64).copy()
    out[:, 0] = (out[:, 0] - pad_x) / ratio
    out[:, 1] = (out[:, 1] - pad_y) / ratio
    out[:, 2] = out[:, 2] / ratio
    out[:, 3] = out[:, 3] / ratio
    return out


def pick_backend(backend: str, weights) -> str:
    """Resolve --backend. 'auto' follows the weights extension, which is the
    unsurprising thing; an explicit value forces one, which is how you compare
    the two paths on the same machine."""
    if backend and backend != "auto":
        return backend
    return "onnx" if str(weights).lower().endswith(".onnx") else "ultralytics"


def draw_detections(frame, detections):
    """Annotated copy, replacing ultralytics' result.plot() (which needs torch).
    Boxes are centre-form, matching detections_from_result."""
    out = frame.copy()
    for d in detections:
        cx, cy, w, h = d["box"]
        x1, y1 = int(cx - w / 2), int(cy - h / 2)
        x2, y2 = int(cx + w / 2), int(cy + h / 2)
        cv2.rectangle(out, (x1, y1), (x2, y2), (0, 0, 255), 2)
        label = f"{d['cls']} {d['conf']:.2f}"
        cv2.putText(out, label, (x1, max(0, y1 - 6)),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.6, (0, 0, 255), 2)
    return out


def make_onnx_infer(weights, conf, imgsz, names=None):
    """Build the inference closure on onnxruntime. Same contract as
    make_yolo_infer: infer(frame) -> (detections, annotated_frame).

    onnxruntime is imported lazily, exactly as ultralytics is, so importing
    detect.py stays free of both.
    """
    import onnxruntime as ort            # heavy, lazy on purpose

    sess = ort.InferenceSession(str(weights),
                                providers=["CPUExecutionProvider"])
    input_name = sess.get_inputs()[0].name
    # An exported graph carries its class names in metadata; fall back to
    # indices so a stripped model still produces usable labels.
    if names is None:
        meta = sess.get_modelmeta().custom_metadata_map or {}
        try:
            names = {int(k): v for k, v in eval(meta.get("names", "{}")).items()}
        except Exception:
            names = {}

    def infer(frame):
        padded, ratio, pad_x, pad_y = letterbox(frame, imgsz)
        blob = cv2.cvtColor(padded, cv2.COLOR_BGR2RGB).astype(np.float32) / 255.0
        blob = np.expand_dims(blob.transpose(2, 0, 1), 0)
        raw = sess.run(None, {input_name: blob})[0]

        xywh, scores, class_ids = decode_yolo_output(raw, conf)
        detections = []
        if len(xywh):
            # cv2.dnn.NMSBoxes wants top-left form. OpenCV is already a
            # dependency, so this needs nothing extra.
            tl = np.stack([xywh[:, 0] - xywh[:, 2] / 2,
                           xywh[:, 1] - xywh[:, 3] / 2,
                           xywh[:, 2], xywh[:, 3]], 1)
            keep = cv2.dnn.NMSBoxes(tl.tolist(), scores.tolist(), conf, NMS_IOU)
            keep = np.asarray(keep).flatten() if len(keep) else []
            boxes = scale_boxes_back(xywh, ratio, pad_x, pad_y)
            for i in keep:
                cid = int(class_ids[i])
                detections.append({
                    "cls": names.get(cid, str(cid)),
                    "conf": float(scores[i]),
                    "box": [float(v) for v in boxes[i]],
                })
        return detections, draw_detections(frame, detections)

    return infer


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


def detection_loop(grab, infer, out_dir, *, camera, conf, stop_event, fps=None,
                   interval_s=None, clock=time.time,
                   max_read_failures=MAX_READ_FAILURES, roi=None):
    """Grab -> infer -> write, until stop_event is set.

    Cadence: interval_s seconds between captures (the primary knob); fps is the
    legacy equivalent, and either being unset/<=0 disables throttling (tests).

    A single None grab is a DROPPED FRAME, not a dead camera -- it is logged and
    retried. Only max_read_failures consecutive misses write an error status and
    end the loop. Exiting on the first miss used to take the whole process down;
    the supervisor then respawned it, reopening the device every few seconds,
    which is what made the camera appear to connect and disconnect forever.
    """
    if interval_s and interval_s > 0:
        period = float(interval_s)
    elif fps and fps > 0:
        period = 1.0 / fps
    else:
        period = 0.0
    failures = 0
    while not stop_event.is_set():
        t0 = clock()
        frame = grab()
        if frame is None:
            failures += 1
            if failures >= max_read_failures:
                _safe_write(write_status, out_dir, build_status(
                    [], ts=clock(), fps=0.0, camera=camera, conf=conf,
                    error=f"camera {camera} read failed "
                          f"({failures} consecutive misses)"))
                return
            # Say nothing to the server: the last good status simply ages, and
            # goes stale on its own if the misses keep coming. Retry sooner than
            # the full interval so a hiccup costs a fraction of a frame.
            log.warning("camera %s: dropped frame %d/%d, retrying",
                        camera, failures, max_read_failures)
            stop_event.wait(READ_RETRY_S)
            continue
        failures = 0
        # Infer on the ROI only: on the A1's wide, low, near-horizontal view
        # the rest of the frame is the room, and the model happily finds
        # "failures" in a laptop or a chair. Results are mapped back so the
        # status and the served frame stay in full-frame coordinates.
        crop, x0, y0 = crop_to_roi(frame, roi)
        detections, annotated_crop = infer(crop)
        detections = offset_detections(detections, x0, y0)
        annotated = compose_frame(frame, annotated_crop, x0, y0, roi)
        dt = max(clock() - t0, 1e-6)
        _safe_write(write_frame, out_dir, annotated)
        _safe_write(write_status, out_dir, build_status(detections, ts=clock(),
                                                        fps=round(1.0 / dt, 1),
                                                        camera=camera, conf=conf))
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
    p.add_argument("--interval", type=float, default=DEFAULT_INTERVAL_S,
                   help="seconds between captures (default: %(default)s)")
    p.add_argument("--roi", default=None,
                   help="bed region to run detection on, as fractions of the "
                        "frame: x,y,w,h (e.g. 0.15,0.35,0.7,0.45). Everything "
                        "outside is ignored, which removes background false "
                        "positives. Default: the whole frame.")
    p.add_argument("--device", default="0" if torch.cuda.is_available() else "cpu")
    p.add_argument("--backend", choices=("auto", "onnx", "ultralytics"),
                   default="auto",
                   help="inference runtime. 'auto' (default) picks onnx for "
                        "a .onnx file and ultralytics otherwise. NOTE: a conf "
                        "threshold tuned on one backend does NOT transfer to "
                        "the other -- they letterbox differently (see "
                        "make_onnx_infer)")
    p.add_argument("--mock", action="store_true")
    a = p.parse_args()

    roi = parse_roi(a.roi)
    if a.roi and roi is None:
        print(f"ignoring malformed --roi {a.roi!r}; using the whole frame",
              file=sys.stderr)
    if roi:
        log.info("detecting on ROI x=%.3f y=%.3f w=%.3f h=%.3f", *roi)

    stop = threading.Event()
    if a.mock:
        size = (480, 640, 3)
        grab = lambda: np.full(size, 40, np.uint8)  # noqa: E731
        infer = mock_infer
        try:
            detection_loop(grab, infer, a.out, camera=a.camera, conf=a.conf,
                           interval_s=a.interval, stop_event=stop, roi=roi)
        except KeyboardInterrupt:
            pass
    else:
        if not pathlib.Path(a.weights).exists():
            print(f"weights not found: {a.weights}", file=sys.stderr)
            return 1
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
        else:
            # Opened lazily by the first grab(), so a camera that is momentarily
            # busy (the previous detector still shutting down) is retried rather
            # than being a hard startup failure.
            cam = WebcamSource(a.camera)

        backend = pick_backend(a.backend, a.weights)
        log.info("inference backend: %s (%s)", backend, a.weights)
        if backend == "onnx":
            infer = make_onnx_infer(a.weights, a.conf, a.imgsz)
        else:
            infer = make_yolo_infer(a.weights, a.conf, a.imgsz, a.device)
        try:
            detection_loop(cam.grab, infer, a.out, camera=a.camera, conf=a.conf,
                           interval_s=a.interval, stop_event=stop, roi=roi)
        except KeyboardInterrupt:
            pass
        finally:
            cam.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
