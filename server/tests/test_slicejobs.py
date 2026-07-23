import logging
import pathlib
import threading

import pytest

from server.slicejobs import SliceCoordinator
from server.slicer import SliceError

INDEX = {
    "Bambu Lab A1 0.4 nozzle": {"name": "Bambu Lab A1 0.4 nozzle"},
    "0.20mm Standard @BBL A1": {"name": "0.20mm Standard @BBL A1"},
    "Generic PLA @BBL A1": {"name": "Generic PLA @BBL A1"},
}

# A real .gcode.3mf parses to seconds/grams; fake it at the parse seam.
FAKE_META = {"seconds": 738, "grams": 3.75, "filaments": [],
             "printer_model_id": None}


class FakeRegistry:
    def __init__(self, model_id="N2S", nozzle="0.4",
                 bed_type="Textured PEI Plate"):
        self._model_id, self._nozzle = model_id, nozzle
        self._bed_type = bed_type
        self.uploaded = []
        self.fail_upload = None

    def get(self, serial):
        return object() if serial == "AAA" else None

    def printer_model(self, serial):
        return self._model_id

    def printer_nozzle(self, serial):
        return self._nozzle

    def printer_bed_type(self, serial):
        return self._bed_type

    def upload_sd_file(self, serial, path, data):
        if self.fail_upload:
            raise self.fail_upload
        self.uploaded.append((serial, path, data))


class FakeQueue:
    def __init__(self):
        self.jobs = []

    def add(self, serial, job):
        self.jobs.append((serial, job))


def make(tmp_path, *, run=None, registry=None, queue=None,
         max_finished_jobs=None):
    def ok_run(exe, model, machine, process, filament, out_dir, **kw):
        out = pathlib.Path(out_dir) / "sliced.gcode.3mf"
        out.write_bytes(b"PK\x03\x04fake")
        return out
    kwargs = {}
    if max_finished_jobs is not None:
        kwargs["max_finished_jobs"] = max_finished_jobs
    return SliceCoordinator(
        registry or FakeRegistry(), queue if queue is not None else FakeQueue(),
        "bs.exe", INDEX, work_dir=tmp_path,
        run=run or ok_run, parse=lambda data: dict(FAKE_META), **kwargs)


def test_a_submitted_job_starts_queued(tmp_path):
    c = make(tmp_path)
    job_id = c.submit("AAA", "part.stl", b"solid", "standard", "PLA", False)
    assert c.get(job_id)["state"] == "queued"


def test_submit_rejects_an_unknown_printer(tmp_path):
    with pytest.raises(KeyError):
        make(tmp_path).submit("ZZZ", "p.stl", b"x", "standard", "PLA", False)


def test_submit_rejects_an_unresolvable_preset(tmp_path):
    c = make(tmp_path, registry=FakeRegistry(nozzle="0.8"))
    with pytest.raises(ValueError, match="preset"):
        c.submit("AAA", "p.stl", b"x", "standard", "PLA", False)


def test_a_successful_job_slices_uploads_and_queues(tmp_path):
    reg, q = FakeRegistry(), FakeQueue()
    c = make(tmp_path, registry=reg, queue=q)
    job_id = c.submit("AAA", "part.stl", b"solid", "standard", "PLA", False)
    c.run_once()

    job = c.get(job_id)
    assert job["state"] == "done"
    assert job["seconds"] == 738 and job["grams"] == 3.75

    serial, path, data = reg.uploaded[0]
    assert (serial, path) == ("AAA", "/part.gcode.3mf")
    assert data == b"PK\x03\x04fake"

    qserial, qjob = q.jobs[0]
    assert (qserial, qjob["sd_path"]) == ("AAA", "/part.gcode.3mf")
    assert qjob["source"] == "3mf"
    # The CLI omits printer_model_id, so provenance supplies it -- we know
    # which printer we sliced FOR. Without this the model guard is skipped.
    assert qjob["model_id"] == "N2S"


def test_the_uploaded_name_is_the_stl_name_with_a_3mf_extension(tmp_path):
    reg = FakeRegistry()
    c = make(tmp_path, registry=reg)
    c.submit("AAA", r"C:\evil\..\sub\Benchy.STL", b"x", "standard", "PLA", False)
    c.run_once()
    assert reg.uploaded[0][1] == "/Benchy.gcode.3mf"


def test_a_slice_failure_latches_and_leaves_the_queue_untouched(tmp_path):
    def boom(*a, **kw):
        raise SliceError("got error when validate: boom")
    q = FakeQueue()
    c = make(tmp_path, run=boom, queue=q)
    job_id = c.submit("AAA", "p.stl", b"x", "standard", "PLA", False)
    c.run_once()
    job = c.get(job_id)
    assert job["state"] == "failed"
    assert "boom" in job["error"]
    assert q.jobs == []


def test_an_upload_failure_leaves_the_queue_untouched(tmp_path):
    # Same principle as start's "dequeue only on confirmation": a step that
    # did not happen must not leave a half-finished job behind.
    reg = FakeRegistry()
    reg.fail_upload = RuntimeError("ftps died")
    q = FakeQueue()
    c = make(tmp_path, registry=reg, queue=q)
    job_id = c.submit("AAA", "p.stl", b"x", "standard", "PLA", False)
    c.run_once()
    assert c.get(job_id)["state"] == "failed"
    assert q.jobs == []


def test_the_work_directory_is_cleaned_up_on_success_and_failure(tmp_path):
    c = make(tmp_path)
    c.submit("AAA", "p.stl", b"x", "standard", "PLA", False)
    c.run_once()

    def boom(*a, **kw):
        raise SliceError("nope")
    c2 = make(tmp_path, run=boom)
    c2.submit("AAA", "p.stl", b"x", "standard", "PLA", False)
    c2.run_once()

    assert list(tmp_path.iterdir()) == []


def test_supports_flag_reaches_the_runner(tmp_path):
    seen = {}

    def spy(exe, model, machine, process, filament, out_dir, **kw):
        seen.update(kw)
        out = pathlib.Path(out_dir) / "sliced.gcode.3mf"
        out.write_bytes(b"x")
        return out
    c = make(tmp_path, run=spy)
    c.submit("AAA", "p.stl", b"x", "standard", "PLA", True)
    c.run_once()
    assert seen["supports"] is True


def test_the_printers_configured_bed_type_reaches_the_runner(tmp_path):
    # MEASURED 2026-07-22: run_slice with curr_bed_type unset defaults to
    # Cool Plate (35 C for PLA); this lab's A1 has a Textured PEI Plate
    # (65 C) and a print with no bed adhesion stalled at 5% with an HMS
    # warning. _do() must read the PRINTER'S configured plate via the
    # registry -- not some hardcoded default -- and pass it through.
    seen = {}

    def spy(exe, model, machine, process, filament, out_dir, **kw):
        seen.update(kw)
        out = pathlib.Path(out_dir) / "sliced.gcode.3mf"
        out.write_bytes(b"x")
        return out
    c = make(tmp_path, run=spy,
             registry=FakeRegistry(bed_type="High Temp Plate"))
    c.submit("AAA", "p.stl", b"x", "standard", "PLA", False)
    c.run_once()
    assert seen["bed_type"] == "High Temp Plate"


def test_bed_type_is_surfaced_on_the_job_record(tmp_path):
    # Alongside material/supports, so the UI and the API can show what a job
    # was actually sliced for.
    reg = FakeRegistry(bed_type="Supertack Plate")
    c = make(tmp_path, registry=reg)
    job_id = c.submit("AAA", "p.stl", b"x", "standard", "PLA", False)
    c.run_once()
    assert c.get(job_id)["bed_type"] == "Supertack Plate"


def test_jobs_are_listed_newest_first_and_filtered_by_serial(tmp_path):
    c = make(tmp_path)
    a = c.submit("AAA", "one.stl", b"x", "standard", "PLA", False)
    b = c.submit("AAA", "two.stl", b"x", "standard", "PLA", False)
    assert [j["id"] for j in c.list("AAA")] == [b, a]
    assert c.list("BBB") == []


def test_cancelling_a_queued_job_stops_it_running(tmp_path):
    q = FakeQueue()
    c = make(tmp_path, queue=q)
    job_id = c.submit("AAA", "p.stl", b"x", "standard", "PLA", False)
    assert c.cancel(job_id) is True
    c.run_once()
    assert c.get(job_id)["state"] == "cancelled"
    assert q.jobs == []


def test_run_once_is_a_noop_with_nothing_queued(tmp_path):
    make(tmp_path).run_once()


# -- Issue 1: stop()/start() thread lifecycle --------------------------------
#
# Nothing above touches start()/stop() at all -- that's exactly why the
# double-worker bug was invisible. run_slice allows the CLI subprocess up to
# SLICE_TIMEOUT_S (900s), so a stop() called mid-slice can reliably outlive
# any short join timeout; if stop() clears self._thread anyway, a later
# start() sees None and spawns a SECOND worker on top of one still alive
# inside _do() -- two slices (two Bambu Studio subprocesses) running at once,
# defeating the entire point of the single global worker (module docstring).

def test_start_twice_creates_only_one_thread(tmp_path):
    c = make(tmp_path)
    try:
        c.start()
        first = c._thread
        c.start()                      # must be a no-op: already running
        assert c._thread is first
    finally:
        c.stop()


def test_stop_then_start_after_a_clean_stop_yields_one_new_thread(tmp_path):
    c = make(tmp_path)
    try:
        c.start()
        first = c._thread
        c.stop()
        assert c._thread is None       # a clean stop DOES clear the reference
        c.start()
        second = c._thread
        assert second is not None and second is not first
        assert second.is_alive()
    finally:
        c.stop()


def test_stop_on_a_wedged_worker_leaves_it_running_and_start_does_not_spawn_a_second(
        tmp_path):
    # Deterministic despite the blocking runner: `unblock` is set in a
    # `finally` so this test cannot hang the suite even if an assertion
    # above it fails.
    entered = threading.Event()
    unblock = threading.Event()

    def wedged_run(exe, model, machine, process, filament, out_dir, **kw):
        entered.set()
        unblock.wait(5.0)
        out = pathlib.Path(out_dir) / "sliced.gcode.3mf"
        out.write_bytes(b"x")
        return out

    c = make(tmp_path, run=wedged_run)
    c.submit("AAA", "p.stl", b"x", "standard", "PLA", False)
    c.start()
    first_thread = None
    try:
        assert entered.wait(2.0), "worker never picked up the job"
        first_thread = c._thread

        # timeout=0.2: short so the test runs fast. The worker is still
        # blocked inside wedged_run(), so this join always times out.
        c.stop(timeout=0.2)

        # The wedged worker is still alive -- stop() must not let go of its
        # reference to it just because the join timed out.
        assert c._thread is first_thread
        assert first_thread.is_alive()

        # A second start() while the first worker is still alive must be a
        # no-op, not a second worker spawned on top of it.
        c.start()
        assert c._thread is first_thread
    finally:
        unblock.set()
        if first_thread is not None:
            first_thread.join(timeout=5.0)
            assert not first_thread.is_alive()


# -- Issue 2: a failure's traceback must survive into the log ----------------

def test_a_slice_failure_is_logged_with_a_traceback_not_just_a_message(
        tmp_path, caplog):
    def boom(*a, **kw):
        raise SliceError("got error when validate: boom")
    c = make(tmp_path, run=boom)
    job_id = c.submit("AAA", "p.stl", b"x", "standard", "PLA", False)
    with caplog.at_level(logging.WARNING, logger="server.slicejobs"):
        c.run_once()
    records = [r for r in caplog.records if job_id in r.getMessage()]
    assert records, "no log record mentioned the failed job"
    # exc_info is what log.exception (vs. log.warning) attaches -- its
    # presence is what lets a real bug's traceback survive into the log
    # instead of being indistinguishable from an ordinary, expected
    # SliceError like this one.
    assert records[0].exc_info is not None


# -- Issue 3: bounded job history ---------------------------------------------

def test_finished_jobs_beyond_the_cap_evict_the_oldest_first(tmp_path):
    c = make(tmp_path, max_finished_jobs=2)
    ids = []
    for i in range(3):
        ids.append(c.submit("AAA", f"p{i}.stl", b"x", "standard", "PLA", False))
        c.run_once()
    assert c.get(ids[0]) is None                 # oldest finished -- evicted
    assert c.get(ids[1]) is not None
    assert c.get(ids[2]) is not None
    assert [j["id"] for j in c.list("AAA")] == [ids[2], ids[1]]


def test_an_active_job_is_never_evicted_by_the_cap(tmp_path):
    # A cap-oblivious "keep only the N most recent job records" would wrongly
    # sweep up a still-queued job the moment the total job count passes the
    # cap. Only TERMINAL (done/failed/cancelled) jobs are history; an active
    # one must never be evicted, however far past the cap the total grows.
    c = make(tmp_path, max_finished_jobs=1)
    a = c.submit("AAA", "a.stl", b"x", "standard", "PLA", False)
    c.run_once()                                  # a -> done
    b = c.submit("AAA", "b.stl", b"x", "standard", "PLA", False)
    active = c.submit("AAA", "active.stl", b"x", "standard", "PLA", False)
    c.run_once()                                  # b -> done; evicts a (cap=1)
    # `active` is still queued behind nothing else -- never run_once()'d.
    assert c.get(a) is None
    assert c.get(b) is not None
    assert c.get(active)["state"] == "queued"
