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
MACHINE_TOKENS = {"N2S": "A1", "N1": "A1 mini"}

# Bambu model id -> the token used in PROCESS and FILAMENT profile names.
# Not derivable from MACHINE_TOKENS: the mini is "A1M" here, "A1 mini" there.
PROCESS_TOKENS = {"N2S": "A1", "N1": "A1M"}

# Layer height prefix on a process profile name, e.g. "0.20mm Standard @...".
_LAYER_RE = re.compile(r"^(\d+\.\d+)mm ")


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
    ending = f"{tier} @BBL {token}{_process_suffix(nozzle)}"
    for name in sorted(index):
        if not name.endswith(ending):
            continue
        m = _LAYER_RE.match(name)
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
