from server.slicepresets import (TIERS, available_presets,
                                 machine_profile_name, resolve_preset)

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
