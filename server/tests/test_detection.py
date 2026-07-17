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


from server.detection import AutoStopController


class Clock:
    def __init__(self): self.t = 0.0
    def __call__(self): return self.t


SPAG = [{"cls": "spaghetti", "conf": 0.9, "box": [0, 0, 1, 1]}]


def armed_controller(clock):
    c = AutoStopController(sustain_s=10.0, verify_s=5.0, clock=clock)
    c.configure(["spaghetti"], 0.5)
    c.arm(True)
    return c


def test_fires_after_ten_sustained_seconds():
    clk = Clock()
    c = armed_controller(clk)
    assert c.update(SPAG, "RUNNING") is None      # t=0, fault starts
    clk.t = 9.9
    assert c.update(SPAG, "RUNNING") is None       # not yet
    clk.t = 10.0
    assert c.update(SPAG, "RUNNING") == "fire"     # sustained -> fire


def test_gap_resets_the_timer():
    clk = Clock()
    c = armed_controller(clk)
    c.update(SPAG, "RUNNING")                       # t=0 fault
    clk.t = 5.0
    c.update([], "RUNNING")                          # clears -> armed_idle
    clk.t = 6.0
    c.update(SPAG, "RUNNING")                         # new fault window
    clk.t = 15.0
    assert c.update(SPAG, "RUNNING") is None          # only 9s -> no fire
    clk.t = 16.0
    assert c.update(SPAG, "RUNNING") == "fire"


def test_below_threshold_never_fires():
    clk = Clock()
    c = armed_controller(clk)
    weak = [{"cls": "spaghetti", "conf": 0.4}]
    for t in (0, 11, 22):
        clk.t = t
        assert c.update(weak, "RUNNING") is None


def test_non_armed_class_ignored():
    clk = Clock()
    c = armed_controller(clk)
    other = [{"cls": "stringing", "conf": 0.99}]
    clk.t = 0; c.update(other, "RUNNING")
    clk.t = 20
    assert c.update(other, "RUNNING") is None


def test_disarmed_never_fires():
    clk = Clock()
    c = AutoStopController(clock=clk)
    c.configure(["spaghetti"], 0.5)  # not armed
    clk.t = 0; c.update(SPAG, "RUNNING")
    clk.t = 30
    assert c.update(SPAG, "RUNNING") is None


def test_fire_auto_disarms_and_latches():
    clk = Clock()
    c = armed_controller(clk)
    c.update(SPAG, "RUNNING"); clk.t = 10.0; c.update(SPAG, "RUNNING")
    snap = c.snapshot()
    assert snap["armed"] is False
    assert snap["stopped_by_monitor"] is True


def test_retries_once_then_gives_up_if_stop_ignored():
    clk = Clock()
    c = armed_controller(clk)
    c.update(SPAG, "RUNNING"); clk.t = 10.0
    assert c.update(SPAG, "RUNNING") == "fire"          # first stop
    clk.t = 15.0
    assert c.update(SPAG, "RUNNING") == "fire"          # not stopped -> retry
    clk.t = 20.0
    assert c.update(SPAG, "RUNNING") is None            # gave up, latched


def test_no_retry_when_stop_confirmed():
    clk = Clock()
    c = armed_controller(clk)
    c.update(SPAG, "RUNNING"); clk.t = 10.0
    assert c.update(SPAG, "RUNNING") == "fire"
    clk.t = 15.0
    assert c.update(SPAG, "FAILED") is None             # printer stopped
    assert c.snapshot()["state"] == "stopped"


def test_seconds_to_stop_counts_down():
    clk = Clock()
    c = armed_controller(clk)
    c.update(SPAG, "RUNNING")
    clk.t = 4.0
    assert c.snapshot()["seconds_to_stop"] == 6.0
