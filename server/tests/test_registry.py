import threading
import time

import pytest

from server.registry import DuplicateSerial, PrinterRegistry
from server.sdcard import SdError
from server.store import MemoryStore, PrinterConfig


class FakeService:
    """Stands in for PrinterService/MockPrinter: no sockets, no threads."""

    def __init__(self, cfg: PrinterConfig):
        self.cfg = cfg
        self.started = False
        self.stopped = False
        self.fetch_result = b"fake-3mf-bytes"
        self.fetch_error = None
        self.fetch_calls: list[str] = []

    def start(self):
        self.started = True

    def stop(self):
        self.stopped = True

    def summary(self):
        return {"serial": self.cfg.serial, "name": self.cfg.name,
                "printer": self.cfg.host, "capture": self.cfg.capture,
                "connection": "ok"}

    def fetch_file(self, path):
        self.fetch_calls.append(path)
        if self.fetch_error is not None:
            raise self.fetch_error
        return self.fetch_result


def reg(store=None):
    return PrinterRegistry(store or MemoryStore(), FakeService)


def test_starts_empty():
    assert reg().summaries() == []


def test_add_starts_and_returns_summary():
    r = reg()
    s = r.add(host="1.2.3.4", serial="S1", access_code="code")
    assert s["serial"] == "S1"
    assert r.get("S1").started is True


def test_add_persists_to_store():
    store = MemoryStore()
    reg(store).add(host="1.2.3.4", serial="S1", access_code="code")
    saved = store.load()
    assert [c.serial for c in saved] == ["S1"]
    assert saved[0].access_code == "code"


def test_duplicate_serial_rejected():
    r = reg()
    r.add(host="1.2.3.4", serial="S1", access_code="code")
    with pytest.raises(DuplicateSerial):
        r.add(host="5.6.7.8", serial="S1", access_code="other")


def test_missing_fields_rejected():
    r = reg()
    for kw in ({"host": "", "serial": "S", "access_code": "c"},
               {"host": "h", "serial": "", "access_code": "c"},
               {"host": "h", "serial": "S", "access_code": ""}):
        with pytest.raises(ValueError):
            r.add(**kw)


def test_remove_stops_service_and_persists():
    store = MemoryStore()
    r = reg(store)
    r.add(host="1.2.3.4", serial="S1", access_code="code")
    svc = r.get("S1")
    assert r.remove("S1") is True
    assert svc.stopped is True
    assert store.load() == []
    assert r.get("S1") is None


def test_remove_unknown_is_false():
    assert reg().remove("nope") is False


def test_summaries_keep_registration_order():
    r = reg()
    for i in (1, 2, 3):
        r.add(host=f"10.0.0.{i}", serial=f"S{i}", access_code="c")
    assert [s["serial"] for s in r.summaries()] == ["S1", "S2", "S3"]


def test_capture_is_single_occupancy():
    r = reg()
    r.add(host="1.1.1.1", serial="S1", access_code="c", capture=True)
    r.add(host="2.2.2.2", serial="S2", access_code="c", capture=True)
    caps = {s["serial"]: s["capture"] for s in r.summaries()}
    assert caps == {"S1": False, "S2": True}


def test_capture_cleared_in_store_too():
    store = MemoryStore()
    r = reg(store)
    r.add(host="1.1.1.1", serial="S1", access_code="c", capture=True)
    r.add(host="2.2.2.2", serial="S2", access_code="c", capture=True)
    assert {c.serial: c.capture for c in store.load()} == {"S1": False,
                                                           "S2": True}


def test_load_restores_and_starts_from_store():
    store = MemoryStore()
    store.save([PrinterConfig(serial="S1", host="1.2.3.4", access_code="c",
                              name="bench", capture=True)])
    r = reg(store)
    r.load()
    assert [s["serial"] for s in r.summaries()] == ["S1"]
    assert r.get("S1").started is True


def test_summaries_never_contain_access_code():
    r = reg()
    r.add(host="1.2.3.4", serial="S1", access_code="test-access-code")
    assert "test-access-code" not in repr(r.summaries())


def test_stop_all_stops_every_service():
    r = reg()
    r.add(host="1.1.1.1", serial="S1", access_code="c")
    r.add(host="2.2.2.2", serial="S2", access_code="c")
    services = [r.get("S1"), r.get("S2")]
    r.stop_all()
    assert all(s.stopped for s in services)


# ---------------------------------------------------------------------------
# Additional tests found while reviewing the plan's reference code.
# ---------------------------------------------------------------------------


class CaptureOnServiceFake:
    """Like FakeService, but summary() reads `capture` off the *service*
    instance, not off cfg. This is what real PrinterService/MockPrinter
    actually do (each owns its own `capture` attribute independent of any
    config object) -- FakeService's summary() reading straight off `self.cfg`
    would pass even a _clear_capture() that forgot to touch the live service,
    since cfg is the same object shared with the registry's _configs dict.
    This fake would catch that bug.
    """

    def __init__(self, cfg: PrinterConfig):
        self.cfg = cfg
        self.capture = cfg.capture
        self.started = False
        self.stopped = False

    def start(self):
        self.started = True

    def stop(self):
        self.stopped = True

    def summary(self):
        return {"serial": self.cfg.serial, "capture": self.capture}


def test_capture_cleared_on_live_service_not_just_config():
    r = PrinterRegistry(MemoryStore(), CaptureOnServiceFake)
    r.add(host="1.1.1.1", serial="S1", access_code="c", capture=True)
    r.add(host="2.2.2.2", serial="S2", access_code="c", capture=True)
    caps = {s["serial"]: s["capture"] for s in r.summaries()}
    assert caps == {"S1": False, "S2": True}


def test_load_enforces_single_capture_even_if_store_has_two():
    # A hand-edited printers.json (store.py explicitly tolerates malformed
    # hand-edited files) could have two capture=True entries. store.py's
    # load() has no opinion on that -- registry owns the single-capture
    # invariant -- so load() must enforce it too, not just add().
    store = MemoryStore()
    store.save([
        PrinterConfig(serial="S1", host="1.1.1.1", access_code="c",
                     capture=True),
        PrinterConfig(serial="S2", host="2.2.2.2", access_code="c",
                     capture=True),
    ])
    r = reg(store)
    r.load()
    caps = {s["serial"]: s["capture"] for s in r.summaries()}
    assert caps == {"S1": False, "S2": True}


def test_whitespace_only_host_rejected():
    # PrinterConfig.__post_init__ strips whitespace before add()'s
    # emptiness check runs, so a host of all-whitespace must be rejected
    # exactly like an empty one.
    r = reg()
    with pytest.raises(ValueError):
        r.add(host="   ", serial="S1", access_code="c")


def test_non_string_required_field_rejected_cleanly():
    # A truthy non-string (host=123) sails through PrinterConfig's
    # `(x or "").strip()` unchanged and then raises AttributeError inside
    # .strip() unless add() rejects it up front. It must surface as a
    # plain ValueError, not an AttributeError leaking out of dataclass
    # internals.
    r = reg()
    with pytest.raises(ValueError):
        r.add(host=123, serial="S1", access_code="c")


def test_summaries_do_not_race_concurrent_add():
    # Regression test for the dict-iteration race: summaries() reads
    # self._services.values() while add() mutates the same dict from
    # another thread (real callers are FastAPI routes on a threadpool, so
    # this is a genuine race). Without a lock around the snapshot in
    # summaries(), CPython can raise "RuntimeError: dictionary changed size
    # during iteration" here.
    r = reg()
    stop = threading.Event()
    errors = []

    def hammer_add():
        i = 0
        while not stop.is_set():
            try:
                r.add(host=f"10.0.0.{i}", serial=f"S{i}", access_code="c")
            except DuplicateSerial:
                pass
            i += 1

    def hammer_summaries():
        try:
            while not stop.is_set():
                r.summaries()
        except Exception as e:  # pragma: no cover - failure path
            errors.append(e)

    threads = [threading.Thread(target=hammer_add),
              threading.Thread(target=hammer_summaries)]
    for t in threads:
        t.start()
    time.sleep(0.2)
    stop.set()
    for t in threads:
        t.join()
    assert errors == []


class SlowStore(MemoryStore):
    """MemoryStore whose save() takes a configurable amount of time, so a
    test can force save() calls to complete out of the order their snapshots
    were taken in."""

    def __init__(self, delays):
        super().__init__()
        self._delays = iter(delays)

    def save(self, configs):
        time.sleep(next(self._delays, 0))
        super().save(configs)


def test_persist_survives_concurrent_add_without_losing_a_printer():
    # The first add()'s store.save() is deliberately slow so it would
    # finish *after* the second add()'s save() if persistence calls were
    # not serialized -- exercising exactly the lost-update race described
    # in _persist()'s docstring: a slow, stale write landing last would
    # silently drop the second printer from the persisted file.
    store = SlowStore([0.2, 0.0])
    r = reg(store)
    barrier = threading.Barrier(2)

    def add_one():
        barrier.wait()
        r.add(host="1.1.1.1", serial="S1", access_code="c")

    def add_two():
        barrier.wait()
        r.add(host="2.2.2.2", serial="S2", access_code="c")

    t1 = threading.Thread(target=add_one)
    t2 = threading.Thread(target=add_two)
    t1.start()
    t2.start()
    t1.join()
    t2.join()

    assert {c.serial for c in store.load()} == {"S1", "S2"}


# ---------------------------------------------------------------------------
# Detection accessors (Task 9)
# ---------------------------------------------------------------------------


def test_capture_serial_returns_the_capture_printer():
    r = reg()
    r.add(host="1.1.1.1", serial="A", access_code="c")
    r.add(host="2.2.2.2", serial="B", access_code="c", capture=True)
    assert r.capture_serial() == "B"


def test_capture_serial_none_when_no_capture():
    r = reg()
    r.add(host="1.1.1.1", serial="A", access_code="c")
    assert r.capture_serial() is None


def test_detection_target_requires_detect_enabled():
    r = reg()
    r.add(host="1.1.1.1", serial="B", access_code="c", capture=True)
    assert r.detection_target() is None
    r.update_detection("B", detect_enabled=True)
    assert r.detection_target() == {"serial": "B", "camera_source": "a1",
                                    "camera_index": 0, "conf": 0.25,
                                    "host": "1.1.1.1", "access_code": "c"}


def test_update_detection_persists_and_clamps():
    r = reg()
    r.add(host="1.1.1.1", serial="B", access_code="c", capture=True)
    assert r.update_detection("B", camera_index=2, conf=0.6,
                              armed_classes=["spaghetti", "cracks"]) is True
    cfg = r.detection_config("B")
    assert cfg["camera_index"] == 2 and cfg["conf"] == 0.6
    assert cfg["armed_classes"] == ["spaghetti", "cracks"]


def test_update_detection_unknown_serial_false():
    r = reg()
    r.add(host="1.1.1.1", serial="A", access_code="c")
    assert r.update_detection("ZZ", conf=0.9) is False


# ---------------------------------------------------------------------------
# camera_source / host / access_code on the detection accessors (Task 4)
# ---------------------------------------------------------------------------


def test_detection_target_includes_source_host_and_code():
    r = reg()
    r.add(host="1.2.3.4", serial="B", access_code="SEKRET", capture=True)
    r.update_detection("B", detect_enabled=True)
    t = r.detection_target()
    assert t["camera_source"] == "a1"
    assert t["host"] == "1.2.3.4"
    assert t["access_code"] == "SEKRET"


def test_update_detection_sets_camera_source():
    r = reg()
    r.add(host="1.1.1.1", serial="B", access_code="c", capture=True)
    assert r.update_detection("B", camera_source="webcam") is True
    assert r.detection_config("B")["camera_source"] == "webcam"


def test_update_detection_ignores_bad_camera_source():
    r = reg()
    r.add(host="1.1.1.1", serial="B", access_code="c", capture=True)
    r.update_detection("B", camera_source="usb")
    assert r.detection_config("B")["camera_source"] == "a1"


# ---------------------------------------------------------------------------
# update() -- editing a registered printer's connection info
# ---------------------------------------------------------------------------


def test_update_name_only_keeps_same_service_and_updates_name():
    r = reg()
    r.add(host="1.2.3.4", serial="S1", access_code="code", name="old-name")
    svc_before = r.get("S1")
    result = r.update("S1", name="new-name")
    assert r.get("S1") is svc_before
    assert result["name"] == "new-name"


def test_update_host_swaps_service_starts_new_and_stops_old():
    r = reg()
    r.add(host="1.2.3.4", serial="S1", access_code="code")
    old_svc = r.get("S1")
    result = r.update("S1", host="5.6.7.8")
    new_svc = r.get("S1")
    assert new_svc is not old_svc
    assert old_svc.stopped is True
    assert new_svc.started is True
    assert result["printer"] == "5.6.7.8"


def test_update_access_code_change_also_swaps_service():
    r = reg()
    r.add(host="1.2.3.4", serial="S1", access_code="original")
    old_svc = r.get("S1")
    r.update("S1", access_code="new-code")
    new_svc = r.get("S1")
    assert new_svc is not old_svc
    assert old_svc.stopped is True
    assert new_svc.started is True


def test_update_no_reconnect_when_host_resubmitted_unchanged():
    # An edit form round-trips the current host even when only the name
    # changed. Resubmitting the SAME host must not tear down the connection.
    r = reg()
    r.add(host="1.2.3.4", serial="S1", access_code="code")
    old_svc = r.get("S1")
    r.update("S1", host="1.2.3.4", name="renamed")
    assert r.get("S1") is old_svc
    assert old_svc.stopped is False


def test_update_blank_access_code_keeps_old_code():
    # A blank access_code means "keep the current one" -- it's a secret the
    # client never receives back, so it can't round-trip it in an edit form.
    # Inspect via detection_target(), the one existing accessor that exposes
    # access_code, rather than asserting anything about summary().
    r = reg()
    r.add(host="1.2.3.4", serial="S1", access_code="original-code",
          capture=True)
    r.update_detection("S1", detect_enabled=True)
    r.update("S1", access_code="")
    target = r.detection_target()
    assert target["access_code"] == "original-code"


def test_update_capture_true_clears_other_printers():
    r = reg()
    r.add(host="1.1.1.1", serial="S1", access_code="c", capture=True)
    r.add(host="2.2.2.2", serial="S2", access_code="c")
    r.update("S2", capture=True)
    caps = {s["serial"]: s["capture"] for s in r.summaries()}
    assert caps == {"S1": False, "S2": True}


def test_update_unknown_serial_returns_none():
    r = reg()
    r.add(host="1.1.1.1", serial="S1", access_code="c")
    assert r.update("nope", name="x") is None


def test_update_empty_host_raises():
    r = reg()
    r.add(host="1.2.3.4", serial="S1", access_code="c")
    with pytest.raises(ValueError):
        r.update("S1", host="")


def test_update_whitespace_only_host_raises():
    r = reg()
    r.add(host="1.2.3.4", serial="S1", access_code="c")
    with pytest.raises(ValueError):
        r.update("S1", host="   ")


def test_update_persists_to_store():
    store = MemoryStore()
    r = reg(store)
    r.add(host="1.2.3.4", serial="S1", access_code="code")
    r.update("S1", name="renamed")
    saved = store.load()
    assert saved[0].name == "renamed"


# ---------------------------------------------------------------------------
# fetch_sd_file (Task 4) -- hides the access code behind the service exactly
# like the /files route's svc.list_files() call does.
# ---------------------------------------------------------------------------


def test_fetch_sd_file_delegates_to_service():
    r = reg()
    r.add(host="1.2.3.4", serial="S1", access_code="code")
    svc = r.get("S1")
    svc.fetch_result = b"PK\x03\x04zipbytes"
    assert r.fetch_sd_file("S1", "/Benchy.gcode.3mf") == b"PK\x03\x04zipbytes"
    assert svc.fetch_calls == ["/Benchy.gcode.3mf"]


def test_fetch_sd_file_unknown_serial_raises_sderror():
    r = reg()
    with pytest.raises(SdError):
        r.fetch_sd_file("nope", "/x.3mf")


def test_fetch_sd_file_propagates_service_sderror():
    r = reg()
    r.add(host="1.2.3.4", serial="S1", access_code="code")
    r.get("S1").fetch_error = SdError("Could not fetch /x.3mf on 1.2.3.4")
    with pytest.raises(SdError):
        r.fetch_sd_file("S1", "/x.3mf")


def test_fetch_sd_file_never_needs_access_code_argument():
    # Structural guard against regression: fetch_sd_file's own signature has
    # no access_code parameter at all, so there is no channel for the secret
    # to flow through it -- this only stays true as long as nobody adds one.
    import inspect
    params = inspect.signature(PrinterRegistry.fetch_sd_file).parameters
    assert "access_code" not in params
