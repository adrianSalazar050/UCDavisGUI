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


@dataclasses.dataclass
class PrinterConfig:
    """One registered printer. `name` falls back to the host."""

    serial: str
    host: str
    access_code: str
    name: str = ""
    capture: bool = False

    def __post_init__(self) -> None:
        self.serial = (self.serial or "").strip()
        self.host = (self.host or "").strip()
        self.access_code = (self.access_code or "").strip()
        self.name = (self.name or "").strip() or self.host

    def to_dict(self) -> dict:
        return dataclasses.asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "PrinterConfig":
        return cls(
            serial=d["serial"], host=d["host"], access_code=d["access_code"],
            name=d.get("name", ""), capture=bool(d.get("capture", False)))


class PrinterStore:
    """printers.json on disk. Never raises on read -- a corrupt file must not
    stop the server from booting; you would have no UI left to fix it with."""

    def __init__(self, path: pathlib.Path):
        self.path = pathlib.Path(path)

    def load(self) -> list[PrinterConfig]:
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return []
        except (OSError, json.JSONDecodeError) as e:
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
            except (KeyError, TypeError, AttributeError) as e:
                # KeyError: a required key is absent. TypeError: `entry` isn't
                # a dict at all. AttributeError: a required key is present but
                # holds a non-string value, so __post_init__'s .strip() call
                # blows up -- a wrong-typed field is just another shape of
                # malformed entry, and gets the same treatment: skip it and
                # log, rather than str()-coercing it into a subtly wrong
                # config that goes on to reach the MQTT layer.
                log.warning("skipping malformed entry in %s: %s", self.path, e)
        return out

    def save(self, configs: list[PrinterConfig]) -> None:
        """Atomic: a crash mid-write must not leave a truncated file that
        bricks the next startup."""
        self.path.parent.mkdir(parents=True, exist_ok=True)
        data = json.dumps([c.to_dict() for c in configs], indent=2)
        fd, tmp = tempfile.mkstemp(dir=str(self.path.parent), suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                f.write(data)
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
