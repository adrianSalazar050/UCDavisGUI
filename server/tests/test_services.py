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
