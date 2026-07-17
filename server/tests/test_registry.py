import threading
import time

import pytest

from server.registry import DuplicateSerial, PrinterRegistry
from server.store import MemoryStore, PrinterConfig


class FakeService:
    """Stands in for PrinterService/MockPrinter: no sockets, no threads."""

    def __init__(self, cfg: PrinterConfig):
        self.cfg = cfg
        self.started = False
        self.stopped = False

    def start(self):
        self.started = True

    def stop(self):
        self.stopped = True

    def summary(self):
        return {"serial": self.cfg.serial, "name": self.cfg.name,
                "printer": self.cfg.host, "capture": self.cfg.capture,
                "connection": "ok"}


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
    r.add(host="1.2.3.4", serial="S1", access_code="31661007")
    assert "31661007" not in repr(r.summaries())


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
