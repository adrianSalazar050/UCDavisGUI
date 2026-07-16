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
