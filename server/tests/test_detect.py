import json

import numpy as np
import pytest

import detect


def test_write_status_round_trips_and_leaves_no_temp(tmp_path):
    payload = {"ts": 1.0, "fps": 4.0, "camera": 0, "conf": 0.25,
               "detections": [], "error": None}
    detect.write_status(tmp_path, payload)
    got = json.loads((tmp_path / "status.json").read_text())
    assert got == payload
    assert [p.name for p in tmp_path.iterdir()] == ["status.json"]


def test_write_status_retries_os_replace_then_succeeds(tmp_path, monkeypatch):
    # Windows: os.replace raises PermissionError (WinError 5/32) when the
    # destination is momentarily open by a reader (server's StatusReader) or
    # held by OneDrive sync. That's transient -- a short retry loop should
    # ride it out rather than propagate and kill the writer.
    real_replace = detect.os.replace
    calls = {"n": 0}

    def flaky_replace(src, dst):
        calls["n"] += 1
        if calls["n"] <= 2:
            raise PermissionError("simulated transient sharing violation")
        return real_replace(src, dst)

    monkeypatch.setattr(detect.os, "replace", flaky_replace)
    monkeypatch.setattr(detect.time, "sleep", lambda s: None)

    payload = {"ts": 1.0, "fps": 4.0, "camera": 0, "conf": 0.25,
               "detections": [], "error": None}
    detect.write_status(tmp_path, payload)

    got = json.loads((tmp_path / "status.json").read_text())
    assert got == payload
    assert [p.name for p in tmp_path.iterdir()] == ["status.json"]
    assert calls["n"] == 3


def test_write_status_gives_up_after_retries(tmp_path, monkeypatch):
    def always_fail(src, dst):
        raise PermissionError("simulated permanent sharing violation")

    monkeypatch.setattr(detect.os, "replace", always_fail)
    monkeypatch.setattr(detect.time, "sleep", lambda s: None)

    payload = {"ts": 1.0, "fps": 4.0, "camera": 0, "conf": 0.25,
               "detections": [], "error": None}
    with pytest.raises(PermissionError):
        detect.write_status(tmp_path, payload)
    assert list(tmp_path.iterdir()) == []


def test_write_frame_writes_a_jpeg(tmp_path):
    detect.write_frame(tmp_path, np.zeros((16, 16, 3), np.uint8))
    data = (tmp_path / "latest.jpg").read_bytes()
    assert data[:2] == b"\xff\xd8"  # JPEG magic


def test_build_status_shape():
    s = detect.build_status([{"cls": "spaghetti", "conf": 0.9, "box": [1, 2, 3, 4]}],
                            ts=5.0, fps=3.0, camera=1, conf=0.3)
    assert s["camera"] == 1 and s["conf"] == 0.3 and s["error"] is None
    assert s["detections"][0]["cls"] == "spaghetti"


class _Boxes:
    def __init__(self, xywh, cls, conf):
        self.xywh = _Arr(xywh); self.cls = _Arr(cls); self.conf = _Arr(conf)


class _Arr:
    def __init__(self, v): self._v = v
    def tolist(self): return self._v


class _Result:
    def __init__(self, boxes): self.boxes = boxes


def test_detections_from_result_parses_boxes():
    r = _Result(_Boxes(xywh=[[10.4, 20.6, 30.0, 40.0]], cls=[3.0], conf=[0.812]))
    names = {3: "spaghetti"}
    got = detect.detections_from_result(r, names)
    assert got == [{"cls": "spaghetti", "conf": 0.812, "box": [10, 21, 30, 40]}]


def test_detections_from_result_empty_when_no_boxes():
    assert detect.detections_from_result(_Result(None), {}) == []


def test_detection_loop_writes_status_and_frame(tmp_path):
    frames = [np.zeros((16, 16, 3), np.uint8) for _ in range(3)]
    grabbed = iter(frames)
    stop = detect.threading.Event()
    calls = {"n": 0}

    def grab():
        return next(grabbed, None)

    def infer(frame):
        calls["n"] += 1
        if calls["n"] >= 2:
            stop.set()   # end after two frames
        return ([{"cls": "spaghetti", "conf": 0.9, "box": [1, 1, 2, 2]}], frame)

    detect.detection_loop(grab, infer, tmp_path, camera=0, conf=0.25,
                          fps=0, stop_event=stop)
    status = json.loads((tmp_path / "status.json").read_text())
    assert status["detections"][0]["cls"] == "spaghetti"
    assert status["error"] is None
    assert (tmp_path / "latest.jpg").exists()


def test_detection_loop_records_camera_read_failure(tmp_path):
    # A camera that NEVER returns a frame is genuinely dead: after
    # MAX_READ_FAILURES consecutive misses the loop gives up and says so.
    stop = detect.threading.Event()

    def grab():
        return None

    detect.detection_loop(grab, lambda f: ([], f), tmp_path, camera=3,
                          conf=0.25, fps=0, stop_event=stop,
                          max_read_failures=3)
    status = json.loads((tmp_path / "status.json").read_text())
    assert status["error"] is not None
    assert status["detections"] == []


def test_detection_loop_survives_a_transient_read_failure(tmp_path):
    # THE BUG: one dropped USB frame used to end the loop, exit the process and
    # make the supervisor respawn it -- releasing and reopening the device every
    # few seconds (the "camera connects/disconnects continuously" symptom).
    # A single miss must be ridden out, not treated as a dead camera.
    seq = [None, np.zeros((16, 16, 3), np.uint8), None,
           np.zeros((16, 16, 3), np.uint8)]
    it = iter(seq)
    stop = detect.threading.Event()
    infers = {"n": 0}

    def grab():
        return next(it, None)

    def infer(frame):
        infers["n"] += 1
        if infers["n"] >= 2:
            stop.set()
        return ([], frame)

    detect.detection_loop(grab, infer, tmp_path, camera=0, conf=0.25, fps=0,
                          stop_event=stop, max_read_failures=3)

    assert infers["n"] == 2          # kept going across both dropped frames
    status = json.loads((tmp_path / "status.json").read_text())
    assert status["error"] is None   # never declared the camera dead


def test_detection_loop_resets_failure_count_on_a_good_frame(tmp_path):
    # 2 misses, a good frame, then 2 more misses must NOT trip a 3-miss budget.
    seq = [None, None, np.zeros((16, 16, 3), np.uint8), None, None,
           np.zeros((16, 16, 3), np.uint8)]
    it = iter(seq)
    stop = detect.threading.Event()
    infers = {"n": 0}

    def infer(frame):
        infers["n"] += 1
        if infers["n"] >= 2:
            stop.set()
        return ([], frame)

    detect.detection_loop(lambda: next(it, None), infer, tmp_path, camera=0,
                          conf=0.25, fps=0, stop_event=stop,
                          max_read_failures=3)

    assert infers["n"] == 2
    assert json.loads((tmp_path / "status.json").read_text())["error"] is None


def test_detection_loop_interval_sets_the_capture_period(tmp_path):
    # --interval is the primary cadence knob: 5 s between captures.
    waits = []
    stop = detect.threading.Event()
    ticks = {"t": 0.0}

    class Ev:                      # stand-in Event recording the wait it is given
        def is_set(self_):
            return stop.is_set()
        def wait(self_, s):
            waits.append(s)
            return stop.wait(0)

    def infer(frame):
        stop.set()
        return ([], frame)

    detect.detection_loop(lambda: np.zeros((8, 8, 3), np.uint8), infer, tmp_path,
                          camera=0, conf=0.25, fps=None, interval_s=5.0,
                          stop_event=Ev(), clock=lambda: ticks["t"])

    assert waits == [5.0]          # full interval, since the tick took ~0 s


class FakeCap:
    """Minimal cv2.VideoCapture stand-in: `reads` is a list of ok flags."""

    def __init__(self, reads):
        self._reads = list(reads)
        self.released = False

    def read(self):
        ok = self._reads.pop(0) if self._reads else False
        return (ok, np.zeros((8, 8, 3), np.uint8) if ok else None)

    def release(self):
        self.released = True


def test_webcam_source_retries_a_read_before_reopening():
    # A dropped frame is recovered by simply reading again -- reopening the
    # device (the expensive, user-visible disconnect) is a LAST resort.
    caps = [FakeCap([True, False,   # flush, failed read   -> attempt 1
                     True, True])]  # flush, good read     -> attempt 2
    opens = []

    def open_fn(index, w, h):
        opens.append(index)
        return caps[len(opens) - 1]

    src = detect.WebcamSource(0, open_fn=open_fn)
    assert src.grab() is not None
    assert len(opens) == 1          # never reopened the device


def test_webcam_source_reopens_after_a_persistent_read_failure():
    caps = [FakeCap([True, False, True, False]),   # both attempts fail
            FakeCap([True, True])]                 # reopened device works
    opens = []

    def open_fn(index, w, h):
        opens.append(index)
        return caps[len(opens) - 1]

    src = detect.WebcamSource(0, open_fn=open_fn)
    assert src.grab() is not None
    assert len(opens) == 2          # exactly one reopen
    assert caps[0].released is True  # old handle freed before reopening


def test_webcam_source_returns_none_when_the_device_is_gone():
    def open_fn(index, w, h):
        raise RuntimeError(f"cannot open camera index {index}")

    assert detect.WebcamSource(0, open_fn=open_fn).grab() is None


def test_detection_loop_survives_a_transient_write_error(monkeypatch, tmp_path):
    # A frame/status write raising OSError (Windows/OneDrive os.replace race)
    # must NOT crash the detector -- the frame is skipped and the loop continues.
    frames = [np.zeros((16, 16, 3), np.uint8) for _ in range(3)]
    it = iter(frames)
    stop = detect.threading.Event()
    calls = {"n": 0}

    def grab():
        return next(it, None)

    def infer(frame):
        calls["n"] += 1
        if calls["n"] >= 2:
            stop.set()
        return ([], frame)

    def boom(*a, **k):
        raise OSError("[WinError 5] Access is denied")

    monkeypatch.setattr(detect, "write_frame", boom)   # every frame write fails

    # Must return normally (no exception propagates out of the loop).
    detect.detection_loop(grab, infer, tmp_path, camera=0, conf=0.25, fps=0,
                          stop_event=stop)
    assert calls["n"] >= 2   # the loop kept running despite the write errors


import struct

import cv2


class FakeSock:
    """A socket-like that hands out a fixed byte buffer and records sends."""
    def __init__(self, data=b""):
        self.buf = data
        self.sent = b""
        self.closed = False

    def settimeout(self, t):
        pass

    def sendall(self, b):
        self.sent += b

    def recv(self, n):
        if not self.buf:
            return b""   # peer closed
        out, self.buf = self.buf[:n], self.buf[n:]
        return out

    def close(self):
        self.closed = True


def _jpeg_bytes():
    ok, buf = cv2.imencode(".jpg", np.zeros((8, 8, 3), np.uint8))
    return buf.tobytes()


def _framed(jpeg):
    return struct.pack("<I", len(jpeg)) + b"\x00" * 12 + jpeg


def test_bambu_source_auths_and_reads_a_frame():
    jpeg = _jpeg_bytes()
    sock = FakeSock(_framed(jpeg))
    src = detect.BambuCameraSource("h", "MYCODE", connect=lambda host, t: sock)
    frame = src.grab()
    assert frame is not None and frame.shape == (8, 8, 3)
    # 80-byte auth: header + bblp + access code
    assert len(sock.sent) == 80
    assert sock.sent[:16] == struct.pack("<IIII", 0x40, 0x3000, 0, 0)
    assert sock.sent[16:20] == b"bblp"
    assert b"MYCODE" in sock.sent


def test_bambu_source_reconnects_once_on_drop():
    jpeg = _jpeg_bytes()
    socks = [FakeSock(b""), FakeSock(_framed(jpeg))]   # first is dead
    calls = []

    def connect(host, t):
        s = socks[len(calls)]
        calls.append(s)
        return s

    src = detect.BambuCameraSource("h", "C", connect=connect)
    assert src.grab() is not None
    assert len(calls) == 2   # reconnected exactly once


def test_bambu_source_returns_none_on_persistent_failure():
    def connect(host, t):
        raise OSError("connection refused")

    src = detect.BambuCameraSource("h", "C", connect=connect)
    assert src.grab() is None   # -> the loop writes an error status


def test_bambu_source_retries_on_decode_failure():
    # valid-size header but a non-JPEG payload -> imdecode None -> must reconnect
    # and recover, not silently return None on the first attempt.
    socks = [FakeSock(_framed(b"not a jpeg at all")), FakeSock(_framed(_jpeg_bytes()))]
    calls = []

    def connect(host, t):
        s = socks[len(calls)]
        calls.append(s)
        return s

    src = detect.BambuCameraSource("h", "C", connect=connect)
    assert src.grab() is not None
    assert len(calls) == 2          # decode failure triggered exactly one reconnect


def test_bambu_source_rejects_implausible_frame_size():
    bad = struct.pack("<I", 0) + b"\x00" * 12    # size 0 header, then nothing
    socks = [FakeSock(bad), FakeSock(_framed(_jpeg_bytes()))]
    calls = []

    def connect(host, t):
        s = socks[len(calls)]
        calls.append(s)
        return s

    src = detect.BambuCameraSource("h", "C", connect=connect)
    assert src.grab() is not None
    assert len(calls) == 2


def test_bambu_source_closes_socket_if_auth_send_fails():
    class BadSendSock(FakeSock):
        def sendall(self, b):
            raise OSError("reset after handshake")

    sock = BadSendSock(b"")
    src = detect.BambuCameraSource("h", "C", connect=lambda host, t: sock)
    assert src.grab() is None
    assert sock.closed is True      # the just-opened socket was cleaned up


# --------------------------------------------------------------------------
# ROI cropping.
#
# The A1's built-in camera is a wide, low, near-horizontal view that includes
# the whole room -- the detector fired "stringing 0.74" on a laptop edge in the
# background. Cropping to the bed before inference removes that false-positive
# surface and enlarges the print in frame, which also helps at this flat angle.
#
# ROI is stored as FRACTIONS of the frame so it survives a resolution change.
# --------------------------------------------------------------------------

def test_parse_roi_reads_four_fractions():
    assert detect.parse_roi("0.1,0.4,0.8,0.5") == (0.1, 0.4, 0.8, 0.5)


def test_parse_roi_rejects_junk_as_no_roi():
    # A bad --roi must degrade to "whole frame", never crash the detector.
    for bad in (None, "", "1,2", "a,b,c,d", "0.1,0.2,0.3", "0,0,0,0"):
        assert detect.parse_roi(bad) is None, bad


def test_parse_roi_rejects_out_of_range():
    assert detect.parse_roi("-0.1,0,0.5,0.5") is None
    assert detect.parse_roi("0.6,0,0.8,0.5") is None   # x+w > 1


def test_crop_to_roi_returns_the_region_and_its_origin():
    frame = np.zeros((100, 200, 3), np.uint8)
    frame[40:90, 20:180] = 7                      # mark the expected region
    crop, x0, y0 = detect.crop_to_roi(frame, (0.1, 0.4, 0.8, 0.5))
    assert (x0, y0) == (20, 40)
    assert crop.shape[:2] == (50, 160)
    assert (crop == 7).all()


def test_crop_to_roi_without_roi_is_the_whole_frame():
    frame = np.zeros((100, 200, 3), np.uint8)
    crop, x0, y0 = detect.crop_to_roi(frame, None)
    assert (x0, y0) == (0, 0)
    assert crop.shape == frame.shape


def test_crop_to_roi_never_returns_an_empty_crop():
    # A sub-pixel ROI on a small frame must still yield at least 1x1, or
    # cv2/YOLO would raise on an empty array.
    frame = np.zeros((10, 10, 3), np.uint8)
    crop, _, _ = detect.crop_to_roi(frame, (0.0, 0.0, 0.01, 0.01))
    assert crop.size > 0


def test_offset_detections_maps_boxes_back_to_full_frame():
    dets = [{"cls": "spaghetti", "conf": 0.9, "box": [10, 20, 30, 40]}]
    out = detect.offset_detections(dets, 100, 200)
    assert out[0]["box"] == [110, 220, 30, 40]     # center moves, size doesn't
    assert out[0]["cls"] == "spaghetti"
    assert dets[0]["box"] == [10, 20, 30, 40]      # input not mutated


def test_offset_detections_is_a_noop_at_the_origin():
    dets = [{"cls": "a", "conf": 0.5, "box": [1, 2, 3, 4]}]
    assert detect.offset_detections(dets, 0, 0) == dets


def test_detection_loop_infers_on_the_crop_and_reports_full_frame_boxes(tmp_path):
    """The whole point: YOLO sees only the bed, but status.json/latest.jpg stay
    in full-frame coordinates so the UI and the ROI overlay line up."""
    frame = np.zeros((100, 200, 3), np.uint8)
    stop = detect.threading.Event()
    seen = {}

    def infer(f):
        seen["shape"] = f.shape           # what the model actually got
        stop.set()
        return ([{"cls": "spaghetti", "conf": 0.9, "box": [5, 5, 10, 10]}], f)

    detect.detection_loop(lambda: frame, infer, tmp_path, camera=0,
                          conf=0.25, fps=0, stop_event=stop,
                          roi=(0.1, 0.4, 0.8, 0.5))

    assert seen["shape"][:2] == (50, 160)          # inferred on the crop
    status = json.loads((tmp_path / "status.json").read_text())
    box = status["detections"][0]["box"]
    assert box[0] == 5 + 20 and box[1] == 5 + 40   # reported in full frame


def test_detection_loop_writes_a_full_frame_when_cropping(tmp_path):
    # latest.jpg must stay the FULL view (annotated crop pasted back, ROI
    # outlined) -- cropping is an inference optimisation, not a change to what
    # the operator sees, and the overlay is how the ROI gets tuned by eye.
    frame = np.zeros((100, 200, 3), np.uint8)
    stop = detect.threading.Event()

    def infer(f):
        stop.set()
        return ([], f)

    detect.detection_loop(lambda: frame, infer, tmp_path, camera=0, conf=0.25,
                          fps=0, stop_event=stop, roi=(0.1, 0.4, 0.8, 0.5))

    written = cv2.imdecode(
        np.frombuffer((tmp_path / "latest.jpg").read_bytes(), np.uint8),
        cv2.IMREAD_COLOR)
    assert written.shape[:2] == (100, 200)         # full frame, not the crop
