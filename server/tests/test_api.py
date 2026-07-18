import io
import zipfile

from fastapi.testclient import TestClient

from server.main import create_app
from server.registry import DuplicateSerial
from server.sdcard import SdError
from server.threemf import SLICE_INFO_PATH

# Sentinel used by FakeService below: any queue-route response containing
# this string would mean the access code leaked out of the service and into
# an API response -- see test_queue_routes_never_leak_access_code.
_SECRET_ACCESS_CODE = "FAKE-ACCESS-CODE-MUST-NEVER-LEAK"


def _fixture_3mf(seconds=917, grams=1.69):
    """A tiny in-memory .gcode.3mf, same shape as the real confirmed sample
    (smallCylinderPLA15m17s -> 917s / 1.69g) -- see server/tests/test_threemf.py."""
    xml = (f'<?xml version="1.0" encoding="UTF-8"?>'
           f'<config><plate>'
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
                 sd_file_error=None):
        self._services = {s.serial: s for s in (services or [])}
        self.duplicate = duplicate
        self.added = []
        self.removed = []
        self.updated = []
        self._sd_file = sd_file
        self._sd_file_error = sd_file_error
        self.fetch_sd_file_calls = []

    def summaries(self):
        return [s.summary() for s in self._services.values()]

    def get(self, serial):
        return self._services.get(serial)

    def add(self, host, serial, access_code, name="", capture=False):
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

    def update(self, serial, host=None, access_code=None, name="", capture=False):
        if serial not in self._services:
            return None
        if not (host and host.strip()):
            raise ValueError("host must not be empty")
        svc = self._services[serial]
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
    assert r.json() == {"printers": [{"serial": "S1", "gcode_state": "IDLE",
                                      "connection": "ok",
                                      "report_age_s": 1.0}]}


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
    def __init__(self, capture="S1"):
        self.capture = capture
        self.armed = {}
        self.updated = []
        self._frame = None
    def snapshot(self, serial):
        if serial != self.capture:
            return None
        return {"running": True, "fps": 4.0, "camera_source": "a1",
                "camera_index": 0, "conf": 0.25,
                "detect_enabled": True, "armed": self.armed.get(serial, False),
                "armed_classes": ["spaghetti"], "detections": [],
                "stopped_by_monitor": False, "seconds_to_stop": None,
                "error": None}
    def arm(self, serial, value): self.armed[serial] = value
    def frame_path(self): return self._frame
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
    c, _ = det_client(tmp_path, FakeDetection())
    assert c.get("/api/printers/S1/detection/frame").status_code == 404


def test_detection_frame_served(tmp_path):
    det = FakeDetection()
    frame = tmp_path / "latest.jpg"
    frame.write_bytes(b"\xff\xd8\xff\xe0jpeg")
    det._frame = frame
    c, _ = det_client(tmp_path, det)
    r = c.get("/api/printers/S1/detection/frame")
    assert r.status_code == 200
    assert r.headers["content-type"] == "image/jpeg"


def test_detection_frame_404_for_non_capture_serial(tmp_path):
    # The route must be capture-gated like the other detection routes: a
    # frame file existing is not enough -- the requested serial must be the
    # capture printer, even though frame_path() itself is serial-agnostic.
    det = FakeDetection(capture="S1")
    frame = tmp_path / "latest.jpg"
    frame.write_bytes(b"\xff\xd8\xff\xe0jpeg")
    det._frame = frame
    c, _ = det_client(tmp_path, det)
    assert c.get("/api/printers/S1/detection/frame").status_code == 200
    assert c.get("/api/printers/NOTCAP/detection/frame").status_code == 404


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
