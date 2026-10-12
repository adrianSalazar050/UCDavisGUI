"""Persistent, operator-authored safety layout for the robot cell.

The layout is declarative only in this first release. It deliberately does
not command a robot; it records the physical assumptions that later dry-run
validation will enforce.
"""
from __future__ import annotations

import json
import math
import pathlib
import threading
import time


ZONE_TYPES = {"allowed", "keepout", "restricted", "fixture", "observation"}


class LayoutError(ValueError):
    pass


def default_layout():
    return {
        "schema_version": 1,
        "frame": {"name": "link_base", "axes_confirmed": False,
                  "notes": "Confirm physical axes before approving a layout."},
        "workspace": {"x_min": -0.60, "x_max": 0.60,
                      "y_min": -0.60, "y_max": 0.80,
                      "z_min": 0.025, "z_max": 0.900},
        "zones": [], "updated_at": None,
    }


class CellLayoutStore:
    def __init__(self, path):
        self.path = pathlib.Path(path)
        self.lock = threading.RLock()
        self.layout = default_layout()
        self._load()

    def _load(self):
        if not self.path.is_file():
            return
        try:
            self.layout = self._validate(json.loads(self.path.read_text()))
        except (OSError, ValueError, TypeError):
            self.layout = default_layout()

    def snapshot(self):
        with self.lock:
            return json.loads(json.dumps(self.layout))

    def save(self, value):
        layout = self._validate(dict(value))
        layout["updated_at"] = time.time()
        with self.lock:
            self.layout = layout
            self.path.parent.mkdir(parents=True, exist_ok=True)
            temporary = self.path.with_suffix(".tmp")
            temporary.write_text(json.dumps(layout, indent=2) + "\n")
            temporary.replace(self.path)
            return self.snapshot()

    @staticmethod
    def _number(value, label, low, high):
        try:
            value = float(value)
        except (TypeError, ValueError) as exc:
            raise LayoutError(f"{label} must be a number") from exc
        if not math.isfinite(value) or not low <= value <= high:
            raise LayoutError(f"{label} must be between {low:g} and {high:g} m")
        return value

    def _validate(self, value):
        workspace = value.get("workspace", {})
        cleaned_workspace = {
            key: self._number(workspace.get(key), key.replace("_", " "), -5.0, 5.0)
            for key in ("x_min", "x_max", "y_min", "y_max", "z_min", "z_max")
        }
        for axis in ("x", "y", "z"):
            if cleaned_workspace[f"{axis}_min"] >= cleaned_workspace[f"{axis}_max"]:
                raise LayoutError(f"{axis} minimum must be below its maximum")
        frame = value.get("frame", {})
        if frame.get("name", "link_base") != "link_base":
            raise LayoutError("only link_base is supported until cell-frame calibration is implemented")
        zones = value.get("zones", [])
        if not isinstance(zones, list) or len(zones) > 64:
            raise LayoutError("layout supports between 0 and 64 zones")
        cleaned_zones, ids = [], set()
        for index, zone in enumerate(zones, 1):
            if not isinstance(zone, dict):
                raise LayoutError(f"zone {index} is invalid")
            zone_id = str(zone.get("id", "")).strip().lower()
            if not zone_id or len(zone_id) > 64 or not all(c.isalnum() or c in "_-" for c in zone_id):
                raise LayoutError(f"zone {index} needs an id using letters, digits, _ or -")
            if zone_id in ids:
                raise LayoutError(f"zone id {zone_id} is repeated")
            ids.add(zone_id)
            kind = zone.get("kind")
            if kind not in ZONE_TYPES:
                raise LayoutError(f"zone {zone_id} has an invalid type")
            points = zone.get("points")
            if not isinstance(points, list) or not 3 <= len(points) <= 32:
                raise LayoutError(f"zone {zone_id} needs 3 to 32 top-view points")
            clean_points = []
            for point in points:
                if not isinstance(point, (list, tuple)) or len(point) != 2:
                    raise LayoutError(f"zone {zone_id} has an invalid point")
                clean_points.append([self._number(point[0], f"zone {zone_id} x", -5, 5),
                                     self._number(point[1], f"zone {zone_id} y", -5, 5)])
            twice_area = sum(clean_points[i][0] * clean_points[(i + 1) % len(clean_points)][1]
                - clean_points[(i + 1) % len(clean_points)][0] * clean_points[i][1]
                for i in range(len(clean_points)))
            if abs(twice_area) < 0.0001:
                raise LayoutError(f"zone {zone_id} polygon has no area")
            z_min = self._number(zone.get("z_min"), f"zone {zone_id} z minimum", -5, 5)
            z_max = self._number(zone.get("z_max"), f"zone {zone_id} z maximum", -5, 5)
            if z_min >= z_max:
                raise LayoutError(f"zone {zone_id} z minimum must be below its maximum")
            cleaned_zones.append({"id": zone_id,
                "name": str(zone.get("name", zone_id)).strip()[:100] or zone_id,
                "kind": kind, "points": clean_points, "z_min": z_min, "z_max": z_max,
                "notes": str(zone.get("notes", "")).strip()[:500]})
        return {"schema_version": 1,
                "frame": {"name": "link_base", "axes_confirmed": bool(frame.get("axes_confirmed")),
                          "notes": str(frame.get("notes", "")).strip()[:500]},
                "workspace": cleaned_workspace, "zones": cleaned_zones,
                "updated_at": value.get("updated_at")}
