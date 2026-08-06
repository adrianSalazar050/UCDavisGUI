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


from server.detection import (DEFAULT_INTERVAL_S, DetectionCoordinator,
                              DetectorSupervisor, out_dir_for)


class FakeProc:
    def __init__(self, argv, env=None):
        self.argv = argv; self.env = env; self._alive = True; self.terminated = False
        self.waited = None
    def poll(self): return None if self._alive else 1
    def terminate(self): self.terminated = True; self._alive = False
    def wait(self, timeout=None): self.waited = timeout; return 1
    def die(self): self._alive = False


def supervisor(tmp_path, clock):
    spawned = []
    def spawn(argv, env=None):
        p = FakeProc(argv, env); spawned.append(p); return p
    sup = DetectorSupervisor(tmp_path, "weights.pt", spawn=spawn,
                             clock=clock, backoff_s=5.0)
    return sup, spawned


# T* is printer S1 (T2 = the same printer with changed settings), U* is a
# SECOND printer with its own built-in camera, its own host and its own access
# code -- two camera printers at once, which is what the 2026-08-05 contract
# change allows. reconcile() takes the LIST of every printer that should have a
# detector, and keys its processes by serial.
T1 = {"serial": "S1", "camera_source": "a1", "camera_index": 0, "conf": 0.25,
      "host": "1.2.3.4", "access_code": "CODE1"}
T2 = {"serial": "S1", "camera_source": "webcam", "camera_index": 2, "conf": 0.4,
      "host": "1.2.3.4", "access_code": "CODE1"}
U1 = {"serial": "S2", "camera_source": "a1", "camera_index": 0, "conf": 0.3,
      "host": "5.6.7.8", "access_code": "CODE2"}
U2 = {"serial": "S2", "camera_source": "a1", "camera_index": 0, "conf": 0.6,
      "host": "5.6.7.8", "access_code": "CODE2"}


def out_arg(proc):
    """The --out directory a spawned detector was given."""
    return proc.argv[proc.argv.index("--out") + 1]


def test_spawns_for_a_new_target(tmp_path):
    sup, spawned = supervisor(tmp_path, lambda: 0.0)
    sup.reconcile([T1])
    assert len(spawned) == 1
    assert "--source" in spawned[0].argv and "a1" in spawned[0].argv
    assert "--host" in spawned[0].argv and "1.2.3.4" in spawned[0].argv


def test_two_targets_spawn_two_detectors_in_their_own_out_dirs(tmp_path):
    # Two camera printers are two independent streams (each printer's own
    # built-in camera on its own address), so they get two processes -- and each
    # must write into ITS OWN directory. One shared --out and both detectors
    # overwrite each other's status.json + latest.jpg, so every printer would
    # show whichever one wrote last: the exact lie `capture` exists to prevent.
    sup, spawned = supervisor(tmp_path, lambda: 0.0)
    sup.reconcile([T1, U1])
    assert len(spawned) == 2
    outs = [out_arg(p) for p in spawned]
    assert outs[0] != outs[1]
    assert outs[0] == str(out_dir_for(tmp_path, "S1")) and "S1" in outs[0]
    assert outs[1] == str(out_dir_for(tmp_path, "S2")) and "S2" in outs[1]


def test_argv_never_contains_access_code(tmp_path):
    # The code must reach each child through its OWN env, never argv: argv is
    # visible to anyone who can list processes. With several a1 cameras there is
    # more than one secret in play, so also prove no child gets the other's.
    sup, spawned = supervisor(tmp_path, lambda: 0.0)
    sup.reconcile([T1, U1])
    for p in spawned:
        assert not any("code" in str(a) for a in p.argv)
        assert not any("CODE1" in str(a) or "CODE2" in str(a) for a in p.argv)
    assert spawned[0].env["BAMBU_ACCESS_CODE"] == "CODE1"
    assert spawned[1].env["BAMBU_ACCESS_CODE"] == "CODE2"


def test_changed_target_restarts(tmp_path):
    sup, spawned = supervisor(tmp_path, lambda: 0.0)
    sup.reconcile([T1])
    sup.reconcile([T2])
    assert spawned[0].terminated is True
    assert len(spawned) == 2
    assert "2" in spawned[1].argv


def test_changed_target_restarts_only_that_printer(tmp_path):
    # Every restart drops a camera connection and loses frames, so reconciling a
    # change on one printer must not touch another whose target did not change.
    sup, spawned = supervisor(tmp_path, lambda: 0.0)
    sup.reconcile([T1, U1])
    a_proc = spawned[0]
    sup.reconcile([T1, U2])                  # only S2's conf changed
    assert sup._procs["S1"] is a_proc         # the identical object, not a twin
    assert a_proc.terminated is False         # and never even asked to exit
    assert len(spawned) == 3                  # exactly one new process
    assert spawned[1].terminated is True      # S2's old detector reaped
    assert out_arg(spawned[2]) == str(out_dir_for(tmp_path, "S2"))
    assert "0.6" in spawned[2].argv


def test_none_target_stops(tmp_path):
    sup, spawned = supervisor(tmp_path, lambda: 0.0)
    sup.reconcile([T1])
    sup.reconcile(None)
    assert spawned[0].terminated is True


def test_dropping_one_printer_leaves_the_others_detector_alone(tmp_path):
    # "Gone from the list" is now per printer, not "stop everything": unmarking
    # one camera printer must not take the other printer's camera down with it.
    sup, spawned = supervisor(tmp_path, lambda: 0.0)
    sup.reconcile([T1, U1])
    sup.reconcile([U1])
    assert spawned[0].terminated is True
    assert spawned[1].terminated is False
    assert spawned[1].poll() is None          # S2's detector still running
    assert list(sup._procs) == ["S2"]
    assert len(spawned) == 2                   # S2 was not respawned either


def test_crash_respawns_after_backoff(tmp_path):
    clk = Clock()
    sup, spawned = supervisor(tmp_path, clk)
    sup.reconcile([T1])
    spawned[0].die()
    clk.t = 2.0
    sup.reconcile([T1])               # within backoff -> no respawn
    assert len(spawned) == 1
    clk.t = 6.0
    sup.reconcile([T1])               # backoff elapsed -> respawn
    assert len(spawned) == 2


def test_crash_respawns_only_the_printer_that_crashed(tmp_path):
    # A dead detector is detected per serial (proc.poll()), so one printer's
    # crash-respawn must not also restart the healthy printer's detector and
    # drop a camera connection that was working fine.
    clk = Clock()
    sup, spawned = supervisor(tmp_path, clk)
    sup.reconcile([T1, U1])
    spawned[1].die()                  # S2's detector crashed
    clk.t = 6.0
    sup.reconcile([T1, U1])
    assert len(spawned) == 3
    assert out_arg(spawned[2]) == str(out_dir_for(tmp_path, "S2"))
    assert sup._procs["S1"] is spawned[0]     # S1 untouched...
    assert spawned[0].terminated is False     # ...and still running


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


def test_build_argv_passes_the_capture_interval(tmp_path):
    # The cadence knob the detector actually honours: one frame every N seconds.
    sup, _ = supervisor(tmp_path, lambda: 0.0)
    argv = sup.build_argv(T1)
    assert "--interval" in argv
    assert argv[argv.index("--interval") + 1] == str(DEFAULT_INTERVAL_S)


def test_stop_waits_for_the_process_to_release_the_camera(tmp_path):
    # terminate() is asynchronous on Windows. Respawning before the old process
    # has actually exited means the new one finds the device still held and dies
    # with "cannot open camera index N" -- a respawn loop that looks exactly like
    # the camera connecting and disconnecting over and over.
    sup, spawned = supervisor(tmp_path, lambda: 0.0)
    sup.reconcile([T1])
    sup.reconcile([T2])
    assert spawned[0].terminated is True
    assert spawned[0].waited is not None      # reaped before the new spawn


def test_stale_window_accommodates_the_capture_interval(tmp_path):
    # A 5 s capture interval rewrites status.json only every 5 s. A 3 s stale
    # window would mark a perfectly healthy detector "down" between every frame
    # -- and feed [] to the controller, silently disabling auto-stop. Every
    # printer's reader is built from the same window (one reader per serial now).
    co = DetectionCoordinator(FakeReg([]), tmp_path, FakeRunner(),
                              interval_s=5.0)
    assert co._reader_for("S1").stale_after > 5.0
    assert co._reader_for("S2").stale_after > 5.0


def test_build_env_carries_code_for_a1_only(tmp_path):
    sup, _ = supervisor(tmp_path, lambda: 0.0)
    a1_env = sup.build_env(T1)
    assert a1_env["BAMBU_ACCESS_CODE"] == "CODE1"
    assert sup.build_env(T2) is None    # webcam inherits the parent env


from server.detection import DetectionCoordinator


class FakeRunner:
    def __init__(self): self.targets = []; self.stopped = False
    def reconcile(self, targets): self.targets.append(targets)
    def stop(self): self.stopped = True


# The two camera printers the coordinator tests use. Both at once is the point:
# capture is a plain per-printer flag now, not an exclusive one.
A = {"serial": "S1", "camera_index": 0, "conf": 0.5}
B = {"serial": "S2", "camera_index": 0, "conf": 0.5}


class FakeReg:
    def __init__(self, targets, gstate="RUNNING"):
        # A LIST of targets: every one of them is a camera printer with
        # detection enabled, so detection_targets() and capture_serials() agree.
        self._targets = list(targets or [])
        self._gstate = gstate
        self._gstates = {}
        self.stopped = 0
        self.stops = []          # which serials were stopped, in order
    def detection_targets(self): return list(self._targets)
    def capture_serials(self): return [t["serial"] for t in self._targets]
    def detection_config(self, serial):
        # Mirrors PrinterRegistry.detection_config's keys EXACTLY, roi included.
        # The fake omitting roi is how a snapshot that never sent the printer's
        # saved region went unnoticed -- the UI seeds its ROI editor from it.
        # Per-serial, so a snapshot handing back the wrong printer's region
        # fails a test instead of quietly cropping the wrong bed.
        rois = {t["serial"]: t.get("roi") for t in self._targets}
        return {"camera_source": "a1", "camera_index": 0, "conf": 0.5,
                "armed_classes": ["spaghetti"], "detect_enabled": True,
                "roi": rois.get(serial)}
    def get(self, serial):
        reg = self
        class S:
            def summary(self_):
                return {"serial": serial,
                        "gcode_state": reg._gstates.get(serial, reg._gstate)}
            def stop_print(self_):
                reg.stopped += 1
                reg.stops.append(serial)
                # Per serial: stopping one printer must never make ANOTHER read
                # as terminal, or a second controller would latch on it.
                reg._gstates[serial] = "FAILED"
        return S()


def detect_dir(co, serial):
    """Where that printer's own detector writes -- per serial, since each camera
    printer has its own detector process and its own status.json/latest.jpg."""
    d = out_dir_for(co.out_dir, serial)
    d.mkdir(parents=True, exist_ok=True)
    return d


def use_test_clock(co, serial, clk, stale_after=3.0):
    """Point one printer's reader at its own directory, on the test clock.
    tick() drops a reader when its printer stops being a camera printer, so
    re-inject after a printer comes back."""
    co._readers[serial] = StatusReader(detect_dir(co, serial),
                                       stale_after=stale_after,
                                       clock=lambda: clk.t)


def write_status(co, serial, ts, *, fps=4.0,
                 detections=({"cls": "spaghetti", "conf": 0.9},)):
    (detect_dir(co, serial) / "status.json").write_text(json.dumps(
        {"ts": ts, "fps": fps, "camera": 0, "conf": 0.5,
         "detections": list(detections), "error": None}))


def test_tick_reconciles_the_runner_with_every_target(tmp_path):
    reg = FakeReg([A, B])
    runner = FakeRunner()
    co = DetectionCoordinator(reg, tmp_path, runner)
    co.tick()
    # The runner is handed the whole LIST -- one detector per camera printer,
    # not one detector for whichever printer happened to be chosen.
    assert [t["serial"] for t in runner.targets[-1]] == ["S1", "S2"]


def test_tick_fires_stop_after_sustained_fault(tmp_path):
    reg = FakeReg([A])
    runner = FakeRunner()
    clk = Clock()
    co = DetectionCoordinator(reg, tmp_path, runner,
                              controller_factory=lambda: AutoStopController(clock=clk))
    use_test_clock(co, "S1", clk)
    co.arm("S1", True)
    write_status(co, "S1", clk.t); co.tick()                 # t=0 fault begins
    clk.t = 10.0; write_status(co, "S1", clk.t); co.tick()   # sustained -> fire
    assert reg.stopped == 1


def test_a_sustained_fault_stops_only_that_printer(tmp_path):
    # The one that matters most with several cameras: arming and the fault timer
    # are per printer, so a spaghetti nest on S1 must stop S1 and NOTHING else.
    # A shared controller (or a shared status file) would stop a printer that is
    # printing perfectly well -- both machines see the same detection here, and
    # only the armed one may be touched.
    reg = FakeReg([A, B])
    clk = Clock()
    co = DetectionCoordinator(reg, tmp_path, FakeRunner(),
                              controller_factory=lambda: AutoStopController(clock=clk))
    use_test_clock(co, "S1", clk)
    use_test_clock(co, "S2", clk)
    co.arm("S1", True)
    assert co.snapshot("S1")["armed"] is True
    assert co.snapshot("S2")["armed"] is False       # arming S1 never armed S2
    for t in (0.0, 10.0, 20.0):
        clk.t = t
        write_status(co, "S1", clk.t)
        write_status(co, "S2", clk.t)                 # S2 faulting just as hard
        co.tick()
    assert reg.stops == ["S1"]                        # S1 stopped, once
    assert "S2" not in reg.stops                      # S2 never touched
    assert co.snapshot("S1")["stopped_by_monitor"] is True
    assert co.snapshot("S2")["stopped_by_monitor"] is False


def test_stale_status_does_not_fire(tmp_path):
    reg = FakeReg([A])
    clk = Clock()
    co = DetectionCoordinator(reg, tmp_path, FakeRunner(),
                              controller_factory=lambda: AutoStopController(clock=clk))
    use_test_clock(co, "S1", clk)
    write_status(co, "S1", 0.0)
    co.arm("S1", True)
    for t in (0, 11, 22, 33):                # status is always stale (ts=0)
        clk.t = float(t); co.tick()
    assert reg.stopped == 0                   # never acts on stale detections


def test_snapshot_none_for_non_capture(tmp_path):
    reg = FakeReg([A])
    co = DetectionCoordinator(reg, tmp_path, FakeRunner())
    assert co.snapshot("OTHER") is None
    snap = co.snapshot("S1")
    assert snap["armed_classes"] == ["spaghetti"]


def test_snapshot_includes_camera_source(tmp_path):
    reg = FakeReg([A])
    co = DetectionCoordinator(reg, tmp_path, FakeRunner())
    assert co.snapshot("S1")["camera_source"] == "a1"


def test_snapshot_returns_each_printers_own_saved_roi(tmp_path):
    """The snapshot must carry the ROI, and the RIGHT printer's.

    It did not carry it at all for a long time, while PUT /detection accepted
    one and PrinterConfig stored it. The Detection page seeds its four % inputs
    and its draggable box from this field, so a saved region never came back:
    both always showed the hardcoded A1 default, and "Use whole frame"
    (disabled on a falsy roi) could never be pressed.

    That was worse than cosmetic. The page tells the operator that the
    draggable box and the outline burned into the JPEG "match once you hit
    Apply" -- so, seeing them differ, the fix was to press Apply, writing the
    default over a correct region. On an A1 mini the A1 default crops the bed
    out of frame entirely, which section 4.1 calls out as a SILENT false
    negative: the detector sees the room and reports all clear.

    Two printers with different regions, because with several camera printers
    the failure mode is no longer just "the default" but "the other machine's
    box" -- and the two are indistinguishable to anyone reading the screen.
    """
    a = {**A, "roi": [0.10, 0.20, 0.30, 0.40]}
    b = {**B, "roi": [0.55, 0.60, 0.35, 0.30]}
    co = DetectionCoordinator(FakeReg([a, b]), tmp_path, FakeRunner())

    assert co.snapshot("S1")["roi"] == [0.10, 0.20, 0.30, 0.40]
    assert co.snapshot("S2")["roi"] == [0.55, 0.60, 0.35, 0.30]


def test_snapshot_roi_is_none_for_whole_frame(tmp_path):
    # None means "whole frame" and must survive as None, not become a box.
    # detect.parse_roi and normalize_roi both degrade a malformed value to
    # None for the same reason: cropping is the dangerous default, not the
    # safe one.
    co = DetectionCoordinator(FakeReg([A]), tmp_path, FakeRunner())
    assert co.snapshot("S1")["roi"] is None


def test_snapshot_reports_each_camera_printers_own_status(tmp_path):
    # Every camera printer reads its OWN detector's status.json. A printer whose
    # detector has not written yet must read "down" -- borrowing the other
    # printer's numbers would claim a camera is up when nothing is watching, and
    # would show a nest detected on the wrong machine.
    reg = FakeReg([A, B, {"serial": "S3", "camera_index": 0, "conf": 0.5}])
    clk = Clock()
    co = DetectionCoordinator(reg, tmp_path, FakeRunner(),
                              controller_factory=lambda: AutoStopController(clock=clk))
    use_test_clock(co, "S1", clk)
    use_test_clock(co, "S2", clk)
    use_test_clock(co, "S3", clk)             # S3 never writes a status
    write_status(co, "S1", clk.t, fps=4.0)
    write_status(co, "S2", clk.t, fps=9.0, detections=())
    co.tick()

    a_snap, b_snap, c_snap = (co.snapshot("S1"), co.snapshot("S2"),
                              co.snapshot("S3"))
    assert a_snap["running"] is True and a_snap["fps"] == 4.0
    assert a_snap["detections"] == [{"cls": "spaghetti", "conf": 0.9}]
    assert b_snap["running"] is True and b_snap["fps"] == 9.0
    assert b_snap["detections"] == []         # S2 is fine, and says so
    assert c_snap["running"] is False         # down, not S1's or S2's status
    assert c_snap["fps"] is None and c_snap["detections"] == []


def test_frame_path_serves_each_printers_own_frame(tmp_path):
    # frame_path takes a serial as of 2026-08-05. Ignoring it and returning one
    # global path would serve whichever detector wrote last under every
    # printer's name -- an operator watching S2 would be looking at S1.
    reg = FakeReg([A, B])
    co = DetectionCoordinator(reg, tmp_path, FakeRunner())
    (detect_dir(co, "S1") / "latest.jpg").write_bytes(b"jpg-S1")
    (detect_dir(co, "S2") / "latest.jpg").write_bytes(b"jpg-S2")
    assert co.frame_path("S1") == out_dir_for(co.out_dir, "S1") / "latest.jpg"
    assert co.frame_path("S1").read_bytes() == b"jpg-S1"
    assert co.frame_path("S2").read_bytes() == b"jpg-S2"
    # A camera printer whose detector has not written a frame yet: None (the
    # route turns that into a 404), never another printer's frame.
    detect_dir(co, "S3")
    assert co.frame_path("S3") is None


def test_losing_the_camera_drops_that_printers_controller(tmp_path):
    # A controller frozen mid-fault for a printer that is no longer a camera
    # printer must not survive: when the printer becomes one again it would
    # otherwise fire on the first frame (stale fault_since), bypassing the 10s
    # debounce. Losing the camera is like a restart for that printer -- it comes
    # back disarmed, matching "arm is runtime-only".
    #
    # Note what is NOT happening here: S2 becoming a camera printer takes
    # nothing away from S1 (see the sibling test below). S1 only loses its
    # controller because it is dropped from the list itself.
    reg = FakeReg([A])
    clk = Clock()
    co = DetectionCoordinator(reg, tmp_path, FakeRunner(),
                              controller_factory=lambda: AutoStopController(clock=clk))
    use_test_clock(co, "S1", clk)
    co.arm("S1", True)
    write_status(co, "S1", clk.t); co.tick()          # t=0: S1 -> armed_faulting
    assert co._controllers["S1"].snapshot()["state"] == "armed_faulting"

    reg._targets = [B]                                 # S1 stops being watched
    clk.t = 500.0; co.tick()
    assert "S1" not in co._controllers                 # controller dropped...
    assert "S1" not in co._readers                     # ...and its reader too

    reg._targets = [A, B]                              # S1 watched again
    use_test_clock(co, "S1", clk)
    clk.t = 501.0; write_status(co, "S1", clk.t); co.tick()
    assert reg.stopped == 0                            # fresh & disarmed: no fire


def test_a_second_camera_printer_does_not_disturb_the_first(tmp_path):
    # The inverse of the old rule, and now worth guarding in its own right: a
    # printer used to LOSE its capture flag (and with it its controller and its
    # arm state) the moment another printer gained one, because the camera was
    # assumed to be a single shared webcam. Every printer has its own built-in
    # camera, so marking S2 must leave S1's controller -- the same object, still
    # armed, still counting the same fault -- completely alone. A future
    # "cleanup" that reintroduces exclusivity would silently disarm a machine
    # that an operator armed.
    reg = FakeReg([A])
    clk = Clock()
    co = DetectionCoordinator(reg, tmp_path, FakeRunner(),
                              controller_factory=lambda: AutoStopController(clock=clk))
    use_test_clock(co, "S1", clk)
    co.arm("S1", True)
    write_status(co, "S1", clk.t); co.tick()           # t=0: S1 -> armed_faulting
    a_ctrl = co._controllers["S1"]

    reg._targets = [A, B]                              # S2 gains a camera too
    clk.t = 5.0; write_status(co, "S1", clk.t); co.tick()
    assert co._controllers["S1"] is a_ctrl             # same controller...
    assert co.snapshot("S1")["armed"] is True          # ...still armed...
    assert co.snapshot("S1")["seconds_to_stop"] == 5.0  # ...same fault window
    assert co.snapshot("S2") is not None               # and S2 is watched too

    clk.t = 10.0; write_status(co, "S1", clk.t); co.tick()
    assert reg.stops == ["S1"]        # the debounce ran to term, uninterrupted


def test_snapshot_uses_cached_status_not_a_live_read(tmp_path):
    # The WS path calls snapshot() every tick and must never touch the
    # filesystem -- tick() already reads status.json every 0.5s, so snapshot()
    # must serve that cached copy. Prove it: tick() once against a running
    # status with a spaghetti detection, delete status.json, then confirm
    # snapshot() still reports running/detections from the cache (a live
    # re-read of the now-missing file would report running False, []).
    reg = FakeReg([A])
    clk = Clock()
    co = DetectionCoordinator(reg, tmp_path, FakeRunner(),
                              controller_factory=lambda: AutoStopController(clock=clk))
    use_test_clock(co, "S1", clk)
    write_status(co, "S1", clk.t)
    co.tick()               # caches a running status with the detection
    (detect_dir(co, "S1") / "status.json").unlink()   # a live re-read: "down"
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


def read_status(path):
    # Single read per poll (not a separate exists-check then a content check) to
    # minimize how often this thread's read races the writer thread's atomic
    # replace; a transient PermissionError/OSError while the file is
    # mid-replace just means "not yet" -- retry like a miss.
    try:
        return json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        return None


def test_mock_runner_reconcile_writes_status_and_is_idempotent(tmp_path):
    # period_s=0.1 (not the 0.02 used elsewhere): fast enough that the first
    # write lands well inside the poll timeout, but slow enough that this
    # test's polling read doesn't collide with the writer thread's temp+
    # os.replace on Windows (a real race there, but a pre-existing property
    # of the atomic-write helper in detect.py, not of the code under test --
    # verified empirically that read+0.02s churn reproduces it and read+0.1s
    # does not; out of scope to change the shared write helper here).
    runner = MockDetectorRunner(tmp_path, period_s=0.1)
    status_path = out_dir_for(tmp_path, "S1") / "status.json"

    def has_spaghetti():
        data = read_status(status_path) or {}
        dets = data.get("detections") or []
        return any(d.get("cls") == "spaghetti" for d in dets)

    try:
        runner.reconcile([{"serial": "S1", "camera_index": 0, "conf": 0.25}])
        assert _wait_until(has_spaghetti), \
            "no spaghetti detection was ever written by the mock writer thread"

        first_thread = runner._writers["S1"][0]
        runner.reconcile([{"serial": "S1", "camera_index": 0, "conf": 0.25}])  # same target again
        assert runner._writers["S1"][0] is first_thread   # idempotent -- no second thread started

        runner.reconcile(None)                  # halts via reconcile(None)
        assert runner._writers == {}

        runner.stop()                           # halting again via stop() must stay safe
        assert runner._writers == {}
    finally:
        runner.stop()                           # never leak a live writer thread on failure


def test_mock_runner_writes_one_status_per_serial_and_halts_them_singly(tmp_path):
    # --mock has to exercise the multi-camera paths too, or the mode used to
    # develop the UI with no hardware can't show two live views at once: one
    # writer thread per serial, each into its OWN out_dir_for directory (a
    # shared file and both mock printers would show the same frame). And each
    # thread gets its own stop Event -- halting one printer's writer with a
    # shared Event would silently stop every other printer's view too.
    runner = MockDetectorRunner(tmp_path, period_s=0.1)
    a_status = out_dir_for(tmp_path, "S1") / "status.json"
    b_status = out_dir_for(tmp_path, "S2") / "status.json"

    def ts(path):
        return (read_status(path) or {}).get("ts")

    try:
        runner.reconcile([{"serial": "S1", "camera_index": 0, "conf": 0.25},
                          {"serial": "S2", "camera_index": 0, "conf": 0.25}])
        assert _wait_until(lambda: ts(a_status) and ts(b_status)), \
            "both mock writers must write under their own serial's directory"
        a_thread, b_thread = runner._writers["S1"][0], runner._writers["S2"][0]
        assert a_thread is not b_thread

        runner.reconcile([{"serial": "S1", "camera_index": 0, "conf": 0.25}])
        assert "S2" not in runner._writers            # S2's writer halted...
        assert b_thread.is_alive() is False           # ...and joined
        assert runner._writers["S1"][0] is a_thread   # S1's never restarted

        # _halt joins, so B's last write has landed by now: whatever timestamp
        # it left behind is final, while A's keeps advancing.
        b_final = ts(b_status)
        a_before = ts(a_status) or 0.0   # a torn read reads as 0 -> just retry
        assert _wait_until(lambda: (ts(a_status) or 0.0) > a_before), \
            "halting S2's writer must leave S1's writer running"
        assert ts(b_status) == b_final                 # S2 really did stop
    finally:
        runner.stop()


def test_build_argv_passes_the_roi_when_set(tmp_path):
    sup, _ = supervisor(tmp_path, lambda: 0.0)
    argv = sup.build_argv({**T2, "roi": [0.1, 0.4, 0.8, 0.5]})
    assert "--roi" in argv
    assert argv[argv.index("--roi") + 1] == "0.1,0.4,0.8,0.5"


def test_build_argv_omits_the_roi_when_unset(tmp_path):
    sup, _ = supervisor(tmp_path, lambda: 0.0)
    assert "--roi" not in sup.build_argv({**T2, "roi": None})
    assert "--roi" not in sup.build_argv(T2)          # key absent entirely
