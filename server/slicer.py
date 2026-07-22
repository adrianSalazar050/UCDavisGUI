"""Driving the Bambu Studio CLI to slice a model into a startable .gcode.3mf.

THE ENGINE IS BAMBU STUDIO, NOT ORCASLICER, and that is not a preference.
Measured 2026-07-22: OrcaSlicer slices fine but --export-3mf never produced a
file across five argument orderings, and a raw .gcode cannot be started over
MQTT (master.md 5.4 -- project_file points at Metadata/plate_N.gcode *inside*
the zip). Orca also needs use_relative_e_distances patched or it refuses to
slice at all. Do not "simplify" this back to Orca.

Kept free of FastAPI and threads so it is testable against a tmp_path; the
job lifecycle lives in slicejobs.py.
"""
from __future__ import annotations

import json
import logging
import os
import pathlib
import subprocess

log = logging.getLogger("server.slicer")


class SliceError(Exception):
    """Anything that went wrong resolving profiles or running the slicer."""


class SlicerNotFound(SliceError):
    """No slicer executable on this machine."""


def flatten_profile(name: str, index: dict) -> dict:
    """Resolve a vendor profile's `inherits` chain into a self-contained dict.

    Vendor profiles are PARTIALS. "Bambu Lab A1 0.4 nozzle" carries 39 keys and
    inherits the other ~70; handing that straight to --load-settings fails
    validation. Child keys win over parent keys.

    Pure: dict-of-dicts in, dict out, so it tests without a slicer installed.
    """
    return _flatten(name, index, ())


def _flatten(name: str, index: dict, seen: tuple) -> dict:
    if name in seen:
        raise SliceError(
            f"inheritance cycle in slicer profiles: {' -> '.join(seen + (name,))}")
    try:
        node = index[name]
    except KeyError:
        raise SliceError(f"unknown slicer profile: {name!r}") from None
    parent = node.get("inherits")
    out = dict(_flatten(parent, index, seen + (name,))) if parent else {}
    out.update({k: v for k, v in node.items() if k != "inherits"})
    return out


class ProfileIndex:
    """The vendor profile tree, keyed by each profile's `name` FIELD.

    Not by filename -- they differ often enough that keying on the filename
    silently loses profiles.
    """

    @staticmethod
    def load(root) -> dict:
        """Index every *.json under `root`. Never raises: a malformed vendor
        file must degrade to "that profile is unavailable" (the options route
        filters unresolvable presets out) rather than break the server."""
        root = pathlib.Path(root)
        index: dict = {}
        if not root.is_dir():
            log.warning("slicer profile directory %s does not exist", root)
            return index
        for path in sorted(root.rglob("*.json")):
            try:
                data = json.loads(path.read_text(encoding="utf-8-sig"))
            except (OSError, UnicodeDecodeError, json.JSONDecodeError) as e:
                log.debug("skipping unreadable profile %s: %s", path, e)
                continue
            if not isinstance(data, dict):
                continue
            name = data.get("name")
            # setdefault, not []=: first one wins, so a later duplicate can
            # never clobber a profile something already resolved against.
            if isinstance(name, str) and name:
                index.setdefault(name, data)
        return index
