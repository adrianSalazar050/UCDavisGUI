import json

from server.store import MemoryStore, PrinterConfig, PrinterStore


def cfg(serial="0300CA633005010", host="192.168.137.2", code="31661007",
        name="", capture=False):
    return PrinterConfig(serial=serial, host=host, access_code=code,
                         name=name, capture=capture)


def test_name_defaults_to_host():
    assert cfg().name == "192.168.137.2"


def test_name_kept_when_given():
    assert cfg(name="A1-bench").name == "A1-bench"


def test_fields_are_stripped():
    c = PrinterConfig(serial=" S1 ", host=" 10.0.0.1 ", access_code=" abc ")
    assert (c.serial, c.host, c.access_code) == ("S1", "10.0.0.1", "abc")


def test_missing_file_loads_empty(tmp_path):
    assert PrinterStore(tmp_path / "nope.json").load() == []


def test_round_trip(tmp_path):
    p = tmp_path / "printers.json"
    store = PrinterStore(p)
    store.save([cfg(name="A1-bench", capture=True)])
    got = store.load()
    assert len(got) == 1
    assert got[0].serial == "0300CA633005010"
    assert got[0].access_code == "31661007"
    assert got[0].name == "A1-bench"
    assert got[0].capture is True


def test_corrupt_json_loads_empty_and_does_not_raise(tmp_path):
    p = tmp_path / "printers.json"
    p.write_text("{not json at all", encoding="utf-8")
    assert PrinterStore(p).load() == []


def test_non_list_json_loads_empty(tmp_path):
    p = tmp_path / "printers.json"
    p.write_text('{"serial": "x"}', encoding="utf-8")
    assert PrinterStore(p).load() == []


def test_invalid_utf8_bytes_load_empty(tmp_path):
    # PowerShell's Out-File/Set-Content default to UTF-16 LE with a BOM, so a
    # hand-edit from a PS prompt lands 0xFF 0xFE here. UnicodeDecodeError
    # subclasses ValueError, not OSError -- it must be caught explicitly or it
    # escapes load() and kills the boot path.
    p = tmp_path / "printers.json"
    p.write_bytes(b"\xff\xfe[{\"serial\": \"x\"}]")
    assert PrinterStore(p).load() == []


def test_utf8_bom_file_still_loads(tmp_path):
    # Notepad and friends prepend a UTF-8 BOM on save. The JSON is fine; only
    # the three leading bytes aren't. Reading with utf-8 would make every
    # configured printer silently vanish.
    p = tmp_path / "printers.json"
    p.write_bytes(b"\xef\xbb\xbf" + json.dumps(
        [{"serial": "S1", "host": "1.2.3.4", "access_code": "c"}]
    ).encode("utf-8"))
    got = PrinterStore(p).load()
    assert [c.serial for c in got] == ["S1"]


def test_entry_missing_required_field_is_skipped(tmp_path):
    p = tmp_path / "printers.json"
    p.write_text(json.dumps([
        {"serial": "good", "host": "1.2.3.4", "access_code": "code"},
        {"serial": "bad-no-host", "access_code": "code"},
    ]), encoding="utf-8")
    got = PrinterStore(p).load()
    assert [c.serial for c in got] == ["good"]


def test_entry_with_wrong_typed_field_is_skipped(tmp_path):
    p = tmp_path / "printers.json"
    p.write_text(json.dumps([
        {"serial": 12345, "host": "1.2.3.4", "access_code": "abc"},
        {"serial": "good", "host": "1.2.3.4", "access_code": "code"},
    ]), encoding="utf-8")
    got = PrinterStore(p).load()
    assert [c.serial for c in got] == ["good"]


def test_save_leaves_no_temp_files(tmp_path):
    store = PrinterStore(tmp_path / "printers.json")
    store.save([cfg()])
    assert sorted(f.name for f in tmp_path.iterdir()) == ["printers.json"]


def test_save_creates_parent_dir(tmp_path):
    store = PrinterStore(tmp_path / "sub" / "printers.json")
    store.save([cfg()])
    assert len(store.load()) == 1


def test_memory_store_round_trips_without_disk():
    store = MemoryStore()
    assert store.load() == []
    store.save([cfg()])
    assert len(store.load()) == 1


def test_memory_store_load_returns_a_copy():
    store = MemoryStore()
    store.save([cfg()])
    store.load().clear()
    assert len(store.load()) == 1
