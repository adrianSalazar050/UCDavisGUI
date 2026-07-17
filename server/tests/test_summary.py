from server.printer import STALE_S, build_summary


def test_empty_state_disconnected():
    s = build_summary({}, None, False, "MOCK")
    assert s["connection"] == "disconnected"
    assert s["report_age_s"] is None
    assert s["hms"] == []
    assert s["layer_num"] is None
    assert s["gcode_state"] is None
    assert s["printer"] == "MOCK"


def test_running_state_ok():
    st = {
        "gcode_state": "RUNNING", "layer_num": 3, "total_layer_num": 100,
        "mc_percent": 3, "nozzle_temper": 219.8,
        "hms": [{"attr": 0x03000100, "code": 0x00010007}],
    }
    s = build_summary(st, 1.23, True, "192.168.1.42")
    assert s["connection"] == "ok"
    assert s["report_age_s"] == 1.2
    assert s["hms"] == ["0300_0100_0001_0007"]
    assert s["layer_num"] == 3
    assert s["printer"] == "192.168.1.42"


def test_stale_when_report_old_or_absent():
    assert build_summary({}, STALE_S + 5, True, "x")["connection"] == "stale"
    assert build_summary({}, None, True, "x")["connection"] == "stale"


def test_hms_none_is_empty_list():
    assert build_summary({"hms": None}, 0.1, True, "x")["hms"] == []


def test_malformed_hms_entries_are_skipped():
    st = {"hms": ["not-a-dict", {"attr": "bogus", "code": 0}, None,
                  {"attr": 0x03000100, "code": 0x00010007}]}
    s = build_summary(st, 0.1, True, "x")
    assert s["hms"] == ["0300_0100_0001_0007"]


def test_summary_never_leaks_unknown_state_fields():
    s = build_summary({"wifi_password": "secret", "layer_num": 1}, 0.1, True, "x")
    assert "wifi_password" not in s
    assert s["layer_num"] == 1


def test_identity_fields_default_empty():
    s = build_summary({}, None, False, "192.0.2.1")
    assert s["serial"] == ""
    assert s["name"] == ""
    assert s["capture"] is False
    assert s["last_error"] is None


def test_identity_fields_passed_through():
    s = build_summary({}, 1.0, True, "192.168.137.2",
                      serial="0300CA633005010", name="A1-bench",
                      capture=True, last_error="Unreachable")
    assert s["serial"] == "0300CA633005010"
    assert s["name"] == "A1-bench"
    assert s["capture"] is True
    assert s["last_error"] == "Unreachable"


def test_summary_never_contains_access_code():
    s = build_summary({"gcode_state": "RUNNING"}, 1.0, True, "192.168.137.2",
                      serial="S1", name="n", capture=False)
    assert "access_code" not in s
    assert "31661007" not in repr(s)


def test_summary_key_set_matches_design_spec_payload():
    # Structural guarantee, not a trivial absence check: build_summary has no
    # access_code parameter at all, so there is no code path by which one
    # could reach the payload. Pinning the exact key set means a future edit
    # that adds a field (e.g. an accidental `access_code`) must consciously
    # update this test instead of silently expanding the payload.
    s = build_summary({"gcode_state": "RUNNING"}, 1.0, True, "192.168.137.2",
                      serial="S1", name="n", capture=True,
                      last_error=None)
    assert set(s.keys()) == {
        "serial", "name", "printer", "capture", "connection",
        "report_age_s", "last_error",
        "gcode_state", "layer_num", "total_layer_num", "mc_percent",
        "mc_remaining_time", "nozzle_temper", "nozzle_target_temper",
        "bed_temper", "bed_target_temper", "spd_lvl", "spd_mag",
        "print_error", "fail_reason", "subtask_name", "gcode_file", "hms",
    }
