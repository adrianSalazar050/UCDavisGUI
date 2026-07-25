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
    idx = {"Generic PLA @BBL A1": {}, "Generic PLA @BBL A1M": {}}
    assert filament_profile_name("PLA", "N2S", idx) == "Generic PLA @BBL A1"
    assert filament_profile_name("PLA", "N1", idx) == "Generic PLA @BBL A1M"
    assert filament_profile_name("NOSUCH", "N2S", idx) == ""
    # A material with no profile in the index resolves to "".
    assert filament_profile_name("ABS", "N2S", idx) == ""


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
    # The P1/X1 family shares the X1C process + filament profiles (verified
    # against real P1S/X1C slices 2026-07-25), so the fixture carries the X1C
    # names, not @BBL P1P.
    "0.20mm Standard @BBL X1C": {"name": "0.20mm Standard @BBL X1C"},
    "Generic PLA @BBL A1": {"name": "Generic PLA @BBL A1"},
    "Generic PLA @BBL A1M": {"name": "Generic PLA @BBL A1M"},
    "Bambu PLA Basic @BBL X1C": {},   # X-series has no "Generic PLA"
})


def test_p1p_resolves_via_the_shared_x1c_process_and_filament():
    # The P1P's bed is its own machine profile, but its process + filament come
    # from the shared X1C profiles (real slices confirm the family sharing).
    assert machine_profile_name("C11", "0.4") == "Bambu Lab P1P 0.4 nozzle"
    got = resolve_preset("standard", "C11", "0.4", MULTI_INDEX)
    assert got["process"] == "0.20mm Standard @BBL X1C"
    assert filament_profile_name("PLA", "C11", MULTI_INDEX) == "Bambu PLA Basic @BBL X1C"


def test_p1s_machine_is_p1s_but_process_and_filament_are_x1c():
    # THE TRAP, confirmed by a real P1S slice: the P1S has its own MACHINE
    # profile ("Bambu Lab P1S") but Bambu Studio slices it with the X1C
    # process/filament profiles. Machine token = P1S (the bed); process/
    # filament token = X1C (the family-shared settings).
    assert machine_profile_name("C12", "0.4") == "Bambu Lab P1S 0.4 nozzle"
    got = resolve_preset("standard", "C12", "0.4", MULTI_INDEX)
    assert got["process"] == "0.20mm Standard @BBL X1C"     # X1C, not P1S/P1P
    assert got["machine"] == "Bambu Lab P1S 0.4 nozzle"     # but the P1S bed
    assert filament_profile_name("PLA", "C12", MULTI_INDEX) == "Bambu PLA Basic @BBL X1C"


# --- Follow-up: X1 Carbon (X1C), which has no Generic <material> profiles ---
# Real X1C filament names (verified 2026-07-25): PLA/PETG are "Bambu <m> Basic",
# ABS is "Bambu ABS" (no Basic), TPU is grade-specific "Bambu TPU 95A".
X1C_INDEX = {
    "Bambu Lab X1 Carbon 0.4 nozzle": {"name": "Bambu Lab X1 Carbon 0.4 nozzle"},
    "0.20mm Standard @BBL X1C": {"name": "0.20mm Standard @BBL X1C"},
    "Bambu PLA Basic @BBL X1C": {},
    "Bambu PETG Basic @BBL X1C": {},
    "Bambu ABS @BBL X1C": {},
    "Bambu TPU 95A @BBL X1C": {},
}


def test_x1_carbon_machine_token_has_a_space_but_process_token_is_x1c():
    assert machine_profile_name("BL-P001", "0.4") == "Bambu Lab X1 Carbon 0.4 nozzle"
    got = resolve_preset("standard", "BL-P001", "0.4", X1C_INDEX)
    assert got["process"] == "0.20mm Standard @BBL X1C"
    assert got["machine"] == "Bambu Lab X1 Carbon 0.4 nozzle"


def test_x1c_filaments_resolve_via_the_candidate_list():
    # The X-series has no Generic profiles -- each material resolves to a
    # different Bambu-branded base name, picked from the index.
    assert filament_profile_name("PLA", "BL-P001", X1C_INDEX) == "Bambu PLA Basic @BBL X1C"
    assert filament_profile_name("PETG", "BL-P001", X1C_INDEX) == "Bambu PETG Basic @BBL X1C"
    assert filament_profile_name("ABS", "BL-P001", X1C_INDEX) == "Bambu ABS @BBL X1C"
    assert filament_profile_name("TPU", "BL-P001", X1C_INDEX) == "Bambu TPU 95A @BBL X1C"
    assert {f["material"] for f in available_filaments("BL-P001", X1C_INDEX)} \
        == {"PLA", "PETG", "ABS", "TPU"}


def test_every_supported_model_resolves_something():
    # Guard: every model we claim to support must resolve at least one preset
    # AND one filament against its real profile names -- a wrong token is a
    # silent empty dropdown otherwise.
    from server.slicepresets import MACHINE_TOKENS
    assert set(MACHINE_TOKENS) == {"N2S", "N1", "C11", "C12", "BL-P001"}, \
        "a model was added/removed without updating this guard"
    idx = dict(MULTI_INDEX, **X1C_INDEX)   # covers every supported model
    for mid in MACHINE_TOKENS:
        assert available_presets(mid, "0.4", idx), f"{mid}: no presets"
        assert available_filaments(mid, idx), f"{mid}: no filaments"
