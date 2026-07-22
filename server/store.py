"""Persistence for the registered-printer list (printers.json).

Holds the LAN access code in PLAINTEXT. That is deliberate -- the same trust
model bambu_link.py already takes by disabling TLS verification on a LAN --
but it is exactly why printers.json is in .gitignore. Never echo access_code
back out of the API.

Kept free of threads and sockets so it is testable against a tmp_path; the
lifecycle side lives in registry.py.
"""
from __future__ import annotations

import dataclasses
import json
import logging
import os
import pathlib
import tempfile

log = logging.getLogger("server.store")

# The 6 classes the failure detector emits (FAILURE_DETECTOR_REPORT.md). The
# only values accepted for armed_classes; anything else is dropped.
DETECTION_CLASSES = ("blobs", "cracks", "over_extrusion", "spaghetti",
                     "stringing", "under_extrusion")

# Valid values for PrinterConfig.camera_source: the printer's built-in
# camera, or a USB webcam. Anything else is dropped back to the default.
CAMERA_SOURCES = ("a1", "webcam")

# Bambu's own model ids, as they appear in a sliced .gcode.3mf at
# Metadata/slice_info.config -> <metadata key="printer_model_id" value="..."/>.
# Used only to show a friendly name; the mismatch check compares raw ids, so
# an id missing from this table still works, it just displays as itself.
#
# VERIFIED here: "N2S" = A1. This repo's printer is serial 03919D531805572 and
# a file sliced for it in Bambu Studio carries printer_model_id N2S
# (2026-07-21). Everything else below is COMMUNITY KNOWLEDGE and has not been
# confirmed against hardware -- correct them as they are observed, and do not
# let an unverified entry become the reason a print is refused.
MODEL_NAMES = {
    "N2S": "A1",            # verified 2026-07-21
    "N1": "A1 mini",        # unverified
    "C11": "P1P",           # unverified
    "C12": "P1S",           # unverified
    "BL-P001": "X1 Carbon",  # unverified
    "BL-P002": "X1",        # unverified
}

# Serial-number prefix -> model id, used only to PREFILL the dropdown on the
# Add/Edit printer form. Same verification caveat as above: 039 is confirmed,
# 030 is inferred from the A1 mini this project previously used.
SERIAL_PREFIX_MODELS = {
    "039": "N2S",           # verified 2026-07-21
    "030": "N1",            # unverified
}

# Nozzle diameters Bambu ships machine profiles for. Needed because slicer
# machine profiles are per-nozzle ("Bambu Lab A1 0.4 nozzle"), and the printer
# does not report its nozzle over MQTT any more than it reports its model --
# so, like model_id, this can only ever be configured.
NOZZLES = ("0.2", "0.4", "0.6", "0.8")
DEFAULT_NOZZLE = "0.4"


def model_name(model_id: str) -> str:
    """Friendly name for a model id, or the id itself when unknown. Never
    raises and never returns None, so it is safe to drop straight into a
    user-facing message."""
    if not model_id:
        return ""
    return MODEL_NAMES.get(model_id, model_id)


def model_mismatch(printer_model_id, file_model_id) -> str | None:
    """-> a user-facing explanation when a file is confidently sliced for a
    different printer, else None.

    UNKNOWN NEVER BLOCKS. If either side is empty/None the answer is None.
    That is the whole safety property of this feature: the printer never
    reports its own model, so an unset model is normal, and every raw .gcode
    lacks one too. Only a *confirmed* difference between two known ids is
    allowed to refuse anything.
    """
    if not printer_model_id or not file_model_id:
        return None
    if printer_model_id == file_model_id:
        return None
    return (f"sliced for {model_name(file_model_id)}, "
            f"but this printer is {model_name(printer_model_id)}")


def guess_model_id(serial) -> str:
    """Serial -> a model id, or "" when the prefix isn't recognised.

    Only ever a PREFILL for the form. "" means unknown, and unknown must never
    block a print -- a confidently wrong guess here would refuse a file that
    is actually fine, which is worse than not guessing at all.
    """
    if not isinstance(serial, str):
        return ""
    for prefix, model_id in SERIAL_PREFIX_MODELS.items():
        if serial.startswith(prefix):
            return model_id
    return ""


def normalize_roi(value):
    """Anything -> a valid [x, y, w, h] fraction list, or None.

    Same rule as detect.parse_roi and deliberately so: a malformed ROI must
    degrade to "whole frame" rather than crash the server or, worse, silently
    crop the print out of view. Kept here (not imported from detect) so the
    server never has to import cv2.
    """
    if not isinstance(value, (list, tuple)) or len(value) != 4:
        return None
    try:
        x, y, w, h = (float(v) for v in value)
    except (TypeError, ValueError):
        return None
    if w <= 0 or h <= 0 or x < 0 or y < 0 or x + w > 1 or y + h > 1:
        return None
    return [x, y, w, h]


@dataclasses.dataclass
class PrinterConfig:
    """One registered printer. `name` falls back to the host."""

    serial: str
    host: str
    access_code: str
    name: str = ""
    capture: bool = False
    camera_source: str = "a1"
    camera_index: int = 0
    conf: float = 0.25
    armed_classes: list = dataclasses.field(
        default_factory=lambda: ["spaghetti"])
    detect_enabled: bool = False
    # Bed region to run detection on, as [x, y, w, h] fractions of the frame,
    # or None for the whole frame. Fractions so it survives a resolution
    # change. On the A1's wide, low view the frame is mostly room, and the
    # model finds "failures" in furniture -- this is what excludes it.
    roi: list | None = None
    # Bambu model id (e.g. "N2S" = A1), used to refuse a .gcode.3mf sliced for
    # a different printer. "" means unknown, which never blocks anything --
    # the printer does not report its own model over MQTT (all 64 published
    # keys were checked, 2026-07-21), so this can only ever be configured.
    model_id: str = ""
    # Installed nozzle diameter as a string ("0.4"), matching the profile
    # names exactly. Degrades to DEFAULT_NOZZLE rather than raising: a wrong
    # nozzle slices for the wrong hardware, so an unparseable value must land
    # on the common case, not refuse to load the printer.
    nozzle: str = DEFAULT_NOZZLE

    def __post_init__(self) -> None:
        self.serial = (self.serial or "").strip()
        self.host = (self.host or "").strip()
        self.access_code = (self.access_code or "").strip()
        self.name = (self.name or "").strip() or self.host

    def to_dict(self) -> dict:
        return dataclasses.asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "PrinterConfig":
        # Validate before __post_init__ strips: (x or "").strip() tolerates
        # None/0/False by coercing them to "", so a falsy wrong-typed serial
        # would otherwise be silently *accepted* as "" instead of rejected --
        # and the registry keys on serial, so several such entries would
        # collapse onto one printer. Reject non-string required fields
        # outright instead of letting __post_init__ explode or coerce.
        for k in ("serial", "host", "access_code"):
            if not isinstance(d[k], str):  # KeyError if absent, TypeError if
                raise TypeError(            # `d` itself isn't a dict
                    f"{k} must be a string, got {type(d[k]).__name__}")
        name = d.get("name", "")
        if not isinstance(name, str):
            name = ""  # cosmetic field: wrong type -> treat as absent
        capture = d.get("capture", False)
        if not isinstance(capture, bool):
            # bool(...) would turn the string "false" into True. capture
            # gates single-occupancy webcam access, so a bad value must
            # default to the safe direction (False), not to whatever
            # truthiness the wrong type happens to have.
            log.warning("capture must be true/false, got %r; defaulting to "
                        "False", capture)
            capture = False
        camera_index = d.get("camera_index", 0)
        if not isinstance(camera_index, int) or isinstance(camera_index, bool):
            camera_index = 0
        conf = d.get("conf", 0.25)
        if not isinstance(conf, (int, float)) or isinstance(conf, bool):
            conf = 0.25
        conf = min(1.0, max(0.0, float(conf)))
        detect_enabled = d.get("detect_enabled", False)
        if not isinstance(detect_enabled, bool):
            detect_enabled = False
        raw_classes = d.get("armed_classes", ["spaghetti"])
        if not isinstance(raw_classes, list):
            raw_classes = ["spaghetti"]
        armed_classes = [c for c in raw_classes if c in DETECTION_CLASSES] \
            or ["spaghetti"]
        camera_source = d.get("camera_source", "a1")
        if camera_source not in CAMERA_SOURCES:
            camera_source = "a1"
        # Not validated against MODEL_NAMES on purpose: that table is not
        # exhaustive, and dropping an id it hasn't heard of would silently
        # disable the check for a printer the user configured correctly.
        model_id = d.get("model_id", "")
        if not isinstance(model_id, str):
            model_id = ""
        model_id = model_id.strip()
        nozzle = d.get("nozzle", DEFAULT_NOZZLE)
        if nozzle not in NOZZLES:  # covers wrong type, "0.5", "", None
            nozzle = DEFAULT_NOZZLE
        return cls(serial=d["serial"], host=d["host"],
                   access_code=d["access_code"], name=name, capture=capture,
                   camera_source=camera_source, camera_index=camera_index,
                   conf=conf, armed_classes=armed_classes,
                   detect_enabled=detect_enabled, roi=normalize_roi(d.get("roi")),
                   model_id=model_id, nozzle=nozzle)


class PrinterStore:
    """printers.json on disk. Never raises on read -- a corrupt file must not
    stop the server from booting; you would have no UI left to fix it with."""

    def __init__(self, path: pathlib.Path):
        self.path = pathlib.Path(path)

    def load(self) -> list[PrinterConfig]:
        try:
            # utf-8-sig, not utf-8: Windows editors (Notepad and friends) add
            # a BOM on save, and a BOM'd file is otherwise-good JSON that
            # would silently read as "no printers" under plain utf-8.
            # utf-8-sig strips the BOM when present and is byte-for-byte
            # identical to utf-8 when absent -- a real UTF-16 file still
            # fails to decode and still returns [] with a warning, which is
            # correct since it genuinely isn't UTF-8.
            raw = json.loads(self.path.read_text(encoding="utf-8-sig"))
        except FileNotFoundError:
            return []
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as e:
            # UnicodeDecodeError is a ValueError subclass, not an OSError, so
            # it needs naming here explicitly. Hand-editing printers.json
            # from a PowerShell prompt (Out-File/Set-Content default to
            # UTF-16 LE with a BOM) is enough to trigger it.
            log.warning("%s is unreadable (%s); starting with no printers",
                        self.path, e)
            return []
        if not isinstance(raw, list):
            log.warning("%s is not a JSON list; starting with no printers",
                        self.path)
            return []
        out = []
        for entry in raw:
            try:
                out.append(PrinterConfig.from_dict(entry))
            except (KeyError, TypeError) as e:
                # KeyError: a required key is absent. TypeError: `entry`
                # isn't a dict, or a required field isn't a string --
                # from_dict validates and raises explicitly rather than
                # relying on __post_init__ to explode (or, worse, silently
                # coerce a falsy wrong-typed value like None/0/False into
                # ""). AttributeError is deliberately NOT caught here: it
                # would now indicate a genuine bug elsewhere in from_dict/
                # __post_init__, and swallowing it would silently drop every
                # printer in the file instead of surfacing the bug.
                log.warning("skipping malformed entry in %s: %s", self.path, e)
        return out

    def save(self, configs: list[PrinterConfig]) -> None:
        """Atomic and durable: os.replace() makes the swap atomic (a process
        crash mid-write can't leave a truncated printers.json), and the
        flush()+fsync() below make sure the data is actually on disk before
        that swap lands (a power-loss right after replace can't leave a
        zero-length one either). Either failure mode would otherwise read
        back as "every printer silently vanished", since load() is
        deliberately tolerant of a bad file."""
        self.path.parent.mkdir(parents=True, exist_ok=True)
        data = json.dumps([c.to_dict() for c in configs], indent=2)
        # prefix=self.path.name (not the default "tmp") is load-bearing: it
        # is what makes this temp file start with "printers.json" and fall
        # under .gitignore's "printers.json*" rule by construction, so an
        # interrupted write here (Ctrl-C, power loss, task kill) can never
        # leak the access code past .gitignore the way a generic
        # tmpXXXXXX.tmp would.
        fd, tmp = tempfile.mkstemp(dir=str(self.path.parent),
                                   prefix=self.path.name, suffix=".tmp")
        try:
            try:
                f = os.fdopen(fd, "w", encoding="utf-8")
            except BaseException:
                os.close(fd)  # fdopen failed -- the fd is still ours to
                raise         # close, or it leaks and later masks this error
            with f:
                f.write(data)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp, self.path)
        except BaseException:
            pathlib.Path(tmp).unlink(missing_ok=True)
            raise


class MemoryStore:
    """Same interface, no disk. Used by --mock so fake printers never land in
    the user's real printers.json."""

    def __init__(self) -> None:
        self._configs: list[PrinterConfig] = []

    def load(self) -> list[PrinterConfig]:
        return list(self._configs)

    def save(self, configs: list[PrinterConfig]) -> None:
        self._configs = list(configs)
