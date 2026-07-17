import json

from server.detection import StatusReader


def write(tmp_path, payload):
    (tmp_path / "status.json").write_text(json.dumps(payload))


def test_reader_missing_file_is_not_running(tmp_path):
    r = StatusReader(tmp_path, clock=lambda: 100.0).read()
    assert r["running"] is False
    assert r["detections"] == []


def test_reader_fresh_status_is_running(tmp_path):
    write(tmp_path, {"ts": 99.0, "fps": 4.0, "camera": 0, "conf": 0.25,
                     "detections": [{"cls": "spaghetti", "conf": 0.9}],
                     "error": None})
    r = StatusReader(tmp_path, stale_after=3.0, clock=lambda: 100.0).read()
    assert r["running"] is True
    assert r["detections"][0]["cls"] == "spaghetti"


def test_reader_stale_status_is_not_running(tmp_path):
    write(tmp_path, {"ts": 10.0, "fps": 4.0, "camera": 0, "conf": 0.25,
                     "detections": [], "error": None})
    r = StatusReader(tmp_path, stale_after=3.0, clock=lambda: 100.0).read()
    assert r["running"] is False


def test_reader_error_status_is_not_running(tmp_path):
    write(tmp_path, {"ts": 99.5, "fps": 0.0, "camera": 3, "conf": 0.25,
                     "detections": [], "error": "camera 3 read failed"})
    r = StatusReader(tmp_path, clock=lambda: 100.0).read()
    assert r["running"] is False
    assert r["error"] == "camera 3 read failed"


def test_reader_half_written_json_is_not_running(tmp_path):
    (tmp_path / "status.json").write_text('{"ts": 99.0, "detec')  # truncated
    r = StatusReader(tmp_path, clock=lambda: 100.0).read()
    assert r["running"] is False
