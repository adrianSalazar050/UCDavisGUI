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


# Where Bambu Studio installs itself on Windows. Checked in order, after the
# BAMBU_STUDIO_EXE override.
DEFAULT_SLICER_PATHS = (
    r"C:\Program Files\Bambu Studio\bambu-studio.exe",
    r"C:\Program Files (x86)\Bambu Studio\bambu-studio.exe",
)

# Vendor whose profiles we index, relative to the install directory.
PROFILES_SUBPATH = ("resources", "profiles", "BBL")


def find_slicer(env=None, candidates=DEFAULT_SLICER_PATHS) -> str | None:
    """Path to bambu-studio.exe, or None when it isn't installed.

    None is a supported outcome, not an error: create_app turns it into 404s
    on every slice route, the same "None means inert" convention queue=None
    and detection=None already use. A machine with no slicer still boots,
    still monitors, still prints files already on the card.
    """
    env = os.environ if env is None else env
    override = (env.get("BAMBU_STUDIO_EXE") or "").strip()
    # An override that no longer exists must not shadow a good install --
    # otherwise a stale env var silently disables the whole feature.
    if override and os.path.exists(override):
        return override
    for path in candidates:
        if os.path.exists(path):
            return path
    return None


def profiles_root(exe: str) -> pathlib.Path:
    """The vendor profile directory shipped beside `exe`.

    Deliberately the INSTALLED resources tree, not the OTA-updated copy under
    %APPDATA%: the two can differ, and this is the one the slicer itself
    validated against.
    """
    return pathlib.Path(exe).parent.joinpath(*PROFILES_SUBPATH)


def build_argv(exe, model_path, machine_json, process_json, filament_json,
               out_name, out_dir) -> list:
    """The invocation verified by hand on 2026-07-22.

    --outputdir is MANDATORY: without it the output lands nowhere findable.
    The model path comes first, before any option, which is the ordering that
    was verified to work.
    """
    return [
        str(exe), str(model_path),
        "--load-settings", f"{machine_json};{process_json}",
        "--load-filaments", str(filament_json),
        "--slice", "0",
        "--export-3mf", str(out_name),
        "--outputdir", str(out_dir),
    ]
