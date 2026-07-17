import json
import pathlib
import time

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


def test_reader_invalid_utf8_is_not_running(tmp_path):
    # e.g. a torn write caught mid multi-byte char (OneDrive sync, a reader
    # racing detect.py's os.replace). Must degrade to "down", not raise.
    (tmp_path / "status.json").write_bytes(b"\xff\xfe\x00bad")
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


def test_null_conf_does_not_fire_or_raise():
    clk = Clock()
    c = armed_controller(clk)
    bad = [{"cls": "spaghetti", "conf": None}]
    for t in (0, 11, 22):
        clk.t = t
        assert c.update(bad, "RUNNING") is None


def test_non_numeric_conf_does_not_fire_or_raise():
    clk = Clock()
    c = armed_controller(clk)
    bad = [{"cls": "spaghetti", "conf": "high"}]
    for t in (0, 11, 22):
        clk.t = t
        assert c.update(bad, "RUNNING") is None


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


from server.detection import DetectorSupervisor


class FakeProc:
    def __init__(self, argv, env=None):
        self.argv = argv; self.env = env; self._alive = True; self.terminated = False
    def poll(self): return None if self._alive else 1
    def terminate(self): self.terminated = True; self._alive = False
    def die(self): self._alive = False


def supervisor(tmp_path, clock):
    spawned = []
    def spawn(argv, env=None):
        p = FakeProc(argv, env); spawned.append(p); return p
    sup = DetectorSupervisor(tmp_path, "weights.pt", spawn=spawn,
                             clock=clock, backoff_s=5.0)
    return sup, spawned


T1 = {"serial": "S1", "camera_source": "a1", "camera_index": 0, "conf": 0.25,
      "host": "1.2.3.4", "access_code": "CODE1"}
T2 = {"serial": "S1", "camera_source": "webcam", "camera_index": 2, "conf": 0.4,
      "host": "1.2.3.4", "access_code": "CODE1"}


def test_spawns_for_a_new_target(tmp_path):
    sup, spawned = supervisor(tmp_path, lambda: 0.0)
    sup.reconcile(T1)
    assert len(spawned) == 1
    assert "--source" in spawned[0].argv and "a1" in spawned[0].argv
    assert "--host" in spawned[0].argv and "1.2.3.4" in spawned[0].argv


def test_argv_never_contains_access_code(tmp_path):
    sup, spawned = supervisor(tmp_path, lambda: 0.0)
    sup.reconcile(T1)
    assert not any("code" in str(a) for a in spawned[0].argv)


def test_changed_target_restarts(tmp_path):
    sup, spawned = supervisor(tmp_path, lambda: 0.0)
    sup.reconcile(T1)
    sup.reconcile(T2)
    assert spawned[0].terminated is True
    assert len(spawned) == 2
    assert "2" in spawned[1].argv


def test_none_target_stops(tmp_path):
    sup, spawned = supervisor(tmp_path, lambda: 0.0)
    sup.reconcile(T1)
    sup.reconcile(None)
    assert spawned[0].terminated is True


def test_crash_respawns_after_backoff(tmp_path):
    clk = Clock()
    sup, spawned = supervisor(tmp_path, clk)
    sup.reconcile(T1)
    spawned[0].die()
    clk.t = 2.0
    sup.reconcile(T1)                 # within backoff -> no respawn
    assert len(spawned) == 1
    clk.t = 6.0
    sup.reconcile(T1)                 # backoff elapsed -> respawn
    assert len(spawned) == 2


def test_build_argv_script_defaults_to_absolute_path(tmp_path):
    # A cwd-relative default ("detect.py") silently never runs the detector
    # if the server is launched from a different working directory. The
    # default must resolve to an absolute path, same as the weights path.
    sup, _ = supervisor(tmp_path, lambda: 0.0)
    script = sup.build_argv(T1)[1]
    assert pathlib.Path(script).is_absolute()
    assert script.endswith("detect.py")


def test_build_argv_a1_has_source_host_no_code(tmp_path):
    sup, _ = supervisor(tmp_path, lambda: 0.0)
    argv = sup.build_argv(T1)
    assert "--source" in argv and "a1" in argv
    assert "--host" in argv and "1.2.3.4" in argv
    assert "--camera" not in argv
    assert not any("CODE1" in str(a) for a in argv)   # secret never in argv


def test_build_argv_webcam_has_camera(tmp_path):
    sup, _ = supervisor(tmp_path, lambda: 0.0)
    argv = sup.build_argv(T2)
    assert "--source" in argv and "webcam" in argv
    assert "--camera" in argv and "2" in argv
    assert "--host" not in argv


def test_build_env_carries_code_for_a1_only(tmp_path):
    sup, _ = supervisor(tmp_path, lambda: 0.0)
    a1_env = sup.build_env(T1)
    assert a1_env["BAMBU_ACCESS_CODE"] == "CODE1"
    assert sup.build_env(T2) is None    # webcam inherits the parent env


from server.detection import DetectionCoordinator


class FakeRunner:
    def __init__(self): self.targets = []; self.stopped = False
    def reconcile(self, target): self.targets.append(target)
    def stop(self): self.stopped = True


class FakeReg:
    def __init__(self, target, gstate="RUNNING"):
        self._target = target
        self._gstate = gstate
        self.stopped = 0
    def detection_target(self): return self._target
    def capture_serial(self): return self._target["serial"] if self._target else None
    def detection_config(self, serial):
        return {"camera_source": "a1", "camera_index": 0, "conf": 0.5,
                "armed_classes": ["spaghetti"], "detect_enabled": True}
    def get(self, serial):
        reg = self
        class S:
            def summary(self_): return {"serial": serial, "gcode_state": reg._gstate}
            def stop_print(self_): reg.stopped += 1; reg._gstate = "FAILED"
        return S()


def test_tick_reconciles_the_runner(tmp_path):
    reg = FakeReg({"serial": "S1", "camera_index": 0, "conf": 0.5})
    runner = FakeRunner()
    co = DetectionCoordinator(reg, tmp_path, runner)
    co.tick()
    assert runner.targets[-1]["serial"] == "S1"


def test_tick_fires_stop_after_sustained_fault(tmp_path):
    reg = FakeReg({"serial": "S1", "camera_index": 0, "conf": 0.5})
    runner = FakeRunner()
    clk = Clock()
    co = DetectionCoordinator(reg, tmp_path, runner,
                              controller_factory=lambda: AutoStopController(clock=clk))
    (tmp_path / "_detect").mkdir()
    def status(ts):
        (tmp_path / "_detect" / "status.json").write_text(json.dumps(
            {"ts": ts, "fps": 4.0, "camera": 0, "conf": 0.5,
             "detections": [{"cls": "spaghetti", "conf": 0.9}], "error": None}))
    co.reader = StatusReader(tmp_path / "_detect", clock=lambda: clk.t)
    co.arm("S1", True)
    status(clk.t); co.tick()                 # t=0 fault begins
    clk.t = 10.0; status(clk.t); co.tick()   # sustained -> fire
    assert reg.stopped == 1


def test_stale_status_does_not_fire(tmp_path):
    reg = FakeReg({"serial": "S1", "camera_index": 0, "conf": 0.5})
    clk = Clock()
    co = DetectionCoordinator(reg, tmp_path, FakeRunner(),
                              controller_factory=lambda: AutoStopController(clock=clk))
    (tmp_path / "_detect").mkdir()
    (tmp_path / "_detect" / "status.json").write_text(json.dumps(
        {"ts": 0.0, "fps": 4.0, "camera": 0, "conf": 0.5,
         "detections": [{"cls": "spaghetti", "conf": 0.9}], "error": None}))
    co.reader = StatusReader(tmp_path / "_detect", stale_after=3.0, clock=lambda: clk.t)
    co.arm("S1", True)
    for t in (0, 11, 22, 33):                # status is always stale (ts=0)
        clk.t = float(t); co.tick()
    assert reg.stopped == 0                   # never acts on stale detections


def test_snapshot_none_for_non_capture(tmp_path):
    reg = FakeReg({"serial": "S1", "camera_index": 0, "conf": 0.5})
    co = DetectionCoordinator(reg, tmp_path, FakeRunner())
    assert co.snapshot("OTHER") is None
    snap = co.snapshot("S1")
    assert snap["armed_classes"] == ["spaghetti"]


def test_snapshot_includes_camera_source(tmp_path):
    reg = FakeReg({"serial": "S1", "camera_index": 0, "conf": 0.5})
    co = DetectionCoordinator(reg, tmp_path, FakeRunner())
    assert co.snapshot("S1")["camera_source"] == "a1"


def test_capture_switch_resets_auto_stop_state(tmp_path):
    # A controller frozen mid-fault for a printer the camera has LEFT must not
    # survive a capture reassignment: on switch-back it would otherwise fire on
    # the first frame (stale fault_since), bypassing the 10s debounce.
    reg = FakeReg({"serial": "S1", "camera_index": 0, "conf": 0.5})
    clk = Clock()
    co = DetectionCoordinator(reg, tmp_path, FakeRunner(),
                              controller_factory=lambda: AutoStopController(clock=clk))
    (tmp_path / "_detect").mkdir()

    def status(ts):
        (tmp_path / "_detect" / "status.json").write_text(json.dumps(
            {"ts": ts, "fps": 4.0, "camera": 0, "conf": 0.5,
             "detections": [{"cls": "spaghetti", "conf": 0.9}], "error": None}))

    co.reader = StatusReader(tmp_path / "_detect", clock=lambda: clk.t)
    co.arm("S1", True)
    status(clk.t); co.tick()                          # t=0: S1 -> armed_faulting
    reg._target = {"serial": "S2", "camera_index": 0, "conf": 0.5}
    clk.t = 500.0; status(clk.t); co.tick()           # camera moved to S2
    reg._target = {"serial": "S1", "camera_index": 0, "conf": 0.5}
    clk.t = 501.0; status(clk.t); co.tick()           # camera back on S1
    assert reg.stopped == 0                            # fresh & disarmed: no fire


def test_snapshot_uses_cached_status_not_a_live_read(tmp_path):
    # The WS path calls snapshot() every tick and must never touch the
    # filesystem -- tick() already reads status.json every 0.5s, so snapshot()
    # must serve that cached copy. Prove it: tick() once against a running
    # status with a spaghetti detection, delete status.json, then confirm
    # snapshot() still reports running/detections from the cache (a live
    # re-read of the now-missing file would report running False, []).
    reg = FakeReg({"serial": "S1", "camera_index": 0, "conf": 0.5})
    clk = Clock()
    co = DetectionCoordinator(reg, tmp_path, FakeRunner(),
                              controller_factory=lambda: AutoStopController(clock=clk))
    (tmp_path / "_detect").mkdir()
    status_path = tmp_path / "_detect" / "status.json"
    status_path.write_text(json.dumps(
        {"ts": clk.t, "fps": 4.0, "camera": 0, "conf": 0.5,
         "detections": [{"cls": "spaghetti", "conf": 0.9}], "error": None}))
    co.reader = StatusReader(tmp_path / "_detect", clock=lambda: clk.t)
    co.tick()               # caches a running status with the detection
    status_path.unlink()    # a live re-read from here on would see "down"
    snap = co.snapshot("S1")
    assert snap["running"] is True
    assert snap["detections"] == [{"cls": "spaghetti", "conf": 0.9}]


from server.detection import MockDetectorRunner


def _wait_until(predicate, timeout=2.0, interval=0.05):
    deadline = time.time() + timeout
    ok = predicate()
    while not ok and time.time() < deadline:
        time.sleep(interval)
        ok = predicate()
    return ok


def test_mock_runner_reconcile_writes_status_and_is_idempotent(tmp_path):
    # period_s=0.1 (not the 0.02 used elsewhere): fast enough that the first
    # write lands well inside the poll timeout, but slow enough that this
    # test's polling read doesn't collide with the writer thread's temp+
    # os.replace on Windows (a real race there, but a pre-existing property
    # of the atomic-write helper in detect.py, not of the code under test --
    # verified empirically that read+0.02s churn reproduces it and read+0.1s
    # does not; out of scope to change the shared write helper here).
    runner = MockDetectorRunner(tmp_path, period_s=0.1)
    status_path = tmp_path / "status.json"

    def has_spaghetti():
        # Single read per poll (not a separate exists-check then a content
        # check) to minimize how often this thread's read races the writer
        # thread's atomic replace; a transient PermissionError/OSError while
        # the file is mid-replace just means "not yet" -- retry like a miss.
        try:
            data = json.loads(status_path.read_text())
        except (OSError, json.JSONDecodeError):
            return False
        dets = data.get("detections") or []
        return any(d.get("cls") == "spaghetti" for d in dets)

    try:
        runner.reconcile({"serial": "S1", "camera_index": 0, "conf": 0.25})
        assert _wait_until(has_spaghetti), \
            "no spaghetti detection was ever written by the mock writer thread"

        first_thread = runner._thread
        runner.reconcile({"serial": "S1", "camera_index": 0, "conf": 0.25})  # same target again
        assert runner._thread is first_thread   # idempotent -- no second thread started

        runner.reconcile(None)                  # halts via reconcile(None)
        assert runner._active is False

        runner.stop()                           # halting again via stop() must stay safe
        assert runner._active is False
    finally:
        runner.stop()                           # never leak a live writer thread on failure
