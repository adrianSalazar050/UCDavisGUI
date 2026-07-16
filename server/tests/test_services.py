import threading
import time

from server.printer import MockPrinter, PrinterService


def test_service_summary_before_connect():
    # 192.0.2.1 is TEST-NET; constructing does NOT open a socket.
    svc = PrinterService("192.0.2.1", "0309TESTSERIAL", "12345678")
    s = svc.summary()
    assert s["connection"] == "disconnected"
    assert s["printer"] == "192.0.2.1"
    assert s["report_age_s"] is None


def test_mock_frame_shape(tmp_path):
    mp = MockPrinter(tmp_path)
    img = mp._frame(5)
    assert img.shape == (480, 640, 3)


def test_mock_touch_updates_summary(tmp_path):
    mp = MockPrinter(tmp_path)
    assert mp.summary()["connection"] == "stale"  # no report yet
    mp._touch({"gcode_state": "RUNNING", "layer_num": 2})
    s = mp.summary()
    assert s["layer_num"] == 2
    assert s["gcode_state"] == "RUNNING"
    assert s["connection"] == "ok"
    assert s["printer"] == "MOCK"


class ScriptedLink:
    """Stub BambuLink whose connect() results are scripted."""

    def __init__(self, results):
        self.results = list(results)  # bool, or an Exception to raise
        self.calls = 0
        self.connected = threading.Event()
        self.state = {}

    def connect(self, timeout=None):
        self.calls += 1
        r = self.results.pop(0) if self.results else False
        if isinstance(r, Exception):
            raise r
        if r:
            self.connected.set()
        return r

    def disconnect(self):
        pass


def test_connect_loop_retries_after_error_then_succeeds(monkeypatch):
    monkeypatch.setattr("server.printer.RETRY_S", 0.01)
    svc = PrinterService("192.0.2.1", "S", "12345678")
    svc.link = ScriptedLink([OSError("network down"), True])
    svc.start()
    svc._thread.join(timeout=5)
    assert not svc._thread.is_alive()
    assert svc.link.calls == 2
    # connected but no report yet -> stale, never "disconnected"
    assert svc.summary()["connection"] == "stale"


def test_connect_loop_hands_off_to_paho_after_rejected_connack(monkeypatch):
    monkeypatch.setattr("server.printer.RETRY_S", 0.01)
    svc = PrinterService("192.0.2.1", "S", "12345678")
    svc.link = ScriptedLink([False])
    svc.start()
    svc._thread.join(timeout=5)
    assert not svc._thread.is_alive()
    assert svc.link.calls == 1  # no manual re-drive once paho owns retries


def test_mock_full_cycle_writes_frames_and_finishes(tmp_path, monkeypatch):
    monkeypatch.setattr(MockPrinter, "LAYERS", 3)
    monkeypatch.setattr(MockPrinter, "LAYER_PERIOD_S", 0.01)
    monkeypatch.setattr(MockPrinter, "IDLE_S", 60.0)  # parks after one cycle
    mp = MockPrinter(tmp_path)
    mp.start()
    deadline = time.time() + 10
    while time.time() < deadline and mp.summary()["gcode_state"] != "FINISH":
        time.sleep(0.02)
    mp.stop()
    assert mp.summary()["gcode_state"] == "FINISH"
    frames = list(tmp_path.glob("*_mock_benchy/frames/layer_*.jpg"))
    assert len(frames) == 3
