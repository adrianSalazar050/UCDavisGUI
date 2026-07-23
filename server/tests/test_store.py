import fnmatch
import json
import logging
import os
import pathlib
import tempfile

import pytest

from server.store import (MemoryStore, PrinterConfig, PrinterStore,
                          guess_model_id, model_mismatch)


def cfg(serial="0300CA633005010", host="192.168.137.2", code="test-access-code",
        name="", capture=False):
    return PrinterConfig(serial=serial, host=host, access_code=code,
                         name=name, capture=capture)


def test_guess_model_id_from_a_verified_serial_prefix():
    # 039 -> N2S (A1). VERIFIED: this repo's printer is serial
    # 03919D531805572 and its slicer output carries printer_model_id N2S.
    assert guess_model_id("03919D531805572") == "N2S"


def test_guess_model_id_from_the_a1_mini_prefix():
    assert guess_model_id("0300CA633005010") == "N1"


def test_guess_model_id_unknown_prefix_is_blank_not_a_guess():
    # A wrong guess can only cost the user a refused print, so an unrecognised
    # prefix must yield "" (= unknown = never block), never a plausible default.
    assert guess_model_id("ZZZ123") == ""
    assert guess_model_id("") == ""
    assert guess_model_id(None) == ""


def test_config_model_id_defaults_blank():
    assert cfg().model_id == ""


def test_from_dict_keeps_a_known_model_id():
    c = PrinterConfig.from_dict({"serial": "S", "host": "h",
                                 "access_code": "c", "model_id": "N2S"})
    assert c.model_id == "N2S"


def test_from_dict_rejects_a_non_string_model_id():
    c = PrinterConfig.from_dict({"serial": "S", "host": "h",
                                 "access_code": "c", "model_id": 42})
    assert c.model_id == ""


def test_from_dict_keeps_an_unrecognised_model_id_verbatim():
    # The id table is not exhaustive -- Bambu ships models this code has never
    # heard of. Storing an unknown id is fine and still enables the check (it
    # compares ids, not names); only the friendly NAME is unavailable.
    c = PrinterConfig.from_dict({"serial": "S", "host": "h",
                                 "access_code": "c", "model_id": "C99"})
    assert c.model_id == "C99"


def test_model_mismatch_names_both_sides():
    msg = model_mismatch("N2S", "N1")
    assert "A1 mini" in msg and "A1" in msg


def test_model_mismatch_none_when_they_match():
    assert model_mismatch("N2S", "N2S") is None


def test_model_mismatch_none_when_printer_model_unknown():
    # THE load-bearing rule: unknown never blocks. The printer's model can
    # only ever be configured by hand, so an unset one is the common case and
    # must not refuse a perfectly good file.
    assert model_mismatch("", "N2S") is None
    assert model_mismatch(None, "N2S") is None


def test_model_mismatch_none_when_file_model_unknown():
    # Every raw .gcode, and any 3mf whose slicer didn't write the key.
    assert model_mismatch("N2S", "") is None
    assert model_mismatch("N2S", None) is None


def test_model_mismatch_uses_raw_ids_when_not_in_the_name_table():
    msg = model_mismatch("C99", "D77")
    assert "C99" in msg and "D77" in msg


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
    assert got[0].access_code == "test-access-code"
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
    # subclasses ValueError, not OSError.
    p = tmp_path / "printers.json"
    p.write_bytes(b"\xff\xfe[{\"serial\": \"x\"}]")
    assert PrinterStore(p).load() == []


def test_corrupt_json_logs_a_warning(tmp_path, caplog):
    # A corrupt file must not just silently return [] -- the difference
    # between a silent [] and a warned [] is whether the user can ever find
    # out why their printers vanished.
    p = tmp_path / "printers.json"
    p.write_text("{not json at all", encoding="utf-8")
    with caplog.at_level(logging.WARNING, logger="server.store"):
        assert PrinterStore(p).load() == []
    assert any("unreadable" in r.message for r in caplog.records)


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


def test_falsy_wrong_typed_fields_are_skipped_not_coerced(tmp_path):
    # (x or "").strip() tolerates None/0/False by coercing them to "" --
    # which would let all of these collapse onto the same registry key
    # (serial=""), silently overwriting each other (registry keys on
    # serial). A falsy wrong type must be rejected exactly like a truthy
    # one, not silently accepted.
    p = tmp_path / "printers.json"
    p.write_text(json.dumps([
        {"serial": None, "host": "1.2.3.4", "access_code": "c"},
        {"serial": 0, "host": "1.2.3.4", "access_code": "c"},
        {"serial": False, "host": "1.2.3.4", "access_code": "c"},
        {"serial": 12345, "host": "1.2.3.4", "access_code": "c"},
        {"serial": "real", "host": "1.2.3.4", "access_code": "c"},
    ]), encoding="utf-8")
    got = PrinterStore(p).load()
    assert [c.serial for c in got] == ["real"]


def test_capture_wrong_type_defaults_false_not_bool_coerced(tmp_path):
    # bool("false") is True in Python. capture gates single-occupancy
    # webcam access (the registry clears it on other printers), so a bad
    # value must default to False, not to whatever truthiness the wrong
    # type happens to have.
    p = tmp_path / "printers.json"
    p.write_text(json.dumps([
        {"serial": "S1", "host": "1.2.3.4", "access_code": "c",
         "capture": "false"},
    ]), encoding="utf-8")
    got = PrinterStore(p).load()
    assert len(got) == 1
    assert got[0].capture is False


def test_name_wrong_type_falls_back_to_host(tmp_path):
    # name is cosmetic -- a wrong type there shouldn't cost the user the
    # whole printer, unlike serial/host/access_code.
    p = tmp_path / "printers.json"
    p.write_text(json.dumps([
        {"serial": "S1", "host": "1.2.3.4", "access_code": "c", "name": 5},
    ]), encoding="utf-8")
    got = PrinterStore(p).load()
    assert len(got) == 1
    assert got[0].name == "1.2.3.4"


def test_save_leaves_no_temp_files(tmp_path):
    store = PrinterStore(tmp_path / "printers.json")
    store.save([cfg()])
    assert sorted(f.name for f in tmp_path.iterdir()) == ["printers.json"]


def test_save_temp_file_name_matches_gitignore_pattern(tmp_path, monkeypatch):
    # .gitignore covers the target with "printers.json*". The temp file
    # save() creates mid-write must fall under that same pattern by
    # construction -- an interrupted save (Ctrl-C, power loss, task kill)
    # must never leak the access code past .gitignore.
    captured = {}
    real_mkstemp = tempfile.mkstemp

    def spy_mkstemp(*a, **kw):
        fd, path = real_mkstemp(*a, **kw)
        captured["name"] = pathlib.Path(path).name
        return fd, path

    monkeypatch.setattr(tempfile, "mkstemp", spy_mkstemp)
    PrinterStore(tmp_path / "printers.json").save([cfg()])
    assert captured["name"].startswith("printers.json")
    assert fnmatch.fnmatch(captured["name"], "printers.json*")


def test_save_failure_leaves_no_leftover_temp_file(tmp_path, monkeypatch):
    store = PrinterStore(tmp_path / "printers.json")

    def boom(*a, **kw):
        raise OSError("disk full")

    monkeypatch.setattr(os, "replace", boom)
    with pytest.raises(OSError):
        store.save([cfg()])
    assert list(tmp_path.iterdir()) == []


def test_save_overwrites_existing_file(tmp_path):
    p = tmp_path / "printers.json"
    store = PrinterStore(p)
    store.save([cfg(serial="OLD")])
    store.save([cfg(serial="NEW")])
    got = store.load()
    assert [c.serial for c in got] == ["NEW"]
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


def test_detection_fields_default(tmp_path):
    c = PrinterConfig(serial="S1", host="1.2.3.4", access_code="c")
    assert c.camera_index == 0
    assert c.conf == 0.25
    assert c.armed_classes == ["spaghetti"]
    assert c.detect_enabled is False


def test_detection_fields_round_trip(tmp_path):
    p = tmp_path / "printers.json"
    store = PrinterStore(p)
    store.save([PrinterConfig(serial="S1", host="1.2.3.4", access_code="c",
                              camera_index=2, conf=0.4,
                              armed_classes=["spaghetti", "cracks"],
                              detect_enabled=True)])
    got = store.load()[0]
    assert got.camera_index == 2
    assert got.conf == 0.4
    assert got.armed_classes == ["spaghetti", "cracks"]
    assert got.detect_enabled is True


def test_detection_defaults_are_independent_lists():
    # A dataclass mutable default MUST use default_factory, or every config
    # shares one list and appending to one printer's classes mutates them all.
    a = PrinterConfig(serial="A", host="h", access_code="c")
    b = PrinterConfig(serial="B", host="h", access_code="c")
    a.armed_classes.append("cracks")
    assert b.armed_classes == ["spaghetti"]


def test_detect_enabled_wrong_type_defaults_false(tmp_path):
    p = tmp_path / "printers.json"
    p.write_text(json.dumps([
        {"serial": "S1", "host": "1.2.3.4", "access_code": "c",
         "detect_enabled": "true"},
    ]), encoding="utf-8")
    got = PrinterStore(p).load()
    assert got[0].detect_enabled is False


def test_armed_classes_wrong_type_defaults_to_spaghetti(tmp_path):
    p = tmp_path / "printers.json"
    p.write_text(json.dumps([
        {"serial": "S1", "host": "1.2.3.4", "access_code": "c",
         "armed_classes": "spaghetti"},   # a string, not a list
    ]), encoding="utf-8")
    got = PrinterStore(p).load()
    assert got[0].armed_classes == ["spaghetti"]


def test_unknown_armed_class_is_dropped(tmp_path):
    p = tmp_path / "printers.json"
    p.write_text(json.dumps([
        {"serial": "S1", "host": "1.2.3.4", "access_code": "c",
         "armed_classes": ["spaghetti", "banana"]},
    ]), encoding="utf-8")
    got = PrinterStore(p).load()
    assert got[0].armed_classes == ["spaghetti"]


def test_camera_source_defaults_to_a1():
    assert PrinterConfig(serial="S", host="h", access_code="c").camera_source == "a1"


def test_camera_source_round_trip(tmp_path):
    p = tmp_path / "printers.json"
    PrinterStore(p).save([PrinterConfig(serial="S", host="h", access_code="c",
                                        camera_source="webcam")])
    assert PrinterStore(p).load()[0].camera_source == "webcam"


def test_camera_source_invalid_defaults_to_a1(tmp_path):
    p = tmp_path / "printers.json"
    p.write_text(json.dumps([
        {"serial": "S", "host": "h", "access_code": "c", "camera_source": "usb"},
    ]), encoding="utf-8")
    assert PrinterStore(p).load()[0].camera_source == "a1"


def test_nozzle_defaults_to_04():
    c = PrinterConfig(serial="s", host="h", access_code="a")
    assert c.nozzle == "0.4"


def test_nozzle_accepts_the_four_known_sizes():
    for n in ("0.2", "0.4", "0.6", "0.8"):
        c = PrinterConfig.from_dict(
            {"serial": "s", "host": "h", "access_code": "a", "nozzle": n})
        assert c.nozzle == n


def test_nozzle_degrades_to_04_on_junk():
    # Same rule as normalize_roi: a bad value degrades to the safe default
    # rather than raising. A wrong nozzle slices for the wrong hardware, so
    # the default has to be the common case, not the last thing typed.
    for bad in ("0.5", "", None, 0.4, ["0.4"], "abc"):
        c = PrinterConfig.from_dict(
            {"serial": "s", "host": "h", "access_code": "a", "nozzle": bad})
        assert c.nozzle == "0.4"


def test_nozzle_survives_a_save_load_round_trip(tmp_path):
    store = PrinterStore(tmp_path / "printers.json")
    store.save([PrinterConfig(serial="s", host="h", access_code="a",
                              nozzle="0.6")])
    assert store.load()[0].nozzle == "0.6"


# ---------------- bed type ----------------
# MEASURED 2026-07-22: with curr_bed_type never set, Bambu Studio defaults to
# Cool Plate, whose PLA temp is 35 C -- the gcode we produced carried
# `M190 S35`. This lab's A1 has a Textured PEI Plate, which needs 65 C for
# PLA. On real hardware: nozzle heated correctly to 205 C, the print reached
# layer 2/100, then stalled at 5% with an HMS warning active -- consistent
# with a failed first layer from a cold bed. Slicing the same cube twice
# confirmed the fix with no hardware involved: curr_bed_type='Cool Plate' ->
# M190 S35, curr_bed_type='Textured PEI Plate' -> M190 S65.

def test_bed_type_defaults_to_textured_pei_plate():
    # The A1 ships with a Textured PEI Plate, and that's what this lab's
    # printer actually has (confirmed 2026-07-22) -- so an unconfigured
    # printer must default to the plate that's really installed, not to
    # Bambu Studio's own Cool Plate default that caused today's failure.
    c = PrinterConfig(serial="s", host="h", access_code="a")
    assert c.bed_type == "Textured PEI Plate"


def test_bed_type_accepts_the_five_known_plates():
    for b in ("Cool Plate", "Textured PEI Plate", "High Temp Plate",
             "Engineering Plate", "Cool Plate (SuperTack)"):
        c = PrinterConfig.from_dict(
            {"serial": "s", "host": "h", "access_code": "a", "bed_type": b})
        assert c.bed_type == b


def test_bed_type_degrades_to_textured_pei_plate_on_junk():
    # Same rule as nozzle: a bad value degrades to the safe default rather
    # than raising. A wrong plate slices for the wrong temperature (this is
    # the exact defect being fixed here), so the default has to be the plate
    # actually installed, not the last thing typed.
    for bad in ("cool plate", "", None, 35, ["Cool Plate"], "Cool Plate!"):
        c = PrinterConfig.from_dict(
            {"serial": "s", "host": "h", "access_code": "a", "bed_type": bad})
        assert c.bed_type == "Textured PEI Plate"


def test_bed_type_survives_a_save_load_round_trip(tmp_path):
    store = PrinterStore(tmp_path / "printers.json")
    store.save([PrinterConfig(serial="s", host="h", access_code="a",
                              bed_type="Cool Plate")])
    assert store.load()[0].bed_type == "Cool Plate"
