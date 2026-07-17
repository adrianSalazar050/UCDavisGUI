from fastapi.testclient import TestClient

from server.main import create_app
from server.registry import DuplicateSerial
from server.sdcard import SdError


class FakeService:
    def __init__(self, serial="S1", entries=None, error=None):
        self.serial = serial
        self.capture = False
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
    def __init__(self, services=None, duplicate=False):
        self._services = {s.serial: s for s in (services or [])}
        self.duplicate = duplicate
        self.added = []
        self.removed = []

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


def client(tmp_path, registry=None):
    return TestClient(create_app(registry or FakeRegistry([FakeService()]),
                                 tmp_path))


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
        return {"running": True, "fps": 4.0, "camera_index": 0, "conf": 0.25,
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
