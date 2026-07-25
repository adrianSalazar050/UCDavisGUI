"""The curated slicing presets, and mapping a printer to vendor profile names.

Curated in-repo rather than enumerated from the installed slicer so the set is
reproducible, reviewable in git, and survives a slicer reinstall.

A preset CANNOT be a literal profile name. Measured 2026-07-22:
  - the mini's machine token is "A1 mini" but its process token is "A1M",
  - the nozzle suffix is omitted at 0.4 ("@BBL A1") and present otherwise
    ("@BBL A1 0.6 nozzle"),
  - the layer height is not constant across nozzles: "Standard" is 0.20mm on
    a 0.4 nozzle and 0.30mm on a 0.6.
So a preset is a quality TIER, resolved against the profile index, and the
label is read back off whatever resolved.
"""
from __future__ import annotations

import re

# Preset id -> the vendor's quality-tier word, as it appears in profile names.
TIERS = {
    "standard": "Standard",
    "fine": "Optimal",
    "draft": "Extra Draft",
}

# Bambu model id -> the token used in MACHINE profile names.
# Every token here is verified against the installed vendor profiles
# (resources/profiles/BBL/**), not derived -- see the two traps below.
MACHINE_TOKENS = {
    "N2S": "A1",        # A1        (verified on hardware)
    "N1": "A1 mini",    # A1 mini
    "C11": "P1P",       # P1P
    "C12": "P1S",       # P1S       -- its OWN machine profile...
    "BL-P001": "X1 Carbon",  # X1 Carbon -- machine token has a SPACE
}

# Bambu model id -> the token used in PROCESS and FILAMENT profile names.
# NOT derivable from MACHINE_TOKENS -- verified against REAL sliced files, not
# just profile-name guessing. The traps here are the whole reason this is a
# separate map:
#   - the mini is "A1 mini" in machine names but "A1M" here;
#   - THE P1/X1 FAMILY SHARES "X1C" for process AND filament. Confirmed by
#     real Bambu-Studio-sliced examples on 2026-07-25: a P1S slice recorded
#     printer_settings_id "Bambu Lab P1S 0.4 nozzle" but print_settings_id
#     "0.20mm Standard @BBL X1C" and filament "... @BBL X1C"; an X1 Carbon
#     slice did the same. So the MACHINE token stays per-model (P1S / P1P /
#     X1 Carbon -- the bed), but the process/filament token is "X1C" for all
#     of them. (A "@BBL P1P" process profile also exists and resolves, but it
#     is NOT what Bambu Studio picks for these machines -- matching the tool's
#     own choice is what keeps our slices identical to a hand slice.)
# The PLAIN X1 (BL-P002) is still absent on purpose: its only process profiles
# are 0.6/0.8-nozzle Standard, so nothing resolves at the default 0.4 nozzle.
PROCESS_TOKENS = {
    "N2S": "A1",        # A1
    "N1": "A1M",        # A1 mini   -- process token differs from machine
    "C11": "X1C",       # P1P       -- P1/X1 family shares X1C (see above)
    "C12": "X1C",       # P1S       -- CONFIRMED by a real P1S slice
    "BL-P001": "X1C",   # X1 Carbon -- CONFIRMED by a real X1 Carbon slice
}


def machine_profile_name(model_id: str, nozzle: str) -> str:
    """Machine profile name for a printer, or "" when the model is unknown.

    "" means unknown and unknown never blocks (master.md 5.3) -- here it means
    the slice options come back empty and the UI says so, rather than the
    server guessing a machine and slicing for the wrong bed.
    """
    token = MACHINE_TOKENS.get(model_id)
    if not token:
        return ""
    return f"Bambu Lab {token} {nozzle} nozzle"


def _process_suffix(nozzle: str) -> str:
    # 0.4 is the default nozzle and its profiles carry no suffix at all.
    return "" if nozzle == "0.4" else f" {nozzle} nozzle"


def resolve_preset(tier_id: str, model_id: str, nozzle: str,
                   index: dict) -> dict | None:
    """-> {"id", "process", "machine", "label"} or None when unavailable.

    None is normal: not every tier exists for every nozzle. The options route
    filters on it so an unavailable combination is a missing choice rather
    than a slice that fails late.
    """
    tier = TIERS.get(tier_id)
    machine = machine_profile_name(model_id, nozzle)
    token = PROCESS_TOKENS.get(model_id)
    if not tier or not machine or not token or machine not in index:
        return None
    # Fully anchored end-to-end (not just start+end independently): a name
    # like "0.20mm Silent Standard @BBL A1" would satisfy a naive
    # startswith-layer-height / endswith-tier-suffix pair for tier
    # "standard", and -- because "Silent Standard" sorts before "Standard"
    # -- would silently win over the real profile. fullmatch on one pattern
    # closes that gap; nothing can hide between the two anchors.
    pattern = re.compile(
        rf"^(\d+\.\d+)mm {re.escape(tier)} @BBL "
        rf"{re.escape(token)}{re.escape(_process_suffix(nozzle))}$")
    for name in sorted(index):
        m = pattern.fullmatch(name)
        if not m:
            continue
        return {"id": tier_id, "process": name, "machine": machine,
                # Read off the resolved name, never hardcoded -- see the
                # module docstring on layer height varying by nozzle.
                "label": f"{tier} {m.group(1)} mm"}
    return None


def available_presets(model_id: str, nozzle: str, index: dict) -> list:
    """Every tier that actually resolves, in TIERS order."""
    out = [resolve_preset(t, model_id, nozzle, index) for t in TIERS]
    return [p for p in out if p is not None]


# Materials offered, in menu order. Generic rather than Bambu-branded profiles
# because a spool the printer could not identify is, by definition, not a
# tagged Bambu spool -- and that is the common case.
#
# Deliberately doing double duty: this is both the offered-menu list (here,
# available_filaments) AND the detection whitelist (_material, below) --
# anything the MQTT state reports that isn't in this tuple is treated as
# unidentifiable. That coupling is intentional, not an oversight: it keeps
# "materials we can slice for" and "materials we'll recognize" from ever
# drifting apart.
MATERIALS = ("PLA", "PETG", "ABS", "TPU")


def _filament_candidates(material: str, token: str) -> list:
    """Candidate filament-profile names for a material, most-generic first.

    Different model families name their baseline filament differently, and
    resolving against the installed index (rather than one hardcoded formula)
    is what lets one code path cover all of them:
      - A1 / A1 mini / P1P / P1S -> "Generic <material> @BBL <token>";
      - X1 Carbon has NO Generic profiles -- PLA/PETG are "Bambu <m> Basic",
        ABS is "Bambu ABS" (no "Basic"), TPU is grade-specific "Bambu TPU 95A"
        (95A is the common grade). Verified against the tree 2026-07-25.
    """
    return [
        f"Generic {material} @BBL {token}",       # A/P-series
        f"Bambu {material} Basic @BBL {token}",   # X1C PLA/PETG
        f"Bambu {material} @BBL {token}",         # X1C ABS
        f"Bambu {material} 95A @BBL {token}",     # X1C TPU (grade 95A)
    ]


def filament_profile_name(material: str, model_id: str, index: dict) -> str:
    """The best filament profile for a material on a printer, verified to
    exist in `index`, or "" when none resolves. Filament profiles use the
    process token (A1 / A1M / X1C), not the machine one, and the exact name
    varies by model family -- see _filament_candidates."""
    token = PROCESS_TOKENS.get(model_id)
    if not token or material not in MATERIALS:
        return ""
    for candidate in _filament_candidates(material, token):
        if candidate in index:
            return candidate
    return ""


def available_filaments(model_id: str, index: dict) -> list:
    """Every material whose profile this slicer actually ships."""
    out = []
    for material in MATERIALS:
        name = filament_profile_name(material, model_id, index)
        if name:
            out.append({"material": material, "profile": name})
    return out


def detect_loaded_filament(state) -> str | None:
    """Material currently loaded, read off the live MQTT state, or None.

    None is the NORMAL case, not an error: the printer only knows the material
    when an RFID-tagged Bambu spool is loaded, and most filament is not that.
    The UI prefills with this and stays editable, so an unidentifiable spool
    never blocks slicing.

    Returns the FIRST identifiable tray in AMS unit/slot traversal order --
    there is no "active tray" signal in this state to read instead. On a
    mixed-material AMS (e.g. PLA in slot 1, PETG in slot 2) this can return
    a material other than the one actually feeding the nozzle. That is
    acceptable because the UI keeps the field editable, but it means this
    value must never be treated as authoritative.

    Pure, and deliberately paranoid about shape: state is deep-merged from
    partial MQTT reports (master.md 3.1), so any node can be missing or be
    the wrong type at any moment.
    """
    if not isinstance(state, dict):
        return None
    ams = state.get("ams")
    if isinstance(ams, dict):
        units = ams.get("ams")
        if isinstance(units, list):
            for unit in units:
                if not isinstance(unit, dict):
                    continue
                trays = unit.get("tray")
                if not isinstance(trays, list):
                    continue
                for tray in trays:
                    if not isinstance(tray, dict):
                        continue
                    found = _material(tray.get("tray_type"))
                    if found:
                        return found
    vt_tray = state.get("vt_tray")
    return _material(vt_tray.get("tray_type")) if isinstance(vt_tray, dict) else None


def _material(value) -> str | None:
    """A tray_type string -> a material we have a profile for, else None."""
    if not isinstance(value, str):
        return None
    value = value.strip().upper()
    return value if value in MATERIALS else None
