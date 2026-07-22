import pathlib

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
    def __init__(self, model_id="N2S", nozzle="0.4"):
        self._model_id, self._nozzle = model_id, nozzle
        self.uploaded = []
        self.fail_upload = None

    def get(self, serial):
        return object() if serial == "AAA" else None

    def printer_model(self, serial):
        return self._model_id

    def printer_nozzle(self, serial):
        return self._nozzle

    def upload_sd_file(self, serial, path, data):
        if self.fail_upload:
            raise self.fail_upload
        self.uploaded.append((serial, path, data))


class FakeQueue:
    def __init__(self):
        self.jobs = []

    def add(self, serial, job):
        self.jobs.append((serial, job))


def make(tmp_path, *, run=None, registry=None, queue=None):
    def ok_run(exe, model, machine, process, filament, out_dir, **kw):
        out = pathlib.Path(out_dir) / "sliced.gcode.3mf"
        out.write_bytes(b"PK\x03\x04fake")
        return out
    return SliceCoordinator(
        registry or FakeRegistry(), queue if queue is not None else FakeQueue(),
        "bs.exe", INDEX, work_dir=tmp_path,
        run=run or ok_run, parse=lambda data: dict(FAKE_META))


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
