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

from .store import DEFAULT_BED_TYPE

log = logging.getLogger("server.slicer")


class SliceError(Exception):
    """Anything that went wrong resolving profiles or running the slicer."""


class SlicerNotFound(SliceError):
    """No slicer executable on this machine.

    Not raised anywhere in this module -- find_slicer() returns None instead
    (see its docstring for why). This is here for a CALLER to raise: a route
    turning "no slicer installed" into a 404, the same "None means inert"
    convention queue=None and detection=None already use.
    """


# Bookkeeping keys carried on a profile for identity, not print settings. When
# we pull the fields out of an *included* template (below) we take its gcode and
# config but not these, so a template called "...template machine_start_gcode"
# can't shadow the including profile's own name/type.
_INCLUDE_METADATA = frozenset((
    "name", "type", "from", "setting_id", "instantiation", "include",
))


def flatten_profile(name: str, index: dict) -> dict:
    """Resolve a vendor profile's `inherits` chain AND its `include` templates
    into a self-contained dict.

    Vendor profiles are PARTIALS. "Bambu Lab A1 0.4 nozzle" carries 39 keys and
    inherits the other ~70; handing that straight to --load-settings fails
    validation. Child keys win over parent keys.

    On top of that, Bambu splits a machine's large gcode blocks -- start, end,
    layer-change, timelapse, change-filament -- into separate "template"
    profiles pulled in via an `include` list, and the main profile does NOT
    define those fields itself. Resolving only `inherits` therefore drops every
    one of them and falls back to fdm_machine_common's GENERIC start gcode,
    which hardcodes M109 S205 and omits the A1's real bed-mesh and first-layer
    init. Measured 2026-07-23: that sliced PLA at 205 C instead of the
    filament's 220 C and produced a print that ran a single layer and then
    halted. So `include` is not optional -- it carries the machine's actual
    gcode. Included keys override inherited ones (they ARE the real gcode) and
    are in turn overridden by the profile's own keys.

    Pure: dict-of-dicts in, dict out, so it tests without a slicer installed.
    """
    return _flatten(name, index, ())


def _flatten(name: str, index: dict, seen: tuple) -> dict:
    if name in seen:
        raise SliceError(
            f"cycle in slicer profiles: {' -> '.join(seen + (name,))}")
    try:
        node = index[name]
    except KeyError:
        raise SliceError(f"unknown slicer profile: {name!r}") from None
    parent = node.get("inherits")
    out = dict(_flatten(parent, index, seen + (name,))) if parent else {}
    # Includes sit between inherited (lowest) and the node's own keys (highest).
    # A missing template raises rather than degrading silently: an unresolved
    # include drops the whole machine start gcode back to the generic fallback,
    # which is the exact dangerous wrong-file case this exists to prevent.
    includes = node.get("include") or []
    if isinstance(includes, str):
        includes = [includes]
    for inc in includes:
        included = _flatten(inc, index, seen + (name,))
        out.update({k: v for k, v in included.items()
                    if k not in _INCLUDE_METADATA})
    out.update({k: v for k, v in node.items()
                if k not in ("inherits", "include")})
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
            # setdefault, not []=: first one wins. rglob() yields paths in
            # sorted order, so this is a deterministic tie-break -- the same
            # tree always produces the same index, whichever duplicate a
            # future re-run of this code happens to see first.
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


# A pathological model can slice for a very long time. The subprocess is
# killed at this point and the job fails normally rather than pinning a core
# forever.
SLICE_TIMEOUT_S = 900.0

# The CLI always names the gcode plate_1.gcode inside out_dir, so every job
# gets its own directory and two slices can never collide.
OUTPUT_NAME = "sliced.gcode.3mf"


def run_slice(exe, model_path, machine: dict, process: dict, filament: dict,
              out_dir, *, supports: bool = False,
              bed_type: str = DEFAULT_BED_TYPE,
              timeout_s: float = SLICE_TIMEOUT_S,
              runner=subprocess.run) -> pathlib.Path:
    """Slice `model_path` into out_dir/sliced.gcode.3mf and return its path.

    `machine`/`process`/`filament` are already-FLATTENED configs (see
    flatten_profile). `runner` is injectable so the whole path tests without a
    slicer installed.

    Always raises SliceError on failure, and the message carries the slicer's
    own stderr, because that is the only thing that explains a bad model.
    """
    out_dir = pathlib.Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    process = dict(process)  # never mutate the caller's cached profile
    process["enable_support"] = "1" if supports else "0"
    # Pinned rather than assumed: the A1 vendor profile already defaults to
    # tree(auto), but a future profile might not, and the UI promises trees.
    process["support_type"] = "tree(auto)"
    # MEASURED 2026-07-22: curr_bed_type left unset defaults to Cool Plate in
    # Bambu Studio, whose PLA temp is 35 C (cool_plate_temp in the flattened
    # filament profile). This lab's A1 has a Textured PEI Plate, which needs
    # 65 C (textured_plate_temp) -- PLA does not adhere at 35 C. A CLI-sliced
    # cube with this key unset heated the nozzle correctly (205 C), reached
    # layer 2/100, then stalled at 5% with an HMS warning: a failed first
    # layer from a cold bed. The gcode carried `M190 S35`. Slicing the same
    # cube twice confirmed the fix: curr_bed_type='Cool Plate' -> M190 S35,
    # curr_bed_type='Textured PEI Plate' -> M190 S65. Defaulting the
    # parameter itself to DEFAULT_BED_TYPE (not leaving the key unset) is
    # deliberate: a caller that forgets to pass bed_type must still get the
    # plate that's actually installed, not reproduce this exact defect.
    process["curr_bed_type"] = bed_type

    paths = {}
    for kind, cfg in (("machine", machine), ("process", process),
                      ("filament", filament)):
        p = out_dir / f"{kind}.json"
        p.write_text(json.dumps(cfg), encoding="utf-8")
        paths[kind] = p

    argv = build_argv(exe, model_path, paths["machine"], paths["process"],
                      paths["filament"], OUTPUT_NAME, out_dir)
    try:
        proc = runner(argv, capture_output=True, text=True, timeout=timeout_s)
    except subprocess.TimeoutExpired:
        raise SliceError(
            f"slicing timed out after {timeout_s:.0f}s") from None
    except OSError as e:
        raise SliceError(f"could not run the slicer: {e}") from None

    produced = out_dir / OUTPUT_NAME
    # Bambu Studio exits 0 on success -- VERIFIED on this machine 2026-07-22.
    # So a nonzero code is a real failure even when a file is on disk: the
    # likeliest way that happens is a crash part-way through export, leaving
    # a TRUNCATED .gcode.3mf. Those bytes get uploaded to a printer's microSD
    # and queued, so "there is a file" must never be enough on its own.
    # (OrcaSlicer does exit nonzero on success -- but Orca is not our engine,
    # see the module docstring.)
    if proc.returncode != 0:
        raise SliceError(_tail(proc.stderr) or
                         f"slicer exited {proc.returncode}")
    if not produced.exists():
        # OrcaSlicer's exact failure mode. Never let it read as success.
        raise SliceError("the slicer produced no .gcode.3mf")
    return produced


def _tail(text: str, limit: int = 2000) -> str:
    """Last `limit` characters of the slicer's stderr. Bounded because it is
    surfaced in a job record and polled by the browser."""
    text = (text or "").strip()
    return text[-limit:]
