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


@dataclasses.dataclass
class PrinterConfig:
    """One registered printer. `name` falls back to the host."""

    serial: str
    host: str
    access_code: str
    name: str = ""
    capture: bool = False
    camera_index: int = 0
    conf: float = 0.25
    armed_classes: list = dataclasses.field(
        default_factory=lambda: ["spaghetti"])
    detect_enabled: bool = False

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
        return cls(serial=d["serial"], host=d["host"],
                   access_code=d["access_code"], name=name, capture=capture,
                   camera_index=camera_index, conf=conf,
                   armed_classes=armed_classes, detect_enabled=detect_enabled)


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
