from fastapi.testclient import TestClient

from server.main import create_app


class FakeService:
    def __init__(self, payload=None):
        self.payload = payload or {"gcode_state": "IDLE", "connection": "ok"}

    def summary(self):
        return dict(self.payload)


def make_frame(runs_dir, run="20260716T000000_x", layer=7):
    frames = runs_dir / run / "frames"
    frames.mkdir(parents=True, exist_ok=True)
    (frames / f"layer_{layer:04d}.jpg").write_bytes(b"\xff\xd8\xff\xe0fake")


def client(tmp_path, payload=None):
    return TestClient(create_app(FakeService(payload), tmp_path))


def test_status_returns_summary(tmp_path):
    r = client(tmp_path).get("/api/status")
    assert r.status_code == 200
    assert r.json()["gcode_state"] == "IDLE"


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


def test_ws_sends_summary_immediately(tmp_path):
    with client(tmp_path).websocket_connect("/ws") as ws:
        msg = ws.receive_json()
        assert msg["gcode_state"] == "IDLE"


def test_root_hint_when_no_dist(tmp_path):
    r = client(tmp_path).get("/")
    assert r.status_code == 200
    assert "npm run build" in r.text
