import json

import numpy as np

import detect


def test_write_status_round_trips_and_leaves_no_temp(tmp_path):
    payload = {"ts": 1.0, "fps": 4.0, "camera": 0, "conf": 0.25,
               "detections": [], "error": None}
    detect.write_status(tmp_path, payload)
    got = json.loads((tmp_path / "status.json").read_text())
    assert got == payload
    assert [p.name for p in tmp_path.iterdir()] == ["status.json"]


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
