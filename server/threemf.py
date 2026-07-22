"""Parse a sliced Bambu .gcode.3mf for its estimated print time + filament use.

A .gcode.3mf is a zip; Metadata/slice_info.config is XML with one <plate> per
plate, each carrying <metadata key="prediction" .../> (seconds) and
<metadata key="weight" .../> (grams), plus <filament .../> rows. Confirmed
against a real A1 mini file. Pure and tolerant: any missing/corrupt part yields
None for that field, so the queue UI can fall back to manual entry.
"""
from __future__ import annotations

import io
import xml.etree.ElementTree as ET
import zipfile

SLICE_INFO_PATH = "Metadata/slice_info.config"

_EMPTY = {"seconds": None, "grams": None, "filaments": [],
          "printer_model_id": None}


def _num(v, cast):
    try:
        return cast(v)
    except (TypeError, ValueError):
        return None


def parse_slice_info(data: bytes) -> dict:
    """bytes of a .gcode.3mf -> {seconds:int|None, grams:float|None,
    filaments:[{type,color,used_g}], printer_model_id:str|None}.
    Never raises."""
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as z:
            raw = z.read(SLICE_INFO_PATH)
    except (zipfile.BadZipFile, KeyError, OSError):
        return dict(_EMPTY)
    try:
        root = ET.fromstring(raw)
    except ET.ParseError:
        return dict(_EMPTY)

    seconds, grams, filaments, model_id = None, None, [], None
    for plate in root.iter("plate"):
        for md in plate.findall("metadata"):
            key, val = md.get("key"), md.get("value")
            if key == "printer_model_id":
                # Every plate in one file is sliced for the same printer, so
                # the first non-empty value wins and later plates can't blank
                # it. None (not "") means "unknown", which the model check
                # treats as "do not block".
                if model_id is None and val:
                    model_id = val
            elif key == "prediction":
                s = _num(val, int)
                if s is not None:
                    seconds = (seconds or 0) + s
            elif key == "weight":
                g = _num(val, float)
                if g is not None:
                    grams = (grams or 0.0) + g
        for f in plate.findall("filament"):
            filaments.append({"type": f.get("type"), "color": f.get("color"),
                              "used_g": _num(f.get("used_g"), float)})
    if grams is not None:
        grams = round(grams, 2)
    return {"seconds": seconds, "grams": grams, "filaments": filaments,
            "printer_model_id": model_id}
