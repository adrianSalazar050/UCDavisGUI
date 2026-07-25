from server.slicepresets import (TIERS, available_filaments,
                                 available_presets, detect_loaded_filament,
                                 filament_profile_name, machine_profile_name,
                                 resolve_preset)

# A miniature stand-in for the real 1,932-preset index.
INDEX = {
    "Bambu Lab A1 0.4 nozzle": {"name": "Bambu Lab A1 0.4 nozzle"},
    "Bambu Lab A1 0.6 nozzle": {"name": "Bambu Lab A1 0.6 nozzle"},
    "Bambu Lab A1 mini 0.4 nozzle": {"name": "Bambu Lab A1 mini 0.4 nozzle"},
    "0.20mm Standard @BBL A1": {"name": "0.20mm Standard @BBL A1"},
    "0.16mm Optimal @BBL A1": {"name": "0.16mm Optimal @BBL A1"},
    "0.28mm Extra Draft @BBL A1": {"name": "0.28mm Extra Draft @BBL A1"},
    "0.30mm Standard @BBL A1 0.6 nozzle":
        {"name": "0.30mm Standard @BBL A1 0.6 nozzle"},
    "0.20mm Standard @BBL A1M": {"name": "0.20mm Standard @BBL A1M"},
}


def test_machine_name_uses_the_machine_token_and_always_has_the_suffix():
    assert machine_profile_name("N2S", "0.4") == "Bambu Lab A1 0.4 nozzle"
    assert machine_profile_name("N1", "0.4") == "Bambu Lab A1 mini 0.4 nozzle"


def test_machine_name_is_empty_for_an_unknown_model():
    assert machine_profile_name("N9X", "0.4") == ""


def test_resolve_preset_is_not_fooled_by_a_prefixed_decoy_name():
    # "0.20mm Silent Standard @BBL A1" satisfies a naive start-anchor +
    # end-anchor check for tier "standard", and sorts BEFORE the real
    # "0.20mm Standard @BBL A1" ('i' < 't'), so an unanchored match would
    # silently return the decoy instead of the real profile. No such profile
    # exists in the installed tree today, but nothing stops a vendor update
    # from introducing one.
    decoy_index = dict(INDEX, **{
        "0.20mm Silent Standard @BBL A1": {"name": "0.20mm Silent Standard @BBL A1"},
    })
    got = resolve_preset("standard", "N2S", "0.4", decoy_index)
    assert got["process"] == "0.20mm Standard @BBL A1"


def test_resolve_preset_finds_the_standard_tier_for_an_a1():
    got = resolve_preset("standard", "N2S", "0.4", INDEX)
    assert got["process"] == "0.20mm Standard @BBL A1"
    assert got["label"] == "Standard 0.20 mm"


def test_the_mini_uses_the_a1m_process_token():
    # The mini is "A1 mini" in machine profiles but "A1M" in process ones.
    # Using one token for both silently resolves nothing.
    got = resolve_preset("standard", "N1", "0.4", INDEX)
    assert got["process"] == "0.20mm Standard @BBL A1M"


def test_the_nozzle_suffix_is_omitted_at_04_and_present_otherwise():
    assert resolve_preset("standard", "N2S", "0.4", INDEX)["process"] \
        == "0.20mm Standard @BBL A1"
    assert resolve_preset("standard", "N2S", "0.6", INDEX)["process"] \
        == "0.30mm Standard @BBL A1 0.6 nozzle"


def test_the_label_is_read_off_the_resolved_name_not_hardcoded():
    # "Standard" is 0.20mm on a 0.4 nozzle and 0.30mm on a 0.6. A hardcoded
    # label would show a layer height the printer is not using.
    assert resolve_preset("standard", "N2S", "0.6", INDEX)["label"] \
        == "Standard 0.30 mm"


def test_resolve_preset_returns_none_when_nothing_matches():
    assert resolve_preset("standard", "N2S", "0.8", INDEX) is None
    assert resolve_preset("nosuchtier", "N2S", "0.4", INDEX) is None
    assert resolve_preset("standard", "", "0.4", INDEX) is None


def test_available_presets_lists_only_what_resolves():
    got = available_presets("N2S", "0.4", INDEX)
    assert [p["id"] for p in got] == ["standard", "fine", "draft"]
    assert available_presets("N2S", "0.8", INDEX) == []


def test_every_tier_is_exercised_by_the_table():
    assert set(TIERS) == {"standard", "fine", "draft"}


FIL_INDEX = dict(INDEX, **{
    "Generic PLA @BBL A1": {"name": "Generic PLA @BBL A1"},
    "Generic PETG @BBL A1": {"name": "Generic PETG @BBL A1"},
    "Generic PLA @BBL A1M": {"name": "Generic PLA @BBL A1M"},
})


def test_detects_the_material_from_an_ams_tray():
    state = {"ams": {"ams": [{"tray": [{"tray_type": "PLA"}]}]}}
    assert detect_loaded_filament(state) == "PLA"


def test_falls_back_to_the_external_spool():
    # An A1 with no AMS reports vt_tray instead.
    assert detect_loaded_filament({"vt_tray": {"tray_type": "PETG"}}) == "PETG"


def test_prefers_a_loaded_ams_tray_over_an_empty_one():
    state = {"ams": {"ams": [{"tray": [{"tray_type": ""},
                                       {"tray_type": "ABS"}]}]}}
    assert detect_loaded_filament(state) == "ABS"


def test_returns_none_for_an_unidentifiable_spool():
    # The normal case for third-party filament with no RFID tag. Must not
    # block slicing -- the UI just leaves the dropdown on its default.
    assert detect_loaded_filament({}) is None
    assert detect_loaded_filament({"vt_tray": {"tray_type": ""}}) is None
    assert detect_loaded_filament({"ams": {"ams": []}}) is None
    assert detect_loaded_filament(None) is None


def test_tolerates_a_malformed_state_without_raising():
    # MQTT state is merged from partial reports; anything can be any shape.
    assert detect_loaded_filament({"ams": "nope"}) is None
    assert detect_loaded_filament({"ams": {"ams": [{"tray": "nope"}]}}) is None
    assert detect_loaded_filament({"vt_tray": ["nope"]}) is None


def test_filament_profile_name_uses_the_process_token():
    assert filament_profile_name("PLA", "N2S") == "Generic PLA @BBL A1"
    assert filament_profile_name("PLA", "N1") == "Generic PLA @BBL A1M"
    assert filament_profile_name("NOSUCH", "N2S") == ""


def test_available_filaments_lists_only_what_resolves():
    got = available_filaments("N2S", FIL_INDEX)
    assert [f["material"] for f in got] == ["PLA", "PETG"]
    assert got[0]["profile"] == "Generic PLA @BBL A1"


# --- Feature A: presets for P1P and P1S (2026-07-25) ---------------------
# A miniature index carrying one profile per newly-supported model, named
# exactly as they appear in the installed vendor tree (verified 2026-07-25).
MULTI_INDEX = dict(INDEX, **{
    "Bambu Lab P1P 0.4 nozzle": {"name": "Bambu Lab P1P 0.4 nozzle"},
    "Bambu Lab P1S 0.4 nozzle": {"name": "Bambu Lab P1S 0.4 nozzle"},
    "0.20mm Standard @BBL P1P": {"name": "0.20mm Standard @BBL P1P"},
    # Generic filaments -- the base INDEX only has process profiles, so add one
    # filament per supported process token (A1, A1M, P1P) for the resolution
    # guard below.
    "Generic PLA @BBL A1": {"name": "Generic PLA @BBL A1"},
    "Generic PLA @BBL A1M": {"name": "Generic PLA @BBL A1M"},
    "Generic PLA @BBL P1P": {"name": "Generic PLA @BBL P1P"},
})


def test_p1p_resolves_a_preset_and_a_filament():
    got = resolve_preset("standard", "C11", "0.4", MULTI_INDEX)
    assert got["process"] == "0.20mm Standard @BBL P1P"
    assert filament_profile_name("PLA", "C11") == "Generic PLA @BBL P1P"
    assert {f["material"] for f in available_filaments("C11", MULTI_INDEX)} \
        >= {"PLA"}


def test_p1s_machine_is_p1s_but_process_and_filament_reuse_p1p():
    # THE TRAP: the P1S has its own MACHINE profile but no @BBL P1S process or
    # filament -- both reuse P1P. A naive PROCESS_TOKENS["C12"]="P1S" resolves
    # nothing. Machine name must be the P1S; process/filament must be P1P.
    assert machine_profile_name("C12", "0.4") == "Bambu Lab P1S 0.4 nozzle"
    got = resolve_preset("standard", "C12", "0.4", MULTI_INDEX)
    assert got["process"] == "0.20mm Standard @BBL P1P"     # P1P, not P1S
    assert got["machine"] == "Bambu Lab P1S 0.4 nozzle"     # but the P1S bed
    assert filament_profile_name("PLA", "C12") == "Generic PLA @BBL P1P"


def test_every_supported_model_resolves_something():
    # Guard: every model we claim to support must resolve at least one preset
    # AND one filament against its real profile names -- a wrong token is a
    # silent empty dropdown otherwise.
    from server.slicepresets import MACHINE_TOKENS
    assert set(MACHINE_TOKENS) == {"N2S", "N1", "C11", "C12"}, \
        "a model was added/removed without updating this guard"
    for mid in MACHINE_TOKENS:
        assert available_presets(mid, "0.4", MULTI_INDEX), f"{mid}: no presets"
        assert available_filaments(mid, MULTI_INDEX), f"{mid}: no filaments"
