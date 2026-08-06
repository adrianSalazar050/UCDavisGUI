import io
import pathlib
import zipfile

import pytest
from fastapi.testclient import TestClient

from server.main import create_app
from server.registry import DuplicateSerial
from server.sdcard import SdError
from server.threemf import SLICE_INFO_PATH

# Sentinel used by FakeService below: any queue-route response containing
# this string would mean the access code leaked out of the service and into
# an API response -- see test_queue_routes_never_leak_access_code.
_SECRET_ACCESS_CODE = "FAKE-ACCESS-CODE-MUST-NEVER-LEAK"


def _fixture_3mf(seconds=917, grams=1.69, model_id=None):
    """A tiny in-memory .gcode.3mf, same shape as the real confirmed sample
    (smallCylinderPLA15m17s -> 917s / 1.69g) -- see server/tests/test_threemf.py.
    `model_id` writes printer_model_id (e.g. "N2S" = A1); None omits it, which
    is what an older slicer's output looks like."""
    model = (f'<metadata key="printer_model_id" value="{model_id}"/>'
             if model_id else "")
    xml = (f'<?xml version="1.0" encoding="UTF-8"?>'
           f'<config><plate>'
           f'{model}'
           f'<metadata key="prediction" value="{seconds}"/>'
           f'<metadata key="weight" value="{grams}"/>'
           f'</plate></config>')
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr(SLICE_INFO_PATH, xml)
    return buf.getvalue()


class FakeService:
    def __init__(self, serial="S1", entries=None, error=None,
                 access_code=_SECRET_ACCESS_CODE):
        self.serial = serial
        self.capture = False
        self.access_code = access_code
        self._entries = entries if entries is not None else [
            {"name": "timelapse", "is_dir": True, "size": None, "mtime": None},
            {"name": "Benchy.3mf", "is_dir": False, "size": 12,
             "mtime": "2026-07-16T13:05:00"},
        ]
        self._error = error
        self.list_files_calls = []

    def summary(self):
        return {"serial": self.serial, "gcode_state": "IDLE",
                "connection": "ok", "report_age_s": 1.0}

    def list_files(self, path="/"):
        self.list_files_calls.append(path)
        if self._error:
            raise SdError(self._error)
        return self._entries


class FakeRegistry:
    def __init__(self, services=None, duplicate=False, sd_file=None,
                 sd_file_error=None, sd_upload_error=None, model_id="",
                 bed_type="Textured PEI Plate", nozzle="0.4"):
        self._services = {s.serial: s for s in (services or [])}
        self.duplicate = duplicate
        self.added = []
        self.removed = []
        self.updated = []
        self._sd_file = sd_file
        self._sd_file_error = sd_file_error
        self._sd_upload_error = sd_upload_error
        self.fetch_sd_file_calls = []
        self.upload_sd_file_calls = []
        self.reconnect_calls = []
        self._model_id = model_id
        self._bed_type = bed_type
        self._nozzle = nozzle

    def printer_model(self, serial):
        # Mirrors PrinterRegistry.printer_model: "" for unknown printer or
        # unset model. "" means unknown, which never blocks.
        if serial not in self._services:
            return ""
        return self._model_id

    def printer_bed_type(self, serial):
        # Mirrors PrinterRegistry.printer_bed_type: the configured default
        # even for an unknown printer -- never "" (see store.DEFAULT_BED_TYPE).
        if serial not in self._services:
            return "Textured PEI Plate"
        return self._bed_type

    def printer_nozzle(self, serial):
        # Mirrors PrinterRegistry.printer_nozzle: the configured default even
        # for an unknown printer -- never "" (see store.DEFAULT_NOZZLE).
        if serial not in self._services:
            return "0.4"
        return self._nozzle

    def summaries(self):
        return [s.summary() for s in self._services.values()]

    def get(self, serial):
        return self._services.get(serial)

    def add(self, host, serial, access_code, name="", capture=False,
            model_id=None):
        if self.duplicate:
            raise DuplicateSerial(serial)
        if not (host and serial and access_code):
            raise ValueError("host, serial and access_code are all required")
        svc = FakeService(serial)
        self._services[serial] = svc
        self.added.append((host, serial, access_code, name, capture))
        return svc.summary()

    def remove(self, serial):
        if serial not in self._services:
            return False
        del self._services[serial]
        self.removed.append(serial)
        return True

    def update(self, serial, host=None, access_code=None, name="",
               capture=False, model_id=None, bed_type=None, nozzle=None):
        if serial not in self._services:
            return None
        if not (host and host.strip()):
            raise ValueError("host must not be empty")
        svc = self._services[serial]
        if model_id is not None:
            self._model_id = model_id
        if bed_type is not None:
            self._bed_type = bed_type
        if nozzle is not None:
            self._nozzle = nozzle
        self.updated.append((serial, host, access_code, name, capture))
        return svc.summary()

    def fetch_sd_file(self, serial, path):
        # Mirrors the real PrinterRegistry.fetch_sd_file's contract: always
        # SdError on failure (unknown serial included), never anything else.
        self.fetch_sd_file_calls.append((serial, path))
        if serial not in self._services:
            raise SdError(f"unknown printer {serial}")
        if self._sd_file_error is not None:
            raise self._sd_file_error
        return self._sd_file if self._sd_file is not None else b""

    def reconnect(self, serial):
        # Mirrors PrinterRegistry.reconnect: new summary, or None if unknown.
        if serial not in self._services:
            return None
        self.reconnect_calls.append(serial)
        return self._services[serial].summary()

    def upload_sd_file(self, serial, path, data):
        # Same contract as fetch_sd_file: always SdError on failure, unknown
        # serial included, and the access code never appears in the signature.
        if serial not in self._services:
            raise SdError(f"unknown printer {serial}")
        if self._sd_upload_error is not None:
            raise SdError(self._sd_upload_error)
        self.upload_sd_file_calls.append((serial, path, data))


class FakeQueue:
    """Stands in for PrintQueue: same method surface (add/remove/reorder/get/
    totals), in-memory only. Its own logic is exercised for real in
    server/tests/test_queue.py -- this fake exists to test the ROUTES'
    orchestration in isolation."""

    def __init__(self):
        self._jobs: dict[str, list[dict]] = {}

    def add(self, serial, job):
        self._jobs.setdefault(serial, []).append(dict(job))

    def remove(self, serial, job_id):
        jobs = self._jobs.get(serial, [])
        kept = [j for j in jobs if j["id"] != job_id]
        removed = len(kept) != len(jobs)
        self._jobs[serial] = kept
        return removed

    def reorder(self, serial, ids):
        jobs = self._jobs.get(serial, [])
        by_id = {j["id"]: j for j in jobs}
        self._jobs[serial] = [by_id[i] for i in ids if i in by_id]

    def get(self, serial):
        return list(self._jobs.get(serial, []))

    def totals(self, serial, now=None):
        jobs = self.get(serial)
        seconds = sum(j["seconds"] for j in jobs if j.get("seconds") is not None)
        grams = sum(j["grams"] for j in jobs if j.get("grams") is not None)
        return {"seconds": seconds, "grams": round(grams, 2),
                "finish_epoch": None}


def client(tmp_path, registry=None):
    return TestClient(create_app(registry or FakeRegistry([FakeService()]),
                                 tmp_path))


def queue_client(tmp_path, queue, registry=None):
    reg = registry or FakeRegistry([FakeService("S1")])
    return TestClient(create_app(reg, tmp_path, queue=queue)), reg


def make_frame(runs_dir, run="20260716T000000_x", layer=7):
    frames = runs_dir / run / "frames"
    frames.mkdir(parents=True, exist_ok=True)
    (frames / f"layer_{layer:04d}.jpg").write_bytes(b"\xff\xd8\xff\xe0fake")


# ---------- GET /api/printers ----------

def test_list_printers_envelope(tmp_path):
    r = client(tmp_path).get("/api/printers")
    assert r.status_code == 200
    # detection_available rides on every summary so the client can tell
    # "no capture printer yet" from "this build has no detector" (see
    # _with_detection); this client is built with detection=None.
    assert r.json() == {"printers": [{"serial": "S1", "gcode_state": "IDLE",
                                      "connection": "ok",
                                      "report_age_s": 1.0,
                                      "bed_type": "Textured PEI Plate",
                                      "nozzle": "0.4",
                                      "detection_available": False}]}


def test_list_printers_empty(tmp_path):
    r = client(tmp_path, FakeRegistry([])).get("/api/printers")
    assert r.json() == {"printers": []}


def test_status_route_is_gone(tmp_path):
    assert client(tmp_path).get("/api/status").status_code == 404


# ---------- POST /api/printers ----------

def test_add_printer_201(tmp_path):
    reg = FakeRegistry([])
    r = client(tmp_path, reg).post("/api/printers", json={
        "host": "192.168.137.2", "serial": "S9",
        "access_code": "test-access-code", "name": "bench", "capture": True})
    assert r.status_code == 201
    assert r.json()["serial"] == "S9"
    assert reg.added == [("192.168.137.2", "S9", "test-access-code", "bench", True)]


def test_add_printer_duplicate_409(tmp_path):
    reg = FakeRegistry([], duplicate=True)
    r = client(tmp_path, reg).post("/api/printers", json={
        "host": "1.2.3.4", "serial": "S1", "access_code": "c"})
    assert r.status_code == 409
    assert "already registered" in r.json()["detail"]


def test_add_printer_duplicate_409_does_not_leak_access_code(tmp_path):
    reg = FakeRegistry([], duplicate=True)
    r = client(tmp_path, reg).post("/api/printers", json={
        "host": "1.2.3.4", "serial": "S1", "access_code": "super-secret-pw"})
    assert "super-secret-pw" not in r.text


def test_add_printer_empty_field_400(tmp_path):
    r = client(tmp_path, FakeRegistry([])).post("/api/printers", json={
        "host": "", "serial": "S1", "access_code": "c"})
    assert r.status_code == 400


def test_add_printer_missing_field_422(tmp_path):
    r = client(tmp_path, FakeRegistry([])).post("/api/printers",
                                                json={"host": "1.2.3.4"})
    assert r.status_code == 422  # pydantic rejects it before the route runs


# ---------- DELETE /api/printers/{serial} ----------

def test_remove_printer_204(tmp_path):
    reg = FakeRegistry([FakeService("S1")])
    r = client(tmp_path, reg).delete("/api/printers/S1")
    assert r.status_code == 204
    assert reg.removed == ["S1"]


def test_remove_printer_204_has_empty_body(tmp_path):
    # A 204 with a body is a protocol violation some clients choke on.
    # The route both sets status_code=204 on the decorator AND returns
    # Response(status_code=204) explicitly -- verify that combination
    # doesn't smuggle a JSON "null" body through (which is what a bare
    # `return None` relying only on the decorator would produce).
    reg = FakeRegistry([FakeService("S1")])
    r = client(tmp_path, reg).delete("/api/printers/S1")
    assert r.status_code == 204
    assert r.content == b""


def test_remove_unknown_printer_404(tmp_path):
    r = client(tmp_path, FakeRegistry([])).delete("/api/printers/nope")
    assert r.status_code == 404


# ---------- PUT /api/printers/{serial} ----------

def test_edit_printer_200(tmp_path):
    reg = FakeRegistry([FakeService("S1")])
    r = client(tmp_path, reg).put("/api/printers/S1", json={
        "host": "192.168.1.50", "access_code": "new-secret-code",
        "name": "bench", "capture": True})
    assert r.status_code == 200
    assert "access_code" not in r.json()
    assert "new-secret-code" not in r.text
    assert reg.updated == [("S1", "192.168.1.50", "new-secret-code",
                            "bench", True)]


def test_edit_printer_unknown_404(tmp_path):
    reg = FakeRegistry([])
    r = client(tmp_path, reg).put("/api/printers/nope",
                                  json={"host": "1.2.3.4"})
    assert r.status_code == 404


def test_edit_printer_empty_host_400(tmp_path):
    reg = FakeRegistry([FakeService("S1")])
    r = client(tmp_path, reg).put("/api/printers/S1", json={"host": ""})
    assert r.status_code == 400


def test_edit_printer_missing_host_422(tmp_path):
    r = client(tmp_path, FakeRegistry([FakeService("S1")])).put(
        "/api/printers/S1", json={})
    assert r.status_code == 422  # pydantic rejects it before the route runs


def test_edit_printer_accepts_and_persists_bed_type(tmp_path):
    # This is the whole point of the route change: the field that fixes the
    # measured 35 C-vs-65 C defect must actually be savable from the Edit
    # form, not just from a hand-edited printers.json.
    reg = FakeRegistry([FakeService("S1")])
    r = client(tmp_path, reg).put("/api/printers/S1", json={
        "host": "1.2.3.4", "bed_type": "Cool Plate"})
    assert r.status_code == 200
    assert r.json()["bed_type"] == "Cool Plate"
    # Persists, not just echoed back: a fresh list reflects it too, which is
    # what lets EditPrinterForm show the ACTUAL current plate on reopen
    # instead of always showing the default and silently overwriting a
    # deliberately-chosen plate on the next unrelated save.
    r2 = client(tmp_path, reg).get("/api/printers")
    assert r2.json()["printers"][0]["bed_type"] == "Cool Plate"


def test_edit_printer_omitted_bed_type_keeps_the_current_one(tmp_path):
    reg = FakeRegistry([FakeService("S1")], bed_type="High Temp Plate")
    r = client(tmp_path, reg).put("/api/printers/S1", json={"host": "1.2.3.4"})
    assert r.status_code == 200
    assert r.json()["bed_type"] == "High Temp Plate"


def test_edit_printer_accepts_and_persists_nozzle(tmp_path):
    # Mirrors test_edit_printer_accepts_and_persists_bed_type: nozzle must
    # be savable from the Edit form, not just from a hand-edited
    # printers.json.
    reg = FakeRegistry([FakeService("S1")])
    r = client(tmp_path, reg).put("/api/printers/S1", json={
        "host": "1.2.3.4", "nozzle": "0.6"})
    assert r.status_code == 200
    assert r.json()["nozzle"] == "0.6"
    # Persists, not just echoed back: a fresh list reflects it too, which is
    # what lets EditPrinterForm show the ACTUAL current nozzle on reopen
    # instead of always showing the default and silently overwriting a
    # deliberately-chosen diameter on the next unrelated save.
    r2 = client(tmp_path, reg).get("/api/printers")
    assert r2.json()["printers"][0]["nozzle"] == "0.6"


def test_edit_printer_omitted_nozzle_keeps_the_current_one(tmp_path):
    reg = FakeRegistry([FakeService("S1")], nozzle="0.8")
    r = client(tmp_path, reg).put("/api/printers/S1", json={"host": "1.2.3.4"})
    assert r.status_code == 200
    assert r.json()["nozzle"] == "0.8"


def test_edit_printer_blank_access_code_defaults_to_keep(tmp_path):
    # The frontend never has the real code to send back, so the model
    # default (and an omitted field) must both mean "keep the current one",
    # not "wipe it" -- confirm the route passes "" through untouched rather
    # than substituting something else.
    reg = FakeRegistry([FakeService("S1")])
    r = client(tmp_path, reg).put("/api/printers/S1",
                                  json={"host": "1.2.3.4"})
    assert r.status_code == 200
    assert reg.updated == [("S1", "1.2.3.4", "", "", False)]


# ---------- GET /api/printers/{serial}/files ----------

def test_list_files_default_root(tmp_path):
    r = client(tmp_path).get("/api/printers/S1/files")
    assert r.status_code == 200
    body = r.json()
    assert body["path"] == "/"
    assert [e["name"] for e in body["entries"]] == ["timelapse", "Benchy.3mf"]


def test_list_files_normalises_path(tmp_path):
    r = client(tmp_path).get("/api/printers/S1/files", params={
        "path": "/timelapse/"})
    assert r.json()["path"] == "/timelapse"


def test_list_files_unknown_printer_404(tmp_path):
    r = client(tmp_path).get("/api/printers/nope/files")
    assert r.status_code == 404


def test_list_files_traversal_400(tmp_path):
    r = client(tmp_path).get("/api/printers/S1/files", params={
        "path": "/../etc"})
    assert r.status_code == 400
    assert ".." in r.json()["detail"]


def test_list_files_traversal_400_when_percent_encoded(tmp_path):
    # The query string is percent-encoded on the wire as
    # /api/printers/S1/files?path=%2F..%2Fetc . Starlette/FastAPI decode
    # query params before the route ever sees them, so normalize_path should
    # still receive the literal "/../etc" and reject it. If it didn't get
    # decoded first, this would be a traversal hole.
    r = client(tmp_path).get("/api/printers/S1/files?path=%2F..%2Fetc")
    assert r.status_code == 400
    assert ".." in r.json()["detail"]


# ---------- printer-model mismatch ----------
#
# The safety rule under all of these: UNKNOWN NEVER BLOCKS. A confirmed
# difference between two known model ids may refuse a start; anything else
# must proceed untouched.

_A1_3MF = _fixture_3mf(model_id="N2S")
_MINI_3MF = _fixture_3mf(model_id="N1")


def test_upload_warns_when_the_file_is_for_another_model(tmp_path):
    reg = FakeRegistry([FakeService("S1")], model_id="N2S")
    r = client(tmp_path, reg).post(
        "/api/printers/S1/files",
        files={"file": ("mini.gcode.3mf", _MINI_3MF, "application/octet-stream")})
    assert r.status_code == 201
    assert "A1 mini" in r.json()["warning"]


def test_upload_is_silent_when_the_model_matches(tmp_path):
    reg = FakeRegistry([FakeService("S1")], model_id="N2S")
    r = client(tmp_path, reg).post(
        "/api/printers/S1/files",
        files={"file": ("ok.gcode.3mf", _A1_3MF, "application/octet-stream")})
    assert r.json()["warning"] is None


def test_upload_is_silent_when_the_printer_model_is_unset(tmp_path):
    reg = FakeRegistry([FakeService("S1")], model_id="")
    r = client(tmp_path, reg).post(
        "/api/printers/S1/files",
        files={"file": ("mini.gcode.3mf", _MINI_3MF, "application/octet-stream")})
    assert r.json()["warning"] is None


def test_queue_job_records_the_file_model(tmp_path):
    q = FakeQueue()
    reg = FakeRegistry([FakeService("S1")], sd_file=_MINI_3MF, model_id="N2S")
    c, _ = queue_client(tmp_path, q, registry=reg)
    r = c.post("/api/printers/S1/queue", json={"sd_path": "/mini.gcode.3mf"})
    assert r.status_code == 201
    assert r.json()["model_id"] == "N1"
    # ...and it must survive into the stored job, since start() reads it back
    # from the queue rather than re-fetching the file over FTPS.
    assert q.get("S1")[0]["model_id"] == "N1"


def _model_start(tmp_path, monkeypatch, *, printer_model, job_model):
    """Queue one job with `job_model` against a printer of `printer_model`,
    then try to start it. -> (response, queue)"""
    _fast_verify(monkeypatch)
    q = FakeQueue()
    reg = FakeRegistry([StartableService()], model_id=printer_model)
    c, _ = queue_client(tmp_path, q, registry=reg)
    _queued(q, model_id=job_model)
    return c.post("/api/printers/S1/queue/J1/start"), q


def test_start_refuses_a_confirmed_model_mismatch_and_keeps_the_job(
        tmp_path, monkeypatch):
    r, q = _model_start(tmp_path, monkeypatch, printer_model="N2S",
                        job_model="N1")
    assert r.status_code == 409
    assert "A1 mini" in r.json()["detail"]
    # The job must survive a refusal -- a refused start is not a consumed job.
    assert len(q.get("S1")) == 1


def test_start_allows_a_matching_model(tmp_path, monkeypatch):
    r, _ = _model_start(tmp_path, monkeypatch, printer_model="N2S",
                        job_model="N2S")
    assert r.status_code == 200


def test_start_allows_when_the_printer_model_is_unset(tmp_path, monkeypatch):
    r, _ = _model_start(tmp_path, monkeypatch, printer_model="",
                        job_model="N1")
    assert r.status_code == 200


def test_start_allows_when_the_file_model_is_unknown(tmp_path, monkeypatch):
    # Raw .gcode, and any 3mf whose slicer didn't write printer_model_id.
    r, _ = _model_start(tmp_path, monkeypatch, printer_model="N2S",
                        job_model=None)
    assert r.status_code == 200


# ---------- POST /api/printers/{serial}/reconnect ----------

def test_reconnect_returns_the_new_summary(tmp_path):
    reg = FakeRegistry([FakeService("S1")])
    r = client(tmp_path, reg).post("/api/printers/S1/reconnect")
    assert r.status_code == 200
    assert r.json()["serial"] == "S1"
    assert reg.reconnect_calls == ["S1"]


def test_reconnect_unknown_printer_404(tmp_path):
    reg = FakeRegistry([FakeService("S1")])
    r = client(tmp_path, reg).post("/api/printers/nope/reconnect")
    assert r.status_code == 404
    assert reg.reconnect_calls == []


def test_reconnect_response_never_contains_the_access_code(tmp_path):
    reg = FakeRegistry([FakeService("S1")])
    r = client(tmp_path, reg).post("/api/printers/S1/reconnect")
    assert _SECRET_ACCESS_CODE not in r.text


# ---------- POST /api/printers/{serial}/files (upload) ----------

def test_upload_file_stores_to_sd_root(tmp_path):
    reg = FakeRegistry([FakeService("S1")])
    r = client(tmp_path, reg).post(
        "/api/printers/S1/files",
        files={"file": ("Benchy.gcode.3mf", b"PK\x03\x04zip", "application/octet-stream")})
    assert r.status_code == 201
    assert r.json()["path"] == "/Benchy.gcode.3mf"
    assert reg.upload_sd_file_calls == [("S1", "/Benchy.gcode.3mf", b"PK\x03\x04zip")]


def test_upload_file_accepts_sliced_3mf_without_a_warning(tmp_path):
    reg = FakeRegistry([FakeService("S1")])
    r = client(tmp_path, reg).post(
        "/api/printers/S1/files",
        files={"file": ("Benchy.gcode.3mf", b"PK\x03\x04", "application/octet-stream")})
    assert r.status_code == 201
    assert r.json()["warning"] is None


def test_upload_file_accepts_raw_gcode_but_warns_it_is_not_queue_startable(tmp_path):
    # Real A1 cards are full of raw .gcode (verified against the hardware
    # 2026-07-21), so rejecting it would break the way the card is actually
    # used. But it cannot be launched by the queue: the verified project_file
    # command points at Metadata/plate_N.gcode INSIDE a 3mf container
    # (master.md 5.4), which a raw .gcode has no equivalent of. So: upload it,
    # and say plainly that it is screen-startable only.
    reg = FakeRegistry([FakeService("S1")])
    r = client(tmp_path, reg).post(
        "/api/printers/S1/files",
        files={"file": ("part.gcode", b"G1 X0 Y0", "text/plain")})
    assert r.status_code == 201
    assert reg.upload_sd_file_calls == [("S1", "/part.gcode", b"G1 X0 Y0")]
    warning = r.json()["warning"]
    assert "printer" in warning.lower() and "queue" in warning.lower()


def test_upload_file_rejects_a_file_the_printer_could_never_print(tmp_path):
    reg = FakeRegistry([FakeService("S1")])
    r = client(tmp_path, reg).post(
        "/api/printers/S1/files",
        files={"file": ("notes.txt", b"hello", "text/plain")})
    assert r.status_code == 400
    assert ".gcode" in r.json()["detail"]
    assert reg.upload_sd_file_calls == []


def test_upload_file_strips_any_directory_from_the_filename(tmp_path):
    # Browsers send a bare name, but a scripted client can send anything. The
    # route takes the basename and writes to the card ROOT, so a traversal
    # attempt lands harmlessly at /evil.gcode.3mf -- it can never escape, and
    # it can never hide in a subdirectory where it would be unstartable.
    reg = FakeRegistry([FakeService("S1")])
    r = client(tmp_path, reg).post(
        "/api/printers/S1/files",
        files={"file": ("../../evil.gcode.3mf", b"x", "application/octet-stream")})
    assert r.status_code == 201
    assert r.json()["path"] == "/evil.gcode.3mf"
    assert reg.upload_sd_file_calls == [("S1", "/evil.gcode.3mf", b"x")]


def test_upload_file_strips_windows_directory_separators(tmp_path):
    reg = FakeRegistry([FakeService("S1")])
    r = client(tmp_path, reg).post(
        "/api/printers/S1/files",
        files={"file": (r"C:\Users\me\Benchy.gcode.3mf", b"x",
                        "application/octet-stream")})
    assert r.status_code == 201
    assert r.json()["path"] == "/Benchy.gcode.3mf"


def test_upload_file_rejects_empty_body(tmp_path):
    reg = FakeRegistry([FakeService("S1")])
    r = client(tmp_path, reg).post(
        "/api/printers/S1/files",
        files={"file": ("a.gcode.3mf", b"", "application/octet-stream")})
    assert r.status_code == 400
    assert reg.upload_sd_file_calls == []


def test_upload_file_unknown_printer_404(tmp_path):
    reg = FakeRegistry([FakeService("S1")])
    r = client(tmp_path, reg).post(
        "/api/printers/nope/files",
        files={"file": ("a.gcode.3mf", b"x", "application/octet-stream")})
    assert r.status_code == 404


def test_upload_file_ftps_failure_502(tmp_path):
    reg = FakeRegistry([FakeService("S1")], sd_upload_error="552 Disk full.")
    r = client(tmp_path, reg).post(
        "/api/printers/S1/files",
        files={"file": ("a.gcode.3mf", b"x", "application/octet-stream")})
    assert r.status_code == 502
    assert "Disk full" in r.json()["detail"]


def test_upload_file_502_does_not_leak_access_code(tmp_path):
    # Same convention as test_list_files_502_does_not_leak_access_code: the
    # "never interpolate the code" guarantee lives in sdcard.py (and is tested
    # there). What this checks is that main.py doesn't introduce a SECOND path
    # -- e.g. echoing the printer config back alongside the error detail.
    reg = FakeRegistry([FakeService("S1")],
                       sd_upload_error="Could not upload /a.gcode.3mf to "
                                       "1.2.3.4: 530 Login incorrect.")
    r = client(tmp_path, reg).post(
        "/api/printers/S1/files",
        files={"file": ("a.gcode.3mf", b"x", "application/octet-stream")})
    assert r.status_code == 502
    assert "530 Login incorrect" in r.json()["detail"]
    assert _SECRET_ACCESS_CODE not in r.text


def test_list_files_ftps_failure_502(tmp_path):
    reg = FakeRegistry([FakeService("S1", error="Could not list / on host")])
    r = client(tmp_path, reg).get("/api/printers/S1/files")
    assert r.status_code == 502
    assert "Could not list" in r.json()["detail"]


def test_list_files_502_does_not_leak_access_code(tmp_path):
    # SdError messages are user-facing (server/sdcard.py's own contract), but
    # verify main.py doesn't introduce a second path (e.g. logging the raw
    # access_code into the detail) when propagating the error to the client.
    reg = FakeRegistry([FakeService(
        "S1", error="Could not list / on 1.2.3.4: 530 Login incorrect.")])
    r = client(tmp_path, reg).get("/api/printers/S1/files")
    assert r.status_code == 502
    assert "530 Login incorrect" in r.json()["detail"]


# ---------- unchanged v1 routes ----------

def test_frame_404_when_no_run(tmp_path):
    r = client(tmp_path).get("/api/frame/latest")
    assert r.status_code == 404
    assert r.json() == {"error": "no active run"}


def test_frame_served_with_headers(tmp_path):
    make_frame(tmp_path, layer=7)
    r = client(tmp_path).get("/api/frame/latest")
    assert r.status_code == 200
    assert r.headers["content-type"] == "image/jpeg"
    assert r.headers["x-frame-layer"] == "7"
    assert r.headers["x-frame-run"] == "20260716T000000_x"
    assert r.headers["cache-control"] == "no-store"


def test_ws_sends_envelope_immediately(tmp_path):
    with client(tmp_path).websocket_connect("/ws") as ws:
        msg = ws.receive_json()
        assert msg["printers"][0]["gcode_state"] == "IDLE"


def test_ws_sends_envelope_with_multiple_printers(tmp_path):
    reg = FakeRegistry([FakeService("S1"), FakeService("S2")])
    with client(tmp_path, reg).websocket_connect("/ws") as ws:
        msg = ws.receive_json()
        serials = {p["serial"] for p in msg["printers"]}
        assert serials == {"S1", "S2"}


def test_root_hint_when_no_dist(tmp_path):
    r = client(tmp_path).get("/")
    assert r.status_code == 200
    assert "npm run build" in r.text


# ---------- detection routes + WS merge (Task 11) ----------

class FakeDetection:
    """Stands in for DetectionCoordinator at the route boundary. `capture` is
    the CAMERA PRINTERS -- plural since 2026-08-05, when the capture flag
    stopped being exclusive: every Bambu has its own built-in camera on its own
    address, so N camera printers are N independent streams. A bare serial is
    still accepted, since most tests only need one."""

    def __init__(self, capture="S1"):
        self.capture = [capture] if isinstance(capture, str) else list(capture)
        self.armed = {}
        self.updated = []
        # serial -> that printer's OWN latest.jpg, mirroring the real
        # coordinator: frame_path() takes a serial, and there is one frame per
        # detector rather than one shared global path.
        self._frames = {}
    def snapshot(self, serial):
        if serial not in self.capture:
            return None
        return {"running": True, "fps": 4.0, "camera_source": "a1",
                "camera_index": 0, "conf": 0.25,
                "detect_enabled": True, "armed": self.armed.get(serial, False),
                "armed_classes": ["spaghetti"], "detections": [],
                "stopped_by_monitor": False, "seconds_to_stop": None,
                "error": None}
    def arm(self, serial, value): self.armed[serial] = value
    def frame_path(self, serial): return self._frames.get(serial)
    def start(self): pass
    def stop(self): pass


class DetRegistry(FakeRegistry):
    def detection_config(self, serial):
        return {"camera_index": 0, "conf": 0.25,
                "armed_classes": ["spaghetti"], "detect_enabled": True}
    def update_detection(self, serial, **kw):
        if serial not in self._services:
            return False
        self.updated = getattr(self, "updated", [])
        self.updated.append(kw)
        return True


def det_client(tmp_path, detection, registry=None):
    from server.main import create_app
    reg = registry or DetRegistry([FakeService("S1")])
    return TestClient(create_app(reg, tmp_path, detection=detection)), reg


def two_camera_client(tmp_path, detection):
    """det_client with TWO registered printers, for the multi-camera routes."""
    return det_client(tmp_path, detection,
                      registry=DetRegistry([FakeService("S1"),
                                            FakeService("S2")]))


def make_detector_frame(tmp_path, serial, data=b"\xff\xd8\xff\xe0jpeg"):
    """Write one printer's annotated frame, one directory per serial the way
    detection.out_dir_for lays them out. FakeDetection just hands the route the
    path it's given, but the SHAPE is the point: each detector writes its own
    latest.jpg, so no printer can be served another's camera."""
    path = tmp_path / serial / "latest.jpg"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    return path


def test_get_detection_returns_snapshot(tmp_path):
    c, _ = det_client(tmp_path, FakeDetection())
    r = c.get("/api/printers/S1/detection")
    assert r.status_code == 200
    assert r.json()["armed_classes"] == ["spaghetti"]


def test_get_detection_404_for_non_capture(tmp_path):
    c, _ = det_client(tmp_path, FakeDetection(capture="OTHER"))
    assert c.get("/api/printers/S1/detection").status_code == 404


def test_put_detection_updates_and_returns_snapshot(tmp_path):
    det = FakeDetection()
    c, reg = det_client(tmp_path, det)
    r = c.put("/api/printers/S1/detection",
              json={"camera_index": 2, "conf": 0.4,
                    "armed_classes": ["spaghetti", "cracks"], "detect_enabled": True})
    assert r.status_code == 200
    assert reg.updated[-1]["camera_index"] == 2


def test_put_detection_rejects_unknown_class_400(tmp_path):
    c, _ = det_client(tmp_path, FakeDetection())
    r = c.put("/api/printers/S1/detection", json={"armed_classes": ["banana"]})
    assert r.status_code == 400


def test_put_detection_accepts_camera_source(tmp_path):
    det = FakeDetection()
    c, reg = det_client(tmp_path, det)
    r = c.put("/api/printers/S1/detection", json={"camera_source": "webcam"})
    assert r.status_code == 200
    assert reg.updated[-1]["camera_source"] == "webcam"


def test_put_detection_rejects_bad_camera_source(tmp_path):
    c, _ = det_client(tmp_path, FakeDetection())
    r = c.put("/api/printers/S1/detection", json={"camera_source": "usb"})
    assert r.status_code == 400


def test_arm_toggles_and_returns_snapshot(tmp_path):
    det = FakeDetection()
    c, _ = det_client(tmp_path, det)
    r = c.post("/api/printers/S1/detection/arm", json={"armed": True})
    assert r.status_code == 200
    assert det.armed["S1"] is True


def test_detection_frame_404_when_none(tmp_path):
    # A camera printer whose OWN detector hasn't written a frame yet is a 404 --
    # and stays one while a second camera printer's detector has written its
    # own, because there is no shared global frame to fall back on any more.
    det = FakeDetection(capture=["S1", "S2"])
    det._frames["S2"] = make_detector_frame(tmp_path, "S2")
    c, _ = two_camera_client(tmp_path, det)
    assert c.get("/api/printers/S1/detection/frame").status_code == 404
    assert c.get("/api/printers/S2/detection/frame").status_code == 200


def test_detection_frame_served(tmp_path):
    det = FakeDetection()
    det._frames["S1"] = make_detector_frame(tmp_path, "S1")
    c, _ = det_client(tmp_path, det)
    r = c.get("/api/printers/S1/detection/frame")
    assert r.status_code == 200
    assert r.headers["content-type"] == "image/jpeg"


def test_detection_frame_serves_each_camera_printers_own_bytes(tmp_path):
    # THE guarantee behind per-serial frames: two camera printers, two
    # detectors, two different latest.jpg -- each serial gets its own bytes.
    # Serving one printer's camera under another's name is the exact class of
    # lie the capture flag exists to prevent, and only a byte comparison
    # catches it: both responses are 200 image/jpeg either way, so a route that
    # ignored the serial would look perfectly healthy.
    det = FakeDetection(capture=["S1", "S2"])
    det._frames["S1"] = make_detector_frame(tmp_path, "S1",
                                            b"\xff\xd8\xff\xe0S1-frame")
    det._frames["S2"] = make_detector_frame(tmp_path, "S2",
                                            b"\xff\xd8\xff\xe0S2-frame")
    c, _ = two_camera_client(tmp_path, det)
    r1 = c.get("/api/printers/S1/detection/frame")
    r2 = c.get("/api/printers/S2/detection/frame")
    assert (r1.status_code, r2.status_code) == (200, 200)
    assert r1.content != r2.content
    assert r1.content == b"\xff\xd8\xff\xe0S1-frame"
    assert r2.content == b"\xff\xd8\xff\xe0S2-frame"


def test_summaries_attach_detection_to_every_camera_printer(tmp_path):
    # capture is non-exclusive as of 2026-08-05, and this is what the frontend
    # actually reads: it gates the camera view on a non-null `detection` object
    # per printer (see _with_detection). While marking one printer cleared
    # every other one, the second camera view could never exist -- so a future
    # "cleanup" that reintroduced exclusivity would show up right here.
    det = FakeDetection(capture=["S1", "S2"])
    c, _ = two_camera_client(tmp_path, det)
    printers = {p["serial"]: p
                for p in c.get("/api/printers").json()["printers"]}
    assert set(printers) == {"S1", "S2"}
    for p in printers.values():
        assert p["detection_available"] is True
        assert p["detection"] is not None
        assert p["detection"]["running"] is True


def test_detection_frame_404_for_non_capture_serial(tmp_path):
    # The route must be capture-gated like the other detection routes: a frame
    # file existing is not enough -- the requested serial must be one of the
    # camera printers. frame_path() takes the serial now, so this gate is a
    # second line of defence rather than the only one; the detail text is what
    # says which line refused ("not a camera printer", not "no detector frame").
    det = FakeDetection(capture="S1")
    det._frames["S1"] = make_detector_frame(tmp_path, "S1")
    c, _ = det_client(tmp_path, det)
    assert c.get("/api/printers/S1/detection/frame").status_code == 200
    r = c.get("/api/printers/NOTCAP/detection/frame")
    assert r.status_code == 404
    assert r.json()["detail"] == "not a camera printer"


def test_ws_merges_detection_into_capture_summary(tmp_path):
    c, _ = det_client(tmp_path, FakeDetection())
    with c.websocket_connect("/ws") as ws:
        msg = ws.receive_json()
        p = next(p for p in msg["printers"] if p["serial"] == "S1")
        assert p["detection"]["running"] is True


def test_detection_routes_404_when_detection_disabled(tmp_path):
    # create_app(..., detection=None) -> the whole feature is inert.
    r = client(tmp_path).get("/api/printers/S1/detection")
    assert r.status_code == 404


# ---------- lifespan starts/stops detection (Task 12) ----------

def test_lifespan_starts_and_stops_detection(tmp_path):
    events = []

    class LifecycleDetection(FakeDetection):
        def start(self): events.append("start")
        def stop(self): events.append("stop")

    c, _ = det_client(tmp_path, LifecycleDetection())
    with c:                      # triggers startup + shutdown
        pass
    assert events == ["start", "stop"]


# ---------- GET/POST/DELETE/PUT /api/printers/{serial}/queue (Task 4) ----------

def test_queue_routes_404_when_queue_disabled(tmp_path):
    # create_app(..., queue=None) -> the whole feature is inert, same
    # gating style as detection=None.
    c = client(tmp_path)
    assert c.get("/api/printers/S1/queue").status_code == 404
    assert c.post("/api/printers/S1/queue",
                  json={"sd_path": "/x.3mf"}).status_code == 404
    assert c.delete("/api/printers/S1/queue/j1").status_code == 404
    assert c.put("/api/printers/S1/queue", json={"ids": []}).status_code == 404


def test_get_queue_envelope_empty(tmp_path):
    c, _ = queue_client(tmp_path, FakeQueue())
    r = c.get("/api/printers/S1/queue")
    assert r.status_code == 200
    assert r.json() == {"jobs": [],
                        "totals": {"seconds": 0, "grams": 0,
                                   "finish_epoch": None}}


def test_get_queue_envelope_populated(tmp_path):
    q = FakeQueue()
    q.add("S1", {"id": "a", "sd_path": "/A.3mf", "name": "A.3mf",
                "seconds": 600, "grams": 10.0, "source": "3mf"})
    c, _ = queue_client(tmp_path, q)
    r = c.get("/api/printers/S1/queue")
    assert r.status_code == 200
    body = r.json()
    assert [j["id"] for j in body["jobs"]] == ["a"]
    assert body["totals"]["seconds"] == 600


def test_add_queue_job_parses_fixture_3mf(tmp_path):
    fixture = _fixture_3mf(seconds=917, grams=1.69)
    reg = FakeRegistry([FakeService("S1")], sd_file=fixture)
    c, _ = queue_client(tmp_path, FakeQueue(), reg)
    r = c.post("/api/printers/S1/queue",
               json={"sd_path": "/Benchy.gcode.3mf"})
    assert r.status_code == 201
    body = r.json()
    assert body["seconds"] == 917
    assert body["grams"] == 1.69
    assert body["source"] == "3mf"
    assert body["name"] == "Benchy.gcode.3mf"
    assert body["sd_path"] == "/Benchy.gcode.3mf"
    assert body["id"]  # a non-empty generated id
    assert reg.fetch_sd_file_calls == [("S1", "/Benchy.gcode.3mf")]


def test_add_queue_job_persists_into_queue(tmp_path):
    reg = FakeRegistry([FakeService("S1")], sd_file=_fixture_3mf())
    q = FakeQueue()
    c, _ = queue_client(tmp_path, q, reg)
    r = c.post("/api/printers/S1/queue",
               json={"sd_path": "/Benchy.gcode.3mf"})
    job_id = r.json()["id"]
    assert [j["id"] for j in q.get("S1")] == [job_id]


def test_add_queue_job_fetch_failure_falls_back_to_manual(tmp_path):
    reg = FakeRegistry([FakeService("S1")],
                       sd_file_error=SdError("Could not fetch /x on host"))
    c, _ = queue_client(tmp_path, FakeQueue(), reg)
    r = c.post("/api/printers/S1/queue",
               json={"sd_path": "/Benchy.gcode.3mf"})
    assert r.status_code == 201  # still added, just without parsed metrics
    body = r.json()
    assert body["source"] == "manual"
    assert body["seconds"] is None
    assert body["grams"] is None


def test_add_queue_job_unknown_printer_404(tmp_path):
    reg = FakeRegistry([])
    c, _ = queue_client(tmp_path, FakeQueue(), reg)
    r = c.post("/api/printers/nope/queue", json={"sd_path": "/x.3mf"})
    assert r.status_code == 404


def test_add_queue_job_traversal_400(tmp_path):
    c, _ = queue_client(tmp_path, FakeQueue())
    r = c.post("/api/printers/S1/queue", json={"sd_path": "/../etc/passwd"})
    assert r.status_code == 400
    assert ".." in r.json()["detail"]


def test_add_queue_job_missing_sd_path_422(tmp_path):
    c, _ = queue_client(tmp_path, FakeQueue())
    r = c.post("/api/printers/S1/queue", json={})
    assert r.status_code == 422  # pydantic rejects it before the route runs


def test_remove_queue_job_204(tmp_path):
    q = FakeQueue()
    q.add("S1", {"id": "j1", "sd_path": "/A.3mf", "name": "A.3mf",
                "seconds": 600, "grams": 10.0, "source": "3mf"})
    c, _ = queue_client(tmp_path, q)
    r = c.delete("/api/printers/S1/queue/j1")
    assert r.status_code == 204
    assert r.content == b""  # same "no smuggled null body" rule as printers
    assert q.get("S1") == []


def test_remove_queue_job_404(tmp_path):
    c, _ = queue_client(tmp_path, FakeQueue())
    r = c.delete("/api/printers/S1/queue/nope")
    assert r.status_code == 404


def test_reorder_queue_returns_envelope(tmp_path):
    q = FakeQueue()
    q.add("S1", {"id": "a", "sd_path": "/A.3mf", "name": "A.3mf",
                "seconds": 600, "grams": 10.0, "source": "3mf"})
    q.add("S1", {"id": "b", "sd_path": "/B.3mf", "name": "B.3mf",
                "seconds": 1200, "grams": 5.5, "source": "3mf"})
    c, _ = queue_client(tmp_path, q)
    r = c.put("/api/printers/S1/queue", json={"ids": ["b", "a"]})
    assert r.status_code == 200
    body = r.json()
    assert [j["id"] for j in body["jobs"]] == ["b", "a"]
    assert body["totals"]["seconds"] == 1800
    assert body["totals"]["grams"] == 15.5


def test_queue_routes_never_leak_access_code(tmp_path):
    # FakeService carries a distinctive secret in .access_code (mirroring
    # the real PrinterService/MockPrinter). None of the queue routes ever
    # read that attribute, but this guards against a future regression that
    # accidentally echoes it into a response.
    reg = FakeRegistry([FakeService("S1")], sd_file=_fixture_3mf())
    q = FakeQueue()
    c, _ = queue_client(tmp_path, q, reg)
    responses = [
        c.get("/api/printers/S1/queue"),
        c.post("/api/printers/S1/queue", json={"sd_path": "/A.3mf"}),
        c.put("/api/printers/S1/queue", json={"ids": []}),
    ]
    for r in responses:
        assert _SECRET_ACCESS_CODE not in r.text


def test_add_queue_job_under_real_mock_printer_end_to_end(tmp_path):
    # Full stack, no fakes for registry/service/queue: a real PrinterRegistry
    # + a real MockPrinter + a real PrintQueue, proving --mock's "Add from
    # SD" flow really does yield real-looking time/grams with zero hardware,
    # and that the mock access code sentinel never reaches the response.
    from server.printer import MockPrinter
    from server.queue import PrintQueue, QueueStore
    from server.registry import PrinterRegistry
    from server.store import MemoryStore

    mock_access_code = "00000000"
    registry = PrinterRegistry(
        MemoryStore(),
        lambda cfg: MockPrinter(tmp_path, serial=cfg.serial, host=cfg.host))
    registry.add(host="mock-bench", serial="MOCK1",
                access_code=mock_access_code)
    real_queue = PrintQueue(QueueStore(tmp_path / "queues.json"))
    c = TestClient(create_app(registry, tmp_path, queue=real_queue))

    r = c.post("/api/printers/MOCK1/queue",
               json={"sd_path": "/calibration_cube.gcode.3mf"})
    assert r.status_code == 201
    body = r.json()
    assert body["source"] == "3mf"
    assert body["seconds"] == 917
    assert body["grams"] == 1.69
    assert mock_access_code not in r.text

    got = c.get("/api/printers/MOCK1/queue")
    assert [j["id"] for j in got.json()["jobs"]] == [body["id"]]


# ---------- POST /api/printers/{serial}/queue/{job_id}/start ----------
#
# The contract that matters: a job leaves the queue ONLY when the printer
# confirms it started. MQTT has no ack, so "published" must never be mistaken
# for "printing" -- otherwise a command the printer ignored silently eats a job.

class StartableService(FakeService):
    """FakeService that records start_print and can fake a printer which
    accepts the command, refuses it, or accepts-then-never-starts."""

    def __init__(self, serial="S1", gcode_state="IDLE", busy=None,
                 starts=True):
        super().__init__(serial=serial)
        self._gcode_state = gcode_state
        self._busy = busy
        self._starts = starts
        self.start_calls = []

    def summary(self):
        return {"serial": self.serial, "gcode_state": self._gcode_state,
                "connection": "ok", "report_age_s": 1.0}

    def start_print(self, sd_path, *, plate=1, **options):
        from server.printer import PrinterBusy
        self.start_calls.append((sd_path, plate))
        if self._busy:
            raise PrinterBusy(self._busy)
        if self._starts:
            self._gcode_state = "PREPARE"


def _queued(q, serial="S1", job_id="J1", sd_path="/Benchy.gcode.3mf", **kw):
    job = {"id": job_id, "sd_path": sd_path, "name": "Benchy.gcode.3mf",
           "seconds": 900, "grams": 2.0, "source": "3mf"}
    job.update(kw)
    q.add(serial, job)
    return job


def _fast_verify(monkeypatch):
    """Keep the verify loop from spending real seconds."""
    import server.main as m
    monkeypatch.setattr(m, "START_VERIFY_S", 0.05)
    monkeypatch.setattr(m, "START_POLL_S", 0.0)


def test_start_sends_the_job_and_dequeues_it(tmp_path, monkeypatch):
    _fast_verify(monkeypatch)
    q, svc = FakeQueue(), StartableService()
    _queued(q)
    c, _ = queue_client(tmp_path, q, registry=FakeRegistry([svc]))
    r = c.post("/api/printers/S1/queue/J1/start")
    assert r.status_code == 200
    assert r.json()["started"] is True
    assert svc.start_calls == [("/Benchy.gcode.3mf", 1)]
    assert q.get("S1") == []                     # dequeued only after confirm


def test_start_keeps_the_job_when_the_printer_never_starts(tmp_path, monkeypatch):
    _fast_verify(monkeypatch)
    q = FakeQueue()
    svc = StartableService(starts=False)         # ignores the command silently
    _queued(q)
    c, _ = queue_client(tmp_path, q, registry=FakeRegistry([svc]))
    r = c.post("/api/printers/S1/queue/J1/start")
    assert r.status_code == 200
    assert r.json()["started"] is False
    assert len(q.get("S1")) == 1                 # NOT eaten
    assert "still queued" in r.json()["detail"]


def test_start_409s_when_the_printer_is_busy(tmp_path, monkeypatch):
    _fast_verify(monkeypatch)
    q = FakeQueue()
    svc = StartableService(busy="S1 is already printing (RUNNING)")
    _queued(q)
    c, _ = queue_client(tmp_path, q, registry=FakeRegistry([svc]))
    r = c.post("/api/printers/S1/queue/J1/start")
    assert r.status_code == 409
    assert "already printing" in r.json()["detail"]
    assert len(q.get("S1")) == 1                 # kept


def test_start_refuses_a_job_that_is_not_at_the_head(tmp_path, monkeypatch):
    _fast_verify(monkeypatch)
    q, svc = FakeQueue(), StartableService()
    _queued(q, job_id="J1")
    _queued(q, job_id="J2")
    c, _ = queue_client(tmp_path, q, registry=FakeRegistry([svc]))
    r = c.post("/api/printers/S1/queue/J2/start")
    assert r.status_code == 409
    assert svc.start_calls == []                 # nothing published
    assert len(q.get("S1")) == 2


def test_start_passes_the_plate_through(tmp_path, monkeypatch):
    _fast_verify(monkeypatch)
    q, svc = FakeQueue(), StartableService()
    _queued(q, plate=3)
    c, _ = queue_client(tmp_path, q, registry=FakeRegistry([svc]))
    c.post("/api/printers/S1/queue/J1/start")
    assert svc.start_calls == [("/Benchy.gcode.3mf", 3)]


def test_start_404s_on_unknown_printer_and_empty_queue(tmp_path, monkeypatch):
    _fast_verify(monkeypatch)
    q = FakeQueue()
    c, _ = queue_client(tmp_path, q, registry=FakeRegistry([StartableService()]))
    assert c.post("/api/printers/NOPE/queue/J1/start").status_code == 404
    assert c.post("/api/printers/S1/queue/J1/start").status_code == 404


# ---------- slice routes ----------

class FakeSlicer:
    """Stands in for SliceCoordinator at the route boundary."""

    def __init__(self):
        self.submitted = []
        self.jobs = []
        self.raise_on_submit = None

    def options(self, serial):
        return {"model_id": "N2S", "nozzle": "0.4",
                "presets": [{"id": "standard", "label": "Standard 0.20 mm"}],
                "filaments": [{"material": "PLA",
                               "profile": "Generic PLA @BBL A1"}],
                "detected_filament": "PLA"}

    def submit(self, serial, filename, data, tier, material, supports):
        if self.raise_on_submit:
            raise self.raise_on_submit
        self.submitted.append((serial, filename, data, tier, material,
                               supports))
        return "job-1"

    def list(self, serial=None):
        return self.jobs

    def cancel(self, job_id):
        return job_id == "job-1"


def test_slice_routes_404_when_no_slicer_is_installed():
    # "None means inert", same as queue=None / detection=None. The server must
    # still boot and monitor on a machine with no slicer. All FOUR routes,
    # not just the two GETs -- a regression that inerts only half the
    # surface would otherwise go unnoticed.
    client = TestClient(create_app(FakeRegistry(), pathlib.Path("."),
                                   slicer=None))
    assert client.get("/api/printers/AAA/slice/options").status_code == 404
    assert client.get("/api/slice/jobs").status_code == 404
    res = client.post(
        "/api/printers/AAA/slice",
        files={"file": ("part.stl", b"solid", "application/octet-stream")},
        data={"preset": "standard", "material": "PLA", "supports": "false"})
    assert res.status_code == 404
    assert client.delete("/api/slice/jobs/anything").status_code == 404


def test_slice_options_returns_presets_and_the_detected_filament():
    client = TestClient(create_app(FakeRegistry(), pathlib.Path("."),
                                   slicer=FakeSlicer()))
    body = client.get("/api/printers/AAA/slice/options").json()
    assert body["detected_filament"] == "PLA"
    assert body["presets"][0]["id"] == "standard"


def test_posting_a_model_starts_a_job():
    slicer = FakeSlicer()
    client = TestClient(create_app(FakeRegistry(), pathlib.Path("."),
                                   slicer=slicer))
    res = client.post(
        "/api/printers/AAA/slice",
        files={"file": ("part.stl", b"solid", "application/octet-stream")},
        data={"preset": "standard", "material": "PLA", "supports": "true"})
    assert res.status_code == 202
    assert res.json()["job_id"] == "job-1"
    serial, filename, data, tier, material, supports = slicer.submitted[0]
    assert (serial, filename, data) == ("AAA", "part.stl", b"solid")
    assert (tier, material, supports) == ("standard", "PLA", True)


def test_posting_an_unsupported_extension_is_a_400():
    client = TestClient(create_app(FakeRegistry(), pathlib.Path("."),
                                   slicer=FakeSlicer()))
    res = client.post(
        "/api/printers/AAA/slice",
        files={"file": ("notes.txt", b"hello", "text/plain")},
        data={"preset": "standard", "material": "PLA", "supports": "false"})
    assert res.status_code == 400


def test_posting_an_empty_file_is_a_400():
    client = TestClient(create_app(FakeRegistry(), pathlib.Path("."),
                                   slicer=FakeSlicer()))
    res = client.post(
        "/api/printers/AAA/slice",
        files={"file": ("part.stl", b"", "application/octet-stream")},
        data={"preset": "standard", "material": "PLA", "supports": "false"})
    assert res.status_code == 400


def test_an_unresolvable_preset_is_a_400_not_a_500():
    slicer = FakeSlicer()
    slicer.raise_on_submit = ValueError("no 'standard' preset")
    client = TestClient(create_app(FakeRegistry(), pathlib.Path("."),
                                   slicer=slicer))
    res = client.post(
        "/api/printers/AAA/slice",
        files={"file": ("part.stl", b"solid", "application/octet-stream")},
        data={"preset": "standard", "material": "PLA", "supports": "false"})
    assert res.status_code == 400
    assert "preset" in res.json()["detail"]


def test_an_unknown_printer_is_a_404():
    slicer = FakeSlicer()
    slicer.raise_on_submit = KeyError("ZZZ")
    client = TestClient(create_app(FakeRegistry(), pathlib.Path("."),
                                   slicer=slicer))
    res = client.post(
        "/api/printers/ZZZ/slice",
        files={"file": ("part.stl", b"solid", "application/octet-stream")},
        data={"preset": "standard", "material": "PLA", "supports": "false"})
    assert res.status_code == 404


def test_cancelling_an_unknown_job_is_a_404():
    client = TestClient(create_app(FakeRegistry(), pathlib.Path("."),
                                   slicer=FakeSlicer()))
    assert client.delete("/api/slice/jobs/nope").status_code == 404
    assert client.delete("/api/slice/jobs/job-1").status_code == 204


def test_lifespan_does_not_leak_a_component_if_a_later_start_raises():
    # Two lifecycle components now share one lifespan. If the SECOND start()
    # raises, the app fails to boot -- but the FIRST component (detection)
    # must still be stopped. Before the fix, both starts sat outside the
    # try/finally, so a raise here skipped the finally entirely and left
    # detection running forever.
    events = []

    class LifecycleDetection(FakeDetection):
        def start(self): events.append("detection-start")
        def stop(self): events.append("detection-stop")

    class BoomSlicer(FakeSlicer):
        def start(self): raise RuntimeError("boom")

    det = LifecycleDetection()
    app = create_app(FakeRegistry(), pathlib.Path("."), detection=det,
                     slicer=BoomSlicer())
    with pytest.raises(RuntimeError, match="boom"):
        with TestClient(app):
            pass
    assert events == ["detection-start", "detection-stop"]


# --- detection availability ------------------------------------------------
# BUG reported 2026-07-23 from the packaged desktop app: the Detection page told
# the user to mark <printer> as the capture printer on the printer-management
# page (then "Overview", now "Printers"), but doing so changed nothing -- the
# wording has since been rewritten, the defect it describes has not moved.
# The desktop launcher passes detection=None (torch is
# deliberately out of the bundle), and _with_detection then attached NO
# detection key at all -- so the client could not tell "no capture printer yet"
# from "this build has no detector", and sent the user on an impossible errand.

def test_summaries_flag_detection_as_unavailable_when_it_is_disabled(tmp_path):
    c, _ = det_client(tmp_path, None)
    p = c.get("/api/printers").json()["printers"][0]
    assert p["detection_available"] is False
    # and no detection object is invented for a server that has no detector
    assert p.get("detection") is None


def test_summaries_flag_detection_as_available_when_it_is_wired(tmp_path):
    c, _ = det_client(tmp_path, FakeDetection())
    p = c.get("/api/printers").json()["printers"][0]
    assert p["detection_available"] is True


# --- shared-password auth --------------------------------------------------
# Only engaged when the server is bound beyond loopback (see __main__'s
# fail-closed rule). auth=None -- the desktop app and dev path -- must leave
# every route open exactly as before.

from server.auth import Auth


def auth_client(tmp_path, password="hunter2"):
    from server.main import create_app
    a = Auth(password)
    return TestClient(create_app(DetRegistry([FakeService("S1")]), tmp_path,
                                 auth=a)), a


def test_auth_none_leaves_every_route_open(tmp_path):
    c, _ = det_client(tmp_path, None)
    assert c.get("/api/printers").status_code == 200


def test_api_requires_a_session_when_auth_is_on(tmp_path):
    c, _ = auth_client(tmp_path)
    assert c.get("/api/printers").status_code == 401


def test_login_with_the_right_password_opens_the_api(tmp_path):
    c, _ = auth_client(tmp_path)
    assert c.post("/api/login", json={"password": "hunter2"}).status_code == 200
    # TestClient keeps the cookie, so the next call rides the session
    assert c.get("/api/printers").status_code == 200


def test_login_with_a_wrong_password_is_401_and_opens_nothing(tmp_path):
    c, _ = auth_client(tmp_path)
    assert c.post("/api/login", json={"password": "nope"}).status_code == 401
    assert c.get("/api/printers").status_code == 401


def test_the_login_route_itself_is_reachable_without_a_session(tmp_path):
    c, _ = auth_client(tmp_path)
    # 401 for a bad password, NOT 401 for "no session" -- otherwise nobody
    # could ever log in.
    assert c.post("/api/login", json={"password": "nope"}).status_code == 401


def test_logout_ends_the_session(tmp_path):
    c, _ = auth_client(tmp_path)
    c.post("/api/login", json={"password": "hunter2"})
    assert c.post("/api/logout").status_code == 200
    assert c.get("/api/printers").status_code == 401


def test_the_websocket_is_protected_too(tmp_path):
    # The whole reason auth is a cookie and not a bearer token: /ws must be
    # covered by the same mechanism.
    c, _ = auth_client(tmp_path)
    with pytest.raises(Exception):
        with c.websocket_connect("/ws"):
            pass


def test_the_websocket_opens_once_logged_in(tmp_path):
    c, _ = auth_client(tmp_path)
    c.post("/api/login", json={"password": "hunter2"})
    with c.websocket_connect("/ws") as ws:
        assert "printers" in ws.receive_json()
