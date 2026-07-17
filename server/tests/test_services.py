import pytest

from server.printer import STALE_S, MockPrinter, PrinterService
from server.sdcard import SdError


# ---------- PrinterService ----------

def svc(**kw):
    # 192.0.2.1 is TEST-NET; constructing does NOT open a socket.
    kw.setdefault("host", "192.0.2.1")
    kw.setdefault("serial", "0309TESTSERIAL")
    kw.setdefault("access_code", "12345678")
    return PrinterService(**kw)


def test_service_summary_before_connect():
    s = svc().summary()
    assert s["connection"] == "disconnected"
    assert s["printer"] == "192.0.2.1"
    assert s["serial"] == "0309TESTSERIAL"
    assert s["report_age_s"] is None


def test_service_name_defaults_to_host():
    assert svc().summary()["name"] == "192.0.2.1"


def test_service_name_used_when_given():
    assert svc(name="A1-bench").summary()["name"] == "A1-bench"


def test_service_capture_flag_in_summary():
    assert svc(capture=True).summary()["capture"] is True


def test_service_summary_never_leaks_access_code():
    s = svc(access_code="31661007").summary()
    assert "31661007" not in repr(s)
    assert "access_code" not in s


def test_service_last_error_none_before_any_attempt():
    assert svc().summary()["last_error"] is None


def test_unreachable_error_message_when_connect_raises(monkeypatch):
    s = svc()

    def boom(timeout=5):
        raise OSError("boom")

    monkeypatch.setattr(s.link, "connect", boom)
    # The retry backoff must actually SET the stop event, not merely return
    # True: _connect_loop's `while not self._stop.is_set()` ignores wait()'s
    # return value, so a lambda returning True would spin forever.
    monkeypatch.setattr(s._stop, "wait", lambda t: s._stop.set())
    s._connect_loop()
    assert "Unreachable" in s.summary()["last_error"]


def test_no_connack_error_message_when_connect_returns_false(monkeypatch):
    s = svc()
    monkeypatch.setattr(s.link, "connect", lambda timeout=5: False)
    s._connect_loop()
    err = s.summary()["last_error"]
    assert "access code" in err
    assert "Developer Mode" in err


def test_last_error_cleared_on_successful_connect(monkeypatch):
    s = svc()
    s._last_error = "stale error from an earlier attempt"
    monkeypatch.setattr(s.link, "connect", lambda timeout=5: True)
    s._connect_loop()
    assert s._last_error is None


def test_stop_on_never_started_service_does_not_raise():
    # Task 5's registry calls stop() on every service, including ones whose
    # start() was never invoked (e.g. constructed but never started because
    # another printer in the batch failed to construct). Thread.is_alive() on
    # a never-started thread must be safe to call.
    s = svc()
    s.stop()  # must not raise


# ---------- MockPrinter ----------

def test_mock_frame_shape(tmp_path):
    assert MockPrinter(tmp_path)._frame(5).shape == (480, 640, 3)


def test_mock_touch_updates_summary(tmp_path):
    mp = MockPrinter(tmp_path)
    assert mp.summary()["connection"] == "stale"  # no report yet
    mp._touch({"gcode_state": "RUNNING", "layer_num": 2})
    s = mp.summary()
    assert s["layer_num"] == 2
    assert s["gcode_state"] == "RUNNING"
    assert s["connection"] == "ok"
    assert s["printer"] == "MOCK"


def test_mock_offline_mode_is_disconnected(tmp_path):
    mp = MockPrinter(tmp_path, mode="offline")
    mp.start()
    s = mp.summary()
    assert s["connection"] == "disconnected"
    assert "Unreachable" in s["last_error"]


def test_mock_offline_mode_stop_does_not_raise(tmp_path):
    mp = MockPrinter(tmp_path, mode="offline")
    mp.start()
    mp.stop()  # never started _thread -- must still be safe


def test_mock_stale_mode_reports_stale(tmp_path):
    mp = MockPrinter(tmp_path, mode="stale")
    mp.start()
    s = mp.summary()
    assert s["connection"] == "stale"
    assert s["report_age_s"] > STALE_S


def test_mock_stale_mode_stop_does_not_raise(tmp_path):
    mp = MockPrinter(tmp_path, mode="stale")
    mp.start()
    mp.stop()  # never started _thread -- must still be safe


def test_mock_identity_in_summary(tmp_path):
    mp = MockPrinter(tmp_path, serial="MOCK1", host="mock-bench",
                     name="bench", capture=True)
    s = mp.summary()
    assert s["serial"] == "MOCK1"
    assert s["name"] == "bench"
    assert s["capture"] is True


def test_mock_lists_root(tmp_path):
    names = [e["name"] for e in MockPrinter(tmp_path).list_files("/")]
    assert "timelapse" in names
    assert "Benchy.3mf" in names


def test_mock_lists_subdir(tmp_path):
    entries = MockPrinter(tmp_path).list_files("/timelapse")
    assert all(e["is_dir"] is False for e in entries)
    assert len(entries) >= 1


def test_mock_unknown_dir_raises_sderror(tmp_path):
    with pytest.raises(SdError):
        MockPrinter(tmp_path).list_files("/nope")


def test_mock_list_rejects_traversal(tmp_path):
    with pytest.raises(SdError):
        MockPrinter(tmp_path).list_files("/../etc")


def test_mock_list_files_does_not_let_caller_mutate_shared_tree(tmp_path):
    # list_files returns a fresh list each call, but the dicts inside come
    # straight from the module-level MOCK_TREE constant. If a caller mutated
    # an entry dict, that mutation would leak into every future call and
    # every other MockPrinter instance -- so verify entries really are
    # independent copies, not just a fresh outer list.
    entries = MockPrinter(tmp_path).list_files("/")
    entries[0]["name"] = "TAMPERED"
    fresh = MockPrinter(tmp_path).list_files("/")
    assert "TAMPERED" not in [e["name"] for e in fresh]
