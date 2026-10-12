"""Persistent, declarative robot-cell profiles.

Profiles describe marker-relative steps; they never bypass RobotManager or
MoveIt. Contact remains a deliberately bounded, supervised MoveIt approach:
it is not force-controlled contact and therefore cannot prove that a fixture
or a part was touched.
"""
from __future__ import annotations

import json
import math
import pathlib
import threading
import time


STEP_KINDS = {"scan", "pose", "gripper", "hold", "contact"}
PROFILE_ROLES = {"printer", "scrape", "box", "return"}
RISK_LEVELS = {"low", "medium", "high"}


class ProfileError(ValueError):
    pass


class CellProfileStore:
    def __init__(self, path):
        self.path = pathlib.Path(path)
        self.lock = threading.RLock()
        self.profiles = {}
        self._load()

    def _load(self):
        if not self.path.is_file():
            return
        try:
            self.profiles = dict(json.loads(self.path.read_text()).get("profiles", {}))
        except (OSError, ValueError, TypeError):
            self.profiles = {}

    def _save(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_suffix(".tmp")
        temporary.write_text(json.dumps({"schema_version": 1,
            "profiles": self.profiles}, indent=2) + "\n")
        temporary.replace(self.path)

    def snapshot(self):
        with self.lock:
            return {"schema_version": 1, "profiles": list(self.profiles.values()),
                    "contact_execution_enabled": True}

    def save(self, data):
        profile = self._validate(dict(data))
        with self.lock:
            old = self.profiles.get(profile["id"])
            profile["version"] = int((old or {}).get("version", 0)) + 1
            profile["updated_at"] = time.time()
            profile["status"] = "draft"
            self.profiles[profile["id"]] = profile
            self._save()
            return dict(profile)

    def delete(self, profile_id):
        with self.lock:
            if profile_id not in self.profiles:
                raise ProfileError("profile does not exist")
            del self.profiles[profile_id]
            self._save()

    def get(self, profile_id):
        with self.lock:
            profile = self.profiles.get(profile_id)
            if profile is None:
                raise ProfileError("profile does not exist")
            # A command must not observe a caller mutating the stored dict.
            return json.loads(json.dumps(profile))

    def _validate(self, value):
        profile_id = str(value.get("id", "")).strip()
        if not profile_id or len(profile_id) > 64:
            raise ProfileError("profile id is required (64 characters maximum)")
        role = value.get("role")
        if role not in PROFILE_ROLES:
            raise ProfileError("profile role must be printer, scrape, box, or return")
        try:
            marker_id = int(value.get("marker_id"))
        except (TypeError, ValueError) as exc:
            raise ProfileError("marker_id must be an integer") from exc
        steps = value.get("steps")
        if not isinstance(steps, list) or not steps:
            raise ProfileError("a profile requires at least one step")
        cleaned = []
        for index, step in enumerate(steps, 1):
            if not isinstance(step, dict) or step.get("kind") not in STEP_KINDS:
                raise ProfileError(f"step {index} has an unsupported kind")
            item = {"kind": step["kind"], "label": str(step.get("label", "")).strip()[:120]}
            if not item["label"]:
                raise ProfileError(f"step {index} needs a label")
            if item["kind"] == "pose":
                reference = step.get("reference", "marker")
                if reference not in {"marker", "previous_tcp", "base"}:
                    raise ProfileError(
                        f"pose step {index} reference must be marker, previous_tcp, or base")
                if reference == "previous_tcp" and index == 1:
                    raise ProfileError(
                        "the first step cannot be relative to the previous TCP pose")
                item["reference"] = reference
                target_frame = step.get("target_frame", "tcp")
                if target_frame not in {"tcp", "camera"}:
                    raise ProfileError(
                        f"pose step {index} target frame must be tcp or camera")
                item["target_frame"] = target_frame
                offset = step.get("offset")
                if not isinstance(offset, list) or len(offset) != 6:
                    raise ProfileError(f"pose step {index} needs six numeric offsets")
                try:
                    item["offset"] = [float(x) for x in offset]
                except (TypeError, ValueError) as exc:
                    raise ProfileError(f"pose step {index} has invalid offsets") from exc
                self._finite(item["offset"], f"pose step {index}")
                translation_limit = 2.0 if reference in {"marker", "base"} else 0.30
                if any(abs(x) > translation_limit for x in item["offset"][:3]):
                    raise ProfileError(
                        f"pose step {index} translation must be within ±{translation_limit:g} m")
                if any(abs(x) > math.tau for x in item["offset"][3:]):
                    raise ProfileError(f"pose step {index} orientation must be within ±2π rad")
                if reference == "previous_tcp":
                    translation_frame = step.get("translation_frame", "base")
                    orientation_frame = step.get("orientation_frame", "tool")
                    if translation_frame not in {"base", "tool"}:
                        raise ProfileError(
                            f"pose step {index} translation frame must be base or tool")
                    if orientation_frame not in {"base", "tool"}:
                        raise ProfileError(
                            f"pose step {index} orientation frame must be base or tool")
                    item["translation_frame"] = translation_frame
                    item["orientation_frame"] = orientation_frame
                elif reference == "marker":
                    translation_frame = step.get("translation_frame", "marker")
                    orientation_frame = step.get("orientation_frame", "marker")
                    if translation_frame not in {"marker", "base"}:
                        raise ProfileError(
                            f"pose step {index} translation frame must be marker or base")
                    if orientation_frame not in {"marker", "base"}:
                        raise ProfileError(
                            f"pose step {index} orientation frame must be marker or base")
                    item["translation_frame"] = translation_frame
                    item["orientation_frame"] = orientation_frame
                else:
                    # A fixed cell-frame target is intentionally independent
                    # of vision. It is useful after a first marker-based
                    # approach takes the wrist out of the camera's view.
                    item["target_frame"] = "tcp"
                    item["translation_frame"] = "base"
                    item["orientation_frame"] = "base"
                item["speed_scale"] = self._number(step.get("speed_scale", 0.15), f"pose step {index} speed scale", 0.01, 1.0)
                item["acceleration_scale"] = self._number(step.get("acceleration_scale", 0.10), f"pose step {index} acceleration scale", 0.01, 1.0)
            elif item["kind"] == "scan":
                item["distance_m"] = self._number(step.get("distance_m", 0.15), f"scan step {index} distance", 0.05, 0.50)
                item["timeout_s"] = self._number(step.get("timeout_s", 4.0), f"scan step {index} timeout", 0.5, 30.0)
            elif item["kind"] == "gripper":
                if step.get("action") not in {"open", "close"}:
                    raise ProfileError(f"gripper step {index} must be open or close")
                item["action"] = step["action"]
                item["settle_s"] = self._number(step.get("settle_s", 0.5), f"gripper step {index} settle time", 0.0, 10.0)
            elif item["kind"] in {"hold", "contact"}:
                item["duration_s"] = self._number(step.get("duration_s", 0.5), f"{item['kind']} step {index} duration", 0.0, 60.0)
                if item["kind"] == "contact":
                    item["requires_physical_validation"] = True
                    # This acknowledgement belongs to this exact saved step;
                    # editing any value requires the operator to affirm it
                    # again before a physical profile may run.
                    item["physically_validated"] = bool(
                        step.get("physically_validated", False))
                    direction = step.get("direction", "-z")
                    if direction not in {"+x", "-x", "+y", "-y", "+z", "-z"}:
                        raise ProfileError(f"contact step {index} has an invalid direction")
                    item["direction"] = direction
                    frame = step.get("frame", "base")
                    if frame not in {"base", "tool"}:
                        raise ProfileError(
                            f"contact step {index} frame must be base or tool")
                    item["frame"] = frame
                    item["travel_m"] = self._number(step.get("travel_m", 0.01), f"contact step {index} travel", 0.001, 0.10)
                    item["speed_mm_s"] = self._number(step.get("speed_mm_s", 5.0), f"contact step {index} speed", 1.0, 20.0)
            cleaned.append(item)
        risk_level = value.get("risk_level", "medium")
        if risk_level not in RISK_LEVELS:
            raise ProfileError("risk level must be low, medium, or high")
        return {"id": profile_id, "role": role, "marker_id": marker_id,
                "purpose": str(value.get("purpose", "")).strip()[:240],
                "risk_level": risk_level,
                "notes": str(value.get("notes", "")).strip()[:1000],
                "requires_operator_confirmation": bool(value.get("requires_operator_confirmation", True)),
                "steps": cleaned}

    @staticmethod
    def _finite(values, label):
        if not all(math.isfinite(value) for value in values):
            raise ProfileError(f"{label} values must be finite")

    @classmethod
    def _number(cls, value, label, minimum, maximum):
        try:
            number = float(value)
        except (TypeError, ValueError) as exc:
            raise ProfileError(f"{label} must be numeric") from exc
        if not math.isfinite(number) or not minimum <= number <= maximum:
            raise ProfileError(f"{label} must be between {minimum} and {maximum}")
        return number
