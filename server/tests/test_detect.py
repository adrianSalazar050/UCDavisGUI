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
    stop = detect.threading.Event()

    def grab():          # a dead camera returns None
        stop.set()
        return None

    detect.detection_loop(grab, lambda f: ([], f), tmp_path, camera=3,
                          conf=0.25, fps=0, stop_event=stop)
    status = json.loads((tmp_path / "status.json").read_text())
    assert status["error"] is not None
    assert status["detections"] == []


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
