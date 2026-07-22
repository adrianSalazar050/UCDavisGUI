# Automatic Slicing Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

> **STATUS: PROPOSED (2026-07-22).** Not started. Implements
> `docs/superpowers/specs/2026-07-22-auto-slicing-design.md`.
> Historical record once executed, not maintained. **`master.md` is
> authoritative wherever this file disagrees with it.**

**Goal:** Upload an STL in the dashboard, pick a printer, and get a sliced
`.gcode.3mf` uploaded to that printer's microSD and queued — with the filament
detected from MQTT, a curated quality preset, and a tree-support toggle.

**Architecture:** Three new server modules. `slicer.py` locates Bambu Studio,
flattens vendor profile `inherits` chains, and runs the CLI. `slicepresets.py`
holds the curated quality tiers and the filament map. `slicejobs.py` runs a
single global worker thread that slices, then reuses the *existing*
`registry.upload_sd_file` and `queue.add`. Everything impure is injectable, so
the whole suite runs with no slicer, printer, or camera.

**Tech Stack:** Python 3.11, FastAPI, pytest. Bambu Studio CLI
(`bambu-studio.exe`). React 19 + Vite on the frontend.

**Read first:** the spec's §2 (measured feasibility). Three findings there are
load-bearing and non-obvious: the engine must be Bambu Studio (OrcaSlicer never
emits a `.gcode.3mf`), vendor profiles are `inherits` partials, and preset names
cannot be a literal table.

---

## File Structure

| File | Responsibility |
|---|---|
| Create `server/slicer.py` | Find the slicer, index + flatten vendor profiles, build argv, run the subprocess |
| Create `server/slicepresets.py` | Quality tiers, model→token maps, filament detection + mapping |
| Create `server/slicejobs.py` | `SliceJob` records, the job store, the worker coordinator |
| Modify `server/store.py` | Add `PrinterConfig.nozzle` + validation |
| Modify `server/registry.py` | Add `printer_nozzle(serial)` accessor |
| Modify `server/main.py` | Four slice routes; `create_app(..., slicer=None)` |
| Modify `server/__main__.py` | Wire the coordinator, `--no-slicer` flag |
| Create `server/tests/test_slicer.py` | Flatten, index, argv, run_slice |
| Create `server/tests/test_slicepresets.py` | Tier resolution, filament detection |
| Create `server/tests/test_slicejobs.py` | Job state machine against a fake `run` |
| Modify `server/tests/test_store.py` | Nozzle validation |
| Modify `server/tests/test_api.py` | Slice routes, 404-when-inert |
| Create `frontend/src/pages/Slice.jsx` | The page |
| Create `frontend/src/components/slice/SliceForm.jsx` | Drop zone, preset, filament, supports |
| Create `frontend/src/components/slice/SliceJobList.jsx` | Polled job list |
| Modify `frontend/src/api/printer.js` | Four fetch wrappers |
| Modify `frontend/src/app/pageRegistry.jsx` | Register the page |
| Modify `master.md` | Document the subsystem |

---

## Task 1: `PrinterConfig.nozzle`

Preset resolution needs the nozzle, and the printer never reports it — same
situation as `model_id` (master.md §5.3), so it is configured.

**Files:**
- Modify: `server/store.py`
- Modify: `server/registry.py`
- Test: `server/tests/test_store.py`

- [ ] **Step 1: Write the failing tests**

Append to `server/tests/test_store.py`:

```python
def test_nozzle_defaults_to_04():
    c = PrinterConfig(serial="s", host="h", access_code="a")
    assert c.nozzle == "0.4"


def test_nozzle_accepts_the_four_known_sizes():
    for n in ("0.2", "0.4", "0.6", "0.8"):
        c = PrinterConfig.from_dict(
            {"serial": "s", "host": "h", "access_code": "a", "nozzle": n})
        assert c.nozzle == n


def test_nozzle_degrades_to_04_on_junk():
    # Same rule as normalize_roi: a bad value degrades to the safe default
    # rather than raising. A wrong nozzle slices for the wrong hardware, so
    # the default has to be the common case, not the last thing typed.
    for bad in ("0.5", "", None, 0.4, ["0.4"], "abc"):
        c = PrinterConfig.from_dict(
            {"serial": "s", "host": "h", "access_code": "a", "nozzle": bad})
        assert c.nozzle == "0.4"


def test_nozzle_survives_a_save_load_round_trip(tmp_path):
    store = PrinterStore(tmp_path / "printers.json")
    store.save([PrinterConfig(serial="s", host="h", access_code="a",
                              nozzle="0.6")])
    assert store.load()[0].nozzle == "0.6"
```

- [ ] **Step 2: Run them and watch them fail**

Run: `python -m pytest server/tests/test_store.py -q -k nozzle`
Expected: FAIL — `TypeError: PrinterConfig.__init__() got an unexpected keyword argument 'nozzle'` / `AttributeError: 'PrinterConfig' object has no attribute 'nozzle'`.

- [ ] **Step 3: Add the field**

In `server/store.py`, after the `MODEL_NAMES` block add:

```python
# Nozzle diameters Bambu ships machine profiles for. Needed because slicer
# machine profiles are per-nozzle ("Bambu Lab A1 0.4 nozzle"), and the printer
# does not report its nozzle over MQTT any more than it reports its model --
# so, like model_id, this can only ever be configured.
NOZZLES = ("0.2", "0.4", "0.6", "0.8")
DEFAULT_NOZZLE = "0.4"
```

Add the field to the dataclass, directly after `model_id`:

```python
    # Installed nozzle diameter as a string ("0.4"), matching the profile
    # names exactly. Degrades to DEFAULT_NOZZLE rather than raising: a wrong
    # nozzle slices for the wrong hardware, so an unparseable value must land
    # on the common case, not refuse to load the printer.
    nozzle: str = DEFAULT_NOZZLE
```

In `from_dict`, before the `return cls(...)`:

```python
        nozzle = d.get("nozzle", DEFAULT_NOZZLE)
        if nozzle not in NOZZLES:  # covers wrong type, "0.5", "", None
            nozzle = DEFAULT_NOZZLE
```

and add `nozzle=nozzle` to the `return cls(...)` call.

- [ ] **Step 4: Run the tests**

Run: `python -m pytest server/tests/test_store.py -q`
Expected: PASS, all of them.

- [ ] **Step 5: Add the registry accessor**

In `server/registry.py`, directly after `printer_model`:

```python
    def printer_nozzle(self, serial: str) -> str:
        """`serial`'s configured nozzle diameter, or the default when the
        printer is unknown. Never "" -- callers substitute it straight into a
        profile name, and an empty one would silently build a name that
        matches nothing."""
        with self._lock:
            cfg = self._configs.get(serial)
            return cfg.nozzle if cfg is not None else store.DEFAULT_NOZZLE
```

Confirm `server/registry.py` already imports `store`; if it imports names
directly, add `DEFAULT_NOZZLE` to that import and drop the `store.` prefix.

- [ ] **Step 6: Run the full suite and commit**

Run: `python -m pytest -q`
Expected: PASS (no existing test constructs `PrinterConfig` positionally past `model_id`, so the new trailing field is safe).

```bash
git add server/store.py server/registry.py server/tests/test_store.py
git commit -m "feat: PrinterConfig.nozzle, needed to resolve slicer machine profiles"
```

---

## Task 2: Profile flattening

The load-bearing discovery from the spike: vendor profiles are `inherits`
partials and fail validation if passed raw.

**Files:**
- Create: `server/slicer.py`
- Test: `server/tests/test_slicer.py`

- [ ] **Step 1: Write the failing tests**

Create `server/tests/test_slicer.py`:

```python
import json

import pytest

from server.slicer import ProfileIndex, SliceError, flatten_profile


def test_flatten_merges_parent_then_child():
    index = {
        "base": {"name": "base", "layer_height": "0.2", "walls": "2"},
        "kid": {"name": "kid", "inherits": "base", "walls": "3"},
    }
    assert flatten_profile("kid", index) == {
        "name": "kid", "layer_height": "0.2", "walls": "3"}


def test_flatten_drops_the_inherits_key_itself():
    # The slicer rejects a config that still names a parent it can't resolve.
    index = {"base": {"name": "base", "a": "1"},
             "kid": {"name": "kid", "inherits": "base"}}
    assert "inherits" not in flatten_profile("kid", index)


def test_flatten_walks_a_multi_level_chain():
    index = {
        "g": {"name": "g", "a": "1", "b": "1"},
        "p": {"name": "p", "inherits": "g", "b": "2", "c": "2"},
        "k": {"name": "k", "inherits": "p", "c": "3"},
    }
    assert flatten_profile("k", index) == {
        "name": "k", "a": "1", "b": "2", "c": "3"}


def test_flatten_raises_on_an_unknown_name():
    with pytest.raises(SliceError, match="nope"):
        flatten_profile("nope", {})


def test_flatten_raises_on_a_missing_parent():
    with pytest.raises(SliceError, match="missing"):
        flatten_profile("kid", {"kid": {"name": "kid", "inherits": "missing"}})


def test_flatten_raises_on_an_inheritance_cycle():
    # Must not recurse until the stack blows: a vendor tree that ships a cycle
    # would otherwise take the whole server down on a RecursionError.
    index = {"a": {"name": "a", "inherits": "b"},
             "b": {"name": "b", "inherits": "a"}}
    with pytest.raises(SliceError, match="cycle"):
        flatten_profile("a", index)


def test_profile_index_keys_on_the_name_field_not_the_filename(tmp_path):
    # Verified on the real tree: name and filename are not always the same.
    (tmp_path / "machine").mkdir()
    (tmp_path / "machine" / "whatever.json").write_text(
        json.dumps({"name": "Bambu Lab A1 0.4 nozzle", "a": "1"}),
        encoding="utf-8")
    index = ProfileIndex.load(tmp_path)
    assert "Bambu Lab A1 0.4 nozzle" in index
    assert "whatever" not in index


def test_profile_index_skips_unreadable_and_nameless_files(tmp_path):
    (tmp_path / "good.json").write_text(json.dumps({"name": "good"}),
                                        encoding="utf-8")
    (tmp_path / "broken.json").write_text("{not json", encoding="utf-8")
    (tmp_path / "nameless.json").write_text(json.dumps({"a": 1}),
                                            encoding="utf-8")
    index = ProfileIndex.load(tmp_path)
    assert list(index) == ["good"]


def test_profile_index_of_a_missing_directory_is_empty(tmp_path):
    assert ProfileIndex.load(tmp_path / "nope") == {}
```

- [ ] **Step 2: Run them and watch them fail**

Run: `python -m pytest server/tests/test_slicer.py -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'server.slicer'`.

- [ ] **Step 3: Write the module**

Create `server/slicer.py`:

```python
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
```

- [ ] **Step 4: Run the tests**

Run: `python -m pytest server/tests/test_slicer.py -q`
Expected: PASS, 9 tests.

- [ ] **Step 5: Commit**

```bash
git add server/slicer.py server/tests/test_slicer.py
git commit -m "feat(slicer): flatten vendor profile inherits chains"
```

---

## Task 3: Finding the slicer, and building argv

**Files:**
- Modify: `server/slicer.py`
- Test: `server/tests/test_slicer.py`

- [ ] **Step 1: Write the failing tests**

Append to `server/tests/test_slicer.py`:

```python
from server.slicer import build_argv, find_slicer, profiles_root


def test_find_slicer_prefers_the_env_override(tmp_path):
    exe = tmp_path / "custom.exe"
    exe.write_text("", encoding="utf-8")
    assert find_slicer({"BAMBU_STUDIO_EXE": str(exe)}) == str(exe)


def test_find_slicer_ignores_an_env_override_that_does_not_exist(tmp_path):
    # A stale env var must not shadow a perfectly good default install.
    default = tmp_path / "bambu-studio.exe"
    default.write_text("", encoding="utf-8")
    got = find_slicer({"BAMBU_STUDIO_EXE": str(tmp_path / "gone.exe")},
                      candidates=(str(default),))
    assert got == str(default)


def test_find_slicer_returns_none_when_nothing_is_installed(tmp_path):
    # None is the "feature is inert" signal -- the server must still boot.
    assert find_slicer({}, candidates=(str(tmp_path / "nope.exe"),)) is None


def test_profiles_root_is_beside_the_executable():
    got = profiles_root(r"C:\Program Files\Bambu Studio\bambu-studio.exe")
    assert got.as_posix().endswith("Bambu Studio/resources/profiles/BBL")


def test_build_argv_has_the_verified_shape():
    argv = build_argv("bs.exe", "model.stl", "m.json", "p.json", "f.json",
                      "out.gcode.3mf", "workdir")
    assert argv[0] == "bs.exe"
    assert argv[1] == "model.stl"          # model first, then options
    assert "--load-settings" in argv
    assert argv[argv.index("--load-settings") + 1] == "m.json;p.json"
    assert argv[argv.index("--load-filaments") + 1] == "f.json"
    assert argv[argv.index("--slice") + 1] == "0"
    assert argv[argv.index("--export-3mf") + 1] == "out.gcode.3mf"


def test_build_argv_always_passes_outputdir():
    # Measured 2026-07-22: without --outputdir the output lands nowhere
    # findable, so the slice "succeeds" and produces nothing.
    argv = build_argv("bs.exe", "m.stl", "m.json", "p.json", "f.json",
                      "o.gcode.3mf", "workdir")
    assert argv[argv.index("--outputdir") + 1] == "workdir"
```

- [ ] **Step 2: Run them and watch them fail**

Run: `python -m pytest server/tests/test_slicer.py -q -k "find_slicer or argv or profiles_root"`
Expected: FAIL — `ImportError: cannot import name 'build_argv'`.

- [ ] **Step 3: Implement**

Append to `server/slicer.py`:

```python
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
```

- [ ] **Step 4: Run the tests**

Run: `python -m pytest server/tests/test_slicer.py -q`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add server/slicer.py server/tests/test_slicer.py
git commit -m "feat(slicer): locate the slicer and build the verified argv"
```

---

## Task 4: `run_slice`

**Files:**
- Modify: `server/slicer.py`
- Test: `server/tests/test_slicer.py`

- [ ] **Step 1: Write the failing tests**

Append to `server/tests/test_slicer.py`:

```python
import subprocess

from server.slicer import run_slice

MACHINE = {"name": "m", "nozzle_diameter": ["0.4"]}
PROCESS = {"name": "p", "enable_support": "0"}
FILAMENT = {"name": "f"}


def _fake_runner(out_dir, name, *, returncode=0, stderr=""):
    """A subprocess.run stand-in that 'produces' the 3mf the real CLI would."""
    def run(argv, **kw):
        if returncode == 0:
            (pathlib.Path(out_dir) / name).write_bytes(b"PK\x03\x04fake")
        return subprocess.CompletedProcess(argv, returncode, "", stderr)
    return run


def test_run_slice_returns_the_produced_3mf(tmp_path):
    out = run_slice("bs.exe", tmp_path / "m.stl", MACHINE, PROCESS, FILAMENT,
                    tmp_path, runner=_fake_runner(tmp_path, "sliced.gcode.3mf"))
    assert out.name == "sliced.gcode.3mf"
    assert out.read_bytes() == b"PK\x03\x04fake"


def test_run_slice_writes_the_flattened_configs_next_to_the_output(tmp_path):
    run_slice("bs.exe", tmp_path / "m.stl", MACHINE, PROCESS, FILAMENT,
              tmp_path, runner=_fake_runner(tmp_path, "sliced.gcode.3mf"))
    assert json.loads((tmp_path / "machine.json").read_text(
        encoding="utf-8")) == MACHINE


def test_run_slice_patches_enable_support_on(tmp_path):
    # The whole supports feature is this one key; support_type is already
    # tree(auto) in the vendor profile.
    run_slice("bs.exe", tmp_path / "m.stl", MACHINE, PROCESS, FILAMENT,
              tmp_path, supports=True,
              runner=_fake_runner(tmp_path, "sliced.gcode.3mf"))
    written = json.loads((tmp_path / "process.json").read_text(encoding="utf-8"))
    assert written["enable_support"] == "1"
    assert written["support_type"] == "tree(auto)"
    assert PROCESS["enable_support"] == "0"   # caller's dict not mutated


def test_run_slice_leaves_supports_off_by_default(tmp_path):
    run_slice("bs.exe", tmp_path / "m.stl", MACHINE, PROCESS, FILAMENT,
              tmp_path, runner=_fake_runner(tmp_path, "sliced.gcode.3mf"))
    written = json.loads((tmp_path / "process.json").read_text(encoding="utf-8"))
    assert written["enable_support"] == "0"


def test_run_slice_raises_with_stderr_when_the_slicer_fails(tmp_path):
    runner = _fake_runner(tmp_path, "sliced.gcode.3mf", returncode=2,
                          stderr="got error when validate: boom")
    with pytest.raises(SliceError, match="boom"):
        run_slice("bs.exe", tmp_path / "m.stl", MACHINE, PROCESS, FILAMENT,
                  tmp_path, runner=runner)


def test_run_slice_raises_when_exit_0_but_no_file_appeared(tmp_path):
    # Exactly the OrcaSlicer failure mode: "success" with no 3mf. A silent
    # empty success would queue a job pointing at nothing.
    def runner(argv, **kw):
        return subprocess.CompletedProcess(argv, 0, "", "")
    with pytest.raises(SliceError, match="produced no"):
        run_slice("bs.exe", tmp_path / "m.stl", MACHINE, PROCESS, FILAMENT,
                  tmp_path, runner=runner)


def test_run_slice_turns_a_timeout_into_a_slice_error(tmp_path):
    def runner(argv, **kw):
        raise subprocess.TimeoutExpired(argv, 1.0)
    with pytest.raises(SliceError, match="timed out"):
        run_slice("bs.exe", tmp_path / "m.stl", MACHINE, PROCESS, FILAMENT,
                  tmp_path, runner=runner)
```

Add `import pathlib` to the test file's imports if it isn't there.

- [ ] **Step 2: Run them and watch them fail**

Run: `python -m pytest server/tests/test_slicer.py -q -k run_slice`
Expected: FAIL — `ImportError: cannot import name 'run_slice'`.

- [ ] **Step 3: Implement**

Append to `server/slicer.py`:

```python
# A pathological model can slice for a very long time. The subprocess is
# killed at this point and the job fails normally rather than pinning a core
# forever.
SLICE_TIMEOUT_S = 900.0

# The CLI always names the gcode plate_1.gcode inside out_dir, so every job
# gets its own directory and two slices can never collide.
OUTPUT_NAME = "sliced.gcode.3mf"


def run_slice(exe, model_path, machine: dict, process: dict, filament: dict,
              out_dir, *, supports: bool = False,
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
    if proc.returncode != 0 and not produced.exists():
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
```

- [ ] **Step 4: Run the tests**

Run: `python -m pytest server/tests/test_slicer.py -q`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add server/slicer.py server/tests/test_slicer.py
git commit -m "feat(slicer): run_slice with timeout, stderr capture, and an empty-output guard"
```

---

## Task 5: Quality tiers

The naming traps here were all measured; see the spec §4.3.

**Files:**
- Create: `server/slicepresets.py`
- Test: `server/tests/test_slicepresets.py`

- [ ] **Step 1: Write the failing tests**

Create `server/tests/test_slicepresets.py`:

```python
from server.slicepresets import (TIERS, machine_profile_name, resolve_preset,
                                 available_presets)

# A miniature stand-in for the real 1,932-preset index.
INDEX = {
    "Bambu Lab A1 0.4 nozzle": {"name": "Bambu Lab A1 0.4 nozzle"},
    "Bambu Lab A1 0.6 nozzle": {"name": "Bambu Lab A1 0.6 nozzle"},
    "Bambu Lab A1 mini 0.4 nozzle": {"name": "Bambu Lab A1 mini 0.4 nozzle"},
    "0.20mm Standard @BBL A1": {"name": "0.20mm Standard @BBL A1"},
    "0.16mm Optimal @BBL A1": {"name": "0.16mm Optimal @BBL A1"},
    "0.28mm Extra Draft @BBL A1": {"name": "0.28mm Extra Draft @BBL A1"},
    "0.30mm Standard @BBL A1 0.6 nozzle":
        {"name": "0.30mm Standard @BBL A1 0.6 nozzle"},
    "0.20mm Standard @BBL A1M": {"name": "0.20mm Standard @BBL A1M"},
}


def test_machine_name_uses_the_machine_token_and_always_has_the_suffix():
    assert machine_profile_name("N2S", "0.4") == "Bambu Lab A1 0.4 nozzle"
    assert machine_profile_name("N1", "0.4") == "Bambu Lab A1 mini 0.4 nozzle"


def test_resolve_preset_finds_the_standard_tier_for_an_a1():
    got = resolve_preset("standard", "N2S", "0.4", INDEX)
    assert got["process"] == "0.20mm Standard @BBL A1"
    assert got["label"] == "Standard 0.20 mm"


def test_the_mini_uses_the_a1m_process_token():
    # The mini is "A1 mini" in machine profiles but "A1M" in process ones.
    # Using one token for both silently resolves nothing.
    got = resolve_preset("standard", "N1", "0.4", INDEX)
    assert got["process"] == "0.20mm Standard @BBL A1M"


def test_the_nozzle_suffix_is_omitted_at_04_and_present_otherwise():
    assert resolve_preset("standard", "N2S", "0.4", INDEX)["process"] \
        == "0.20mm Standard @BBL A1"
    assert resolve_preset("standard", "N2S", "0.6", INDEX)["process"] \
        == "0.30mm Standard @BBL A1 0.6 nozzle"


def test_the_label_is_read_off_the_resolved_name_not_hardcoded():
    # "Standard" is 0.20mm on a 0.4 nozzle and 0.30mm on a 0.6. A hardcoded
    # label would show a layer height the printer is not using.
    assert resolve_preset("standard", "N2S", "0.6", INDEX)["label"] \
        == "Standard 0.30 mm"


def test_resolve_preset_returns_none_when_nothing_matches():
    assert resolve_preset("standard", "N2S", "0.8", INDEX) is None
    assert resolve_preset("nosuchtier", "N2S", "0.4", INDEX) is None
    assert resolve_preset("standard", "", "0.4", INDEX) is None


def test_available_presets_lists_only_what_resolves():
    got = available_presets("N2S", "0.4", INDEX)
    assert [p["id"] for p in got] == ["standard", "fine", "draft"]
    assert available_presets("N2S", "0.8", INDEX) == []


def test_every_tier_is_exercised_by_the_table():
    assert set(TIERS) == {"standard", "fine", "draft"}
```

- [ ] **Step 2: Run them and watch them fail**

Run: `python -m pytest server/tests/test_slicepresets.py -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'server.slicepresets'`.

- [ ] **Step 3: Implement**

Create `server/slicepresets.py`:

```python
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
```

- [ ] **Step 4: Run the tests**

Run: `python -m pytest server/tests/test_slicepresets.py -q`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add server/slicepresets.py server/tests/test_slicepresets.py
git commit -m "feat(slicer): curated quality tiers resolved against the profile index"
```

---

## Task 6: Filament detection and mapping

**Files:**
- Modify: `server/slicepresets.py`
- Test: `server/tests/test_slicepresets.py`

- [ ] **Step 1: Write the failing tests**

Append to `server/tests/test_slicepresets.py`:

```python
from server.slicepresets import (available_filaments, detect_loaded_filament,
                                 filament_profile_name)

FIL_INDEX = dict(INDEX, **{
    "Generic PLA @BBL A1": {"name": "Generic PLA @BBL A1"},
    "Generic PETG @BBL A1": {"name": "Generic PETG @BBL A1"},
    "Generic PLA @BBL A1M": {"name": "Generic PLA @BBL A1M"},
})


def test_detects_the_material_from_an_ams_tray():
    state = {"ams": {"ams": [{"tray": [{"tray_type": "PLA"}]}]}}
    assert detect_loaded_filament(state) == "PLA"


def test_falls_back_to_the_external_spool():
    # An A1 with no AMS reports vt_tray instead.
    assert detect_loaded_filament({"vt_tray": {"tray_type": "PETG"}}) == "PETG"


def test_prefers_a_loaded_ams_tray_over_an_empty_one():
    state = {"ams": {"ams": [{"tray": [{"tray_type": ""},
                                       {"tray_type": "ABS"}]}]}}
    assert detect_loaded_filament(state) == "ABS"


def test_returns_none_for_an_unidentifiable_spool():
    # The normal case for third-party filament with no RFID tag. Must not
    # block slicing -- the UI just leaves the dropdown on its default.
    assert detect_loaded_filament({}) is None
    assert detect_loaded_filament({"vt_tray": {"tray_type": ""}}) is None
    assert detect_loaded_filament({"ams": {"ams": []}}) is None
    assert detect_loaded_filament(None) is None


def test_tolerates_a_malformed_state_without_raising():
    # MQTT state is merged from partial reports; anything can be any shape.
    assert detect_loaded_filament({"ams": "nope"}) is None
    assert detect_loaded_filament({"ams": {"ams": [{"tray": "nope"}]}}) is None
    assert detect_loaded_filament({"vt_tray": ["nope"]}) is None


def test_filament_profile_name_uses_the_process_token():
    assert filament_profile_name("PLA", "N2S") == "Generic PLA @BBL A1"
    assert filament_profile_name("PLA", "N1") == "Generic PLA @BBL A1M"
    assert filament_profile_name("NOSUCH", "N2S") == ""


def test_available_filaments_lists_only_what_resolves():
    got = available_filaments("N2S", FIL_INDEX)
    assert [f["material"] for f in got] == ["PLA", "PETG"]
    assert got[0]["profile"] == "Generic PLA @BBL A1"
```

- [ ] **Step 2: Run them and watch them fail**

Run: `python -m pytest server/tests/test_slicepresets.py -q -k "filament or detect"`
Expected: FAIL — `ImportError: cannot import name 'detect_loaded_filament'`.

- [ ] **Step 3: Implement**

Append to `server/slicepresets.py`:

```python
# Materials offered, in menu order. Generic rather than Bambu-branded profiles
# because a spool the printer could not identify is, by definition, not a
# tagged Bambu spool -- and that is the common case.
MATERIALS = ("PLA", "PETG", "ABS", "TPU")


def filament_profile_name(material: str, model_id: str) -> str:
    """Filament profile name, or "" when unavailable. Filament profiles use
    the same token as process profiles (A1 / A1M), not the machine one."""
    token = PROCESS_TOKENS.get(model_id)
    if not token or material not in MATERIALS:
        return ""
    return f"Generic {material} @BBL {token}"


def available_filaments(model_id: str, index: dict) -> list:
    """Every material whose profile this slicer actually ships."""
    out = []
    for material in MATERIALS:
        name = filament_profile_name(material, model_id)
        if name and name in index:
            out.append({"material": material, "profile": name})
    return out


def detect_loaded_filament(state) -> str | None:
    """Material currently loaded, read off the live MQTT state, or None.

    None is the NORMAL case, not an error: the printer only knows the material
    when an RFID-tagged Bambu spool is loaded, and most filament is not that.
    The UI prefills with this and stays editable, so an unidentifiable spool
    never blocks slicing.

    Pure, and deliberately paranoid about shape: state is deep-merged from
    partial MQTT reports (master.md 3.1), so any node can be missing or be
    the wrong type at any moment.
    """
    if not isinstance(state, dict):
        return None
    ams = state.get("ams")
    if isinstance(ams, dict):
        units = ams.get("ams")
        if isinstance(units, list):
            for unit in units:
                if not isinstance(unit, dict):
                    continue
                trays = unit.get("tray")
                if not isinstance(trays, list):
                    continue
                for tray in trays:
                    if not isinstance(tray, dict):
                        continue
                    found = _material(tray.get("tray_type"))
                    if found:
                        return found
    return _material((state.get("vt_tray") or {}).get("tray_type")
                     if isinstance(state.get("vt_tray"), dict) else None)


def _material(value) -> str | None:
    """A tray_type string -> a material we have a profile for, else None."""
    if not isinstance(value, str):
        return None
    value = value.strip().upper()
    return value if value in MATERIALS else None
```

- [ ] **Step 4: Run the tests**

Run: `python -m pytest server/tests/test_slicepresets.py -q`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add server/slicepresets.py server/tests/test_slicepresets.py
git commit -m "feat(slicer): detect the loaded filament from MQTT state"
```

---

## Task 7: Slice jobs and the coordinator

**Files:**
- Create: `server/slicejobs.py`
- Test: `server/tests/test_slicejobs.py`

- [ ] **Step 1: Write the failing tests**

Create `server/tests/test_slicejobs.py`:

```python
import pathlib

import pytest

from server.slicejobs import SliceCoordinator
from server.slicer import SliceError

INDEX = {
    "Bambu Lab A1 0.4 nozzle": {"name": "Bambu Lab A1 0.4 nozzle"},
    "0.20mm Standard @BBL A1": {"name": "0.20mm Standard @BBL A1"},
    "Generic PLA @BBL A1": {"name": "Generic PLA @BBL A1"},
}

# A real .gcode.3mf parses to seconds/grams; fake it at the parse seam.
FAKE_META = {"seconds": 738, "grams": 3.75, "filaments": [],
             "printer_model_id": None}


class FakeRegistry:
    def __init__(self, model_id="N2S", nozzle="0.4"):
        self._model_id, self._nozzle = model_id, nozzle
        self.uploaded = []
        self.fail_upload = None

    def get(self, serial):
        return object() if serial == "AAA" else None

    def printer_model(self, serial):
        return self._model_id

    def printer_nozzle(self, serial):
        return self._nozzle

    def upload_sd_file(self, serial, path, data):
        if self.fail_upload:
            raise self.fail_upload
        self.uploaded.append((serial, path, data))


class FakeQueue:
    def __init__(self):
        self.jobs = []

    def add(self, serial, job):
        self.jobs.append((serial, job))


def make(tmp_path, *, run=None, registry=None, queue=None):
    def ok_run(exe, model, machine, process, filament, out_dir, **kw):
        out = pathlib.Path(out_dir) / "sliced.gcode.3mf"
        out.write_bytes(b"PK\x03\x04fake")
        return out
    return SliceCoordinator(
        registry or FakeRegistry(), queue if queue is not None else FakeQueue(),
        "bs.exe", INDEX, work_dir=tmp_path,
        run=run or ok_run, parse=lambda data: dict(FAKE_META))


def test_a_submitted_job_starts_queued(tmp_path):
    c = make(tmp_path)
    job_id = c.submit("AAA", "part.stl", b"solid", "standard", "PLA", False)
    assert c.get(job_id)["state"] == "queued"


def test_submit_rejects_an_unknown_printer(tmp_path):
    with pytest.raises(KeyError):
        make(tmp_path).submit("ZZZ", "p.stl", b"x", "standard", "PLA", False)


def test_submit_rejects_an_unresolvable_preset(tmp_path):
    c = make(tmp_path, registry=FakeRegistry(nozzle="0.8"))
    with pytest.raises(ValueError, match="preset"):
        c.submit("AAA", "p.stl", b"x", "standard", "PLA", False)


def test_a_successful_job_slices_uploads_and_queues(tmp_path):
    reg, q = FakeRegistry(), FakeQueue()
    c = make(tmp_path, registry=reg, queue=q)
    job_id = c.submit("AAA", "part.stl", b"solid", "standard", "PLA", False)
    c.run_once()

    job = c.get(job_id)
    assert job["state"] == "done"
    assert job["seconds"] == 738 and job["grams"] == 3.75

    serial, path, data = reg.uploaded[0]
    assert (serial, path) == ("AAA", "/part.gcode.3mf")
    assert data == b"PK\x03\x04fake"

    qserial, qjob = q.jobs[0]
    assert (qserial, qjob["sd_path"]) == ("AAA", "/part.gcode.3mf")
    assert qjob["source"] == "3mf"
    # The CLI omits printer_model_id, so provenance supplies it -- we know
    # which printer we sliced FOR. Without this the model guard is skipped.
    assert qjob["model_id"] == "N2S"


def test_the_uploaded_name_is_the_stl_name_with_a_3mf_extension(tmp_path):
    reg = FakeRegistry()
    c = make(tmp_path, registry=reg)
    c.submit("AAA", r"C:\evil\..\sub\Benchy.STL", b"x", "standard", "PLA", False)
    c.run_once()
    assert reg.uploaded[0][1] == "/Benchy.gcode.3mf"


def test_a_slice_failure_latches_and_leaves_the_queue_untouched(tmp_path):
    def boom(*a, **kw):
        raise SliceError("got error when validate: boom")
    q = FakeQueue()
    c = make(tmp_path, run=boom, queue=q)
    job_id = c.submit("AAA", "p.stl", b"x", "standard", "PLA", False)
    c.run_once()
    job = c.get(job_id)
    assert job["state"] == "failed"
    assert "boom" in job["error"]
    assert q.jobs == []


def test_an_upload_failure_leaves_the_queue_untouched(tmp_path):
    # Same principle as start's "dequeue only on confirmation": a step that
    # did not happen must not leave a half-finished job behind.
    reg = FakeRegistry()
    reg.fail_upload = RuntimeError("ftps died")
    q = FakeQueue()
    c = make(tmp_path, registry=reg, queue=q)
    job_id = c.submit("AAA", "p.stl", b"x", "standard", "PLA", False)
    c.run_once()
    assert c.get(job_id)["state"] == "failed"
    assert q.jobs == []


def test_the_work_directory_is_cleaned_up_on_success_and_failure(tmp_path):
    c = make(tmp_path)
    c.submit("AAA", "p.stl", b"x", "standard", "PLA", False)
    c.run_once()

    def boom(*a, **kw):
        raise SliceError("nope")
    c2 = make(tmp_path, run=boom)
    c2.submit("AAA", "p.stl", b"x", "standard", "PLA", False)
    c2.run_once()

    assert list(tmp_path.iterdir()) == []


def test_supports_flag_reaches_the_runner(tmp_path):
    seen = {}

    def spy(exe, model, machine, process, filament, out_dir, **kw):
        seen.update(kw)
        out = pathlib.Path(out_dir) / "sliced.gcode.3mf"
        out.write_bytes(b"x")
        return out
    c = make(tmp_path, run=spy)
    c.submit("AAA", "p.stl", b"x", "standard", "PLA", True)
    c.run_once()
    assert seen["supports"] is True


def test_jobs_are_listed_newest_first_and_filtered_by_serial(tmp_path):
    c = make(tmp_path)
    a = c.submit("AAA", "one.stl", b"x", "standard", "PLA", False)
    b = c.submit("AAA", "two.stl", b"x", "standard", "PLA", False)
    assert [j["id"] for j in c.list("AAA")] == [b, a]
    assert c.list("BBB") == []


def test_cancelling_a_queued_job_stops_it_running(tmp_path):
    q = FakeQueue()
    c = make(tmp_path, queue=q)
    job_id = c.submit("AAA", "p.stl", b"x", "standard", "PLA", False)
    assert c.cancel(job_id) is True
    c.run_once()
    assert c.get(job_id)["state"] == "cancelled"
    assert q.jobs == []


def test_run_once_is_a_noop_with_nothing_queued(tmp_path):
    make(tmp_path).run_once()
```

- [ ] **Step 2: Run them and watch them fail**

Run: `python -m pytest server/tests/test_slicejobs.py -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'server.slicejobs'`.

- [ ] **Step 3: Implement**

Create `server/slicejobs.py`:

```python
"""Slice jobs: submit a model, slice it, upload it, queue it.

ONE WORKER, GLOBALLY -- not one per printer. Slicing pegs a core, and this
server also supervises a YOLO detector process (master.md 2). A per-printer
worker would let a three-printer fleet start three slices at once and starve
detection, which is the one thing on this box that has to stay responsive.

Jobs are RUNTIME-ONLY and never persisted, the same reasoning as "arm is
runtime-only" (master.md 4.5): they are transient work, and a half-finished
slice pointing at a deleted temp directory must not survive a restart. The
RESULT is durable -- it lands on the microSD and in queues.json.
"""
from __future__ import annotations

import logging
import pathlib
import posixpath
import shutil
import threading
import time
import uuid

from . import slicepresets, threemf
from .slicer import SliceError, flatten_profile, run_slice

log = logging.getLogger("server.slicejobs")

TICK_S = 0.25

# Extensions the slicer can load. Anything else is refused at the boundary.
MODEL_EXTS = (".stl", ".3mf", ".step", ".stp", ".obj")


def output_name(filename: str) -> str:
    """An uploaded model filename -> the .gcode.3mf name to write to the card.

    Takes the basename and discards any directory component, for the same two
    reasons the SD upload route does (master.md 3.2): file:///sdcard/<name>
    has no path component, so a file in a subdirectory could be listed but
    never started -- and stripping the directory also makes a traversal
    attempt land harmlessly at the root.
    """
    base = posixpath.basename((filename or "").replace("\\", "/")).strip()
    stem = base
    for ext in MODEL_EXTS:
        if stem.lower().endswith(ext):
            stem = stem[: -len(ext)]
            break
    return f"{stem or 'model'}.gcode.3mf"


class SliceCoordinator:
    """Owns the job list and the single worker thread.

    `run` and `parse` are injectable so the whole state machine tests with no
    slicer, no printer and no camera -- the same seam design as
    DetectorSupervisor's `spawn` and the registry's `service_factory`.
    """

    def __init__(self, registry, queue, slicer_exe, index, *, work_dir,
                 run=run_slice, parse=threemf.parse_slice_info,
                 clock=time.time):
        self._registry = registry
        self._queue = queue
        self._exe = slicer_exe
        self._index = index
        self._work_dir = pathlib.Path(work_dir)
        self._run = run
        self._parse = parse
        self._clock = clock
        self._lock = threading.Lock()
        self._jobs: dict = {}
        self._order: list = []
        self._pending: list = []
        self._thread = None
        self._stop = threading.Event()

    # -- lifecycle ---------------------------------------------------------

    def start(self) -> None:
        if self._thread is not None:
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, daemon=True,
                                        name="slice-worker")
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        thread, self._thread = self._thread, None
        if thread is not None:
            thread.join(timeout=5.0)

    def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                self.run_once()
            except Exception:            # never let one bad job kill the loop
                log.exception("slice worker tick failed")
            self._stop.wait(TICK_S)

    # -- api ---------------------------------------------------------------

    def options(self, serial: str) -> dict:
        """Presets and filaments that actually resolve for this printer, plus
        the detected filament."""
        model_id = self._registry.printer_model(serial)
        nozzle = self._registry.printer_nozzle(serial)
        svc = self._registry.get(serial)
        detected = slicepresets.detect_loaded_filament(
            getattr(svc, "state", None))
        return {
            "model_id": model_id,
            "nozzle": nozzle,
            "presets": slicepresets.available_presets(model_id, nozzle,
                                                      self._index),
            "filaments": slicepresets.available_filaments(model_id,
                                                          self._index),
            "detected_filament": detected,
        }

    def submit(self, serial, filename, data, tier_id, material,
               supports) -> str:
        """Queue a slice. Raises KeyError (unknown printer) or ValueError
        (unusable preset/filament) -- both are 4xx at the route, and both are
        checked HERE so a doomed job never reaches the worker."""
        if self._registry.get(serial) is None:
            raise KeyError(serial)
        model_id = self._registry.printer_model(serial)
        nozzle = self._registry.printer_nozzle(serial)
        preset = slicepresets.resolve_preset(tier_id, model_id, nozzle,
                                             self._index)
        if preset is None:
            raise ValueError(
                f"no {tier_id!r} preset for this printer -- check its model "
                f"and nozzle ({nozzle} mm) on the Overview page")
        filament = slicepresets.filament_profile_name(material, model_id)
        if not filament or filament not in self._index:
            raise ValueError(f"no profile for {material!r} on this printer")

        job_id = uuid.uuid4().hex
        job = {
            "id": job_id, "serial": serial, "state": "queued",
            "name": output_name(filename), "source_name": filename,
            "preset": preset["id"], "preset_label": preset["label"],
            "material": material, "supports": bool(supports),
            "created": self._clock(), "error": None,
            "seconds": None, "grams": None, "sd_path": None,
        }
        with self._lock:
            self._jobs[job_id] = job
            self._order.append(job_id)
            self._pending.append((job_id, preset, filament, data))
        return job_id

    def get(self, job_id) -> dict | None:
        with self._lock:
            job = self._jobs.get(job_id)
            return dict(job) if job else None

    def list(self, serial=None) -> list:
        with self._lock:
            jobs = [dict(self._jobs[i]) for i in reversed(self._order)]
        return [j for j in jobs if serial is None or j["serial"] == serial]

    def cancel(self, job_id) -> bool:
        """Cancel a queued job or clear a finished one. A job already being
        sliced is left alone -- killing the subprocess mid-write is how you
        get a truncated file on the card."""
        with self._lock:
            job = self._jobs.get(job_id)
            if job is None:
                return False
            if job["state"] == "queued":
                job["state"] = "cancelled"
                self._pending = [p for p in self._pending if p[0] != job_id]
                return True
            if job["state"] in ("done", "failed", "cancelled"):
                self._jobs.pop(job_id, None)
                self._order.remove(job_id)
                return True
            return False

    # -- the work ----------------------------------------------------------

    def run_once(self) -> None:
        """Process at most one pending job. Public so tests drive the whole
        state machine synchronously, with no thread involved."""
        with self._lock:
            if not self._pending:
                return
            job_id, preset, filament_name, data = self._pending.pop(0)
            job = self._jobs.get(job_id)
            if job is None or job["state"] != "queued":
                return          # cancelled between submit and now
            job["state"] = "slicing"
            serial, name, supports = job["serial"], job["name"], job["supports"]
            source_name = job["source_name"]

        work = self._work_dir / job_id
        try:
            self._do(job_id, serial, name, source_name, preset, filament_name,
                     data, supports, work)
        except Exception as e:
            log.warning("slice job %s failed: %s", job_id, e)
            self._finish(job_id, "failed", error=str(e))
        finally:
            # Always: a work directory left behind holds a whole gcode file,
            # and this runs on every path including cancellation.
            shutil.rmtree(work, ignore_errors=True)

    def _do(self, job_id, serial, name, source_name, preset, filament_name,
            data, supports, work) -> None:
        work.mkdir(parents=True, exist_ok=True)
        model_path = work / posixpath.basename(
            source_name.replace("\\", "/")) or work / "model.stl"
        model_path.write_bytes(data)

        machine = flatten_profile(preset["machine"], self._index)
        process = flatten_profile(preset["process"], self._index)
        filament = flatten_profile(filament_name, self._index)

        produced = self._run(self._exe, model_path, machine, process, filament,
                             work, supports=supports)
        sliced = pathlib.Path(produced).read_bytes()

        meta = self._parse(sliced)
        self._set(job_id, state="uploading", seconds=meta.get("seconds"),
                  grams=meta.get("grams"))

        target = "/" + name
        self._registry.upload_sd_file(serial, target, sliced)

        seconds, grams = meta.get("seconds"), meta.get("grams")
        self._queue.add(serial, {
            "id": uuid.uuid4().hex,
            "sd_path": target,
            "name": name,
            "seconds": seconds,
            "grams": grams,
            "source": "3mf" if (seconds or grams) else "manual",
            # PROVENANCE, not the file. The CLI omits printer_model_id, so a
            # sliced file would otherwise skip the model guard entirely
            # (master.md 5.3). We know which printer we sliced for.
            "model_id": self._registry.printer_model(serial) or None,
        })
        self._finish(job_id, "done", sd_path=target)

    def _set(self, job_id, **fields) -> None:
        with self._lock:
            job = self._jobs.get(job_id)
            if job is not None:
                job.update(fields)

    def _finish(self, job_id, state, **fields) -> None:
        self._set(job_id, state=state, **fields)
```

- [ ] **Step 4: Run the tests**

Run: `python -m pytest server/tests/test_slicejobs.py -q`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add server/slicejobs.py server/tests/test_slicejobs.py
git commit -m "feat(slicer): slice job coordinator - slice, upload, queue"
```

---

## Task 8: The routes

**Files:**
- Modify: `server/main.py`
- Test: `server/tests/test_api.py`

- [ ] **Step 1: Write the failing tests**

Append to `server/tests/test_api.py`. Match the file's existing fixture style —
read the top of the file first and reuse its fake registry rather than adding a
second one.

```python
class FakeSlicer:
    """Stands in for SliceCoordinator at the route boundary."""

    def __init__(self):
        self.submitted = []
        self.jobs = []
        self.raise_on_submit = None

    def options(self, serial):
        return {"model_id": "N2S", "nozzle": "0.4",
                "presets": [{"id": "standard", "label": "Standard 0.20 mm"}],
                "filaments": [{"material": "PLA",
                               "profile": "Generic PLA @BBL A1"}],
                "detected_filament": "PLA"}

    def submit(self, serial, filename, data, tier, material, supports):
        if self.raise_on_submit:
            raise self.raise_on_submit
        self.submitted.append((serial, filename, data, tier, material,
                               supports))
        return "job-1"

    def list(self, serial=None):
        return self.jobs

    def cancel(self, job_id):
        return job_id == "job-1"


def test_slice_routes_404_when_no_slicer_is_installed():
    # "None means inert", same as queue=None / detection=None. The server must
    # still boot and monitor on a machine with no slicer.
    client = TestClient(create_app(FakeRegistry(), pathlib.Path("."),
                                   slicer=None))
    assert client.get("/api/printers/AAA/slice/options").status_code == 404
    assert client.get("/api/slice/jobs").status_code == 404


def test_slice_options_returns_presets_and_the_detected_filament():
    client = TestClient(create_app(FakeRegistry(), pathlib.Path("."),
                                   slicer=FakeSlicer()))
    body = client.get("/api/printers/AAA/slice/options").json()
    assert body["detected_filament"] == "PLA"
    assert body["presets"][0]["id"] == "standard"


def test_posting_a_model_starts_a_job():
    slicer = FakeSlicer()
    client = TestClient(create_app(FakeRegistry(), pathlib.Path("."),
                                   slicer=slicer))
    res = client.post(
        "/api/printers/AAA/slice",
        files={"file": ("part.stl", b"solid", "application/octet-stream")},
        data={"preset": "standard", "material": "PLA", "supports": "true"})
    assert res.status_code == 202
    assert res.json()["job_id"] == "job-1"
    serial, filename, data, tier, material, supports = slicer.submitted[0]
    assert (serial, filename, data) == ("AAA", "part.stl", b"solid")
    assert (tier, material, supports) is not None and supports is True


def test_posting_an_unsupported_extension_is_a_400():
    client = TestClient(create_app(FakeRegistry(), pathlib.Path("."),
                                   slicer=FakeSlicer()))
    res = client.post(
        "/api/printers/AAA/slice",
        files={"file": ("notes.txt", b"hello", "text/plain")},
        data={"preset": "standard", "material": "PLA", "supports": "false"})
    assert res.status_code == 400


def test_posting_an_empty_file_is_a_400():
    client = TestClient(create_app(FakeRegistry(), pathlib.Path("."),
                                   slicer=FakeSlicer()))
    res = client.post(
        "/api/printers/AAA/slice",
        files={"file": ("part.stl", b"", "application/octet-stream")},
        data={"preset": "standard", "material": "PLA", "supports": "false"})
    assert res.status_code == 400


def test_an_unresolvable_preset_is_a_400_not_a_500():
    slicer = FakeSlicer()
    slicer.raise_on_submit = ValueError("no 'standard' preset")
    client = TestClient(create_app(FakeRegistry(), pathlib.Path("."),
                                   slicer=slicer))
    res = client.post(
        "/api/printers/AAA/slice",
        files={"file": ("part.stl", b"solid", "application/octet-stream")},
        data={"preset": "standard", "material": "PLA", "supports": "false"})
    assert res.status_code == 400
    assert "preset" in res.json()["detail"]


def test_an_unknown_printer_is_a_404():
    slicer = FakeSlicer()
    slicer.raise_on_submit = KeyError("ZZZ")
    client = TestClient(create_app(FakeRegistry(), pathlib.Path("."),
                                   slicer=slicer))
    res = client.post(
        "/api/printers/ZZZ/slice",
        files={"file": ("part.stl", b"solid", "application/octet-stream")},
        data={"preset": "standard", "material": "PLA", "supports": "false"})
    assert res.status_code == 404


def test_cancelling_an_unknown_job_is_a_404():
    client = TestClient(create_app(FakeRegistry(), pathlib.Path("."),
                                   slicer=FakeSlicer()))
    assert client.delete("/api/slice/jobs/nope").status_code == 404
    assert client.delete("/api/slice/jobs/job-1").status_code == 204
```

- [ ] **Step 2: Run them and watch them fail**

Run: `python -m pytest server/tests/test_api.py -q -k slice`
Expected: FAIL — `TypeError: create_app() got an unexpected keyword argument 'slicer'`.

- [ ] **Step 3: Implement**

In `server/main.py`, change the signature and docstring:

```python
def create_app(registry, runs_dir: pathlib.Path,
               frontend_dist: pathlib.Path | None = None,
               detection=None, queue=None, slicer=None) -> FastAPI:
```

Add to the docstring: `` `slicer` is a SliceCoordinator (or a test fake); None disables the slice routes entirely, same "None means inert" convention. ``

Add `Form` to the existing `fastapi` import. Then add, after the queue routes:

```python
    def _require_slicer() -> None:
        if slicer is None:
            raise HTTPException(
                404, "slicing is not available on this server -- Bambu Studio "
                     "was not found. Set BAMBU_STUDIO_EXE if it is installed "
                     "elsewhere.")

    @app.get("/api/printers/{serial}/slice/options")
    def slice_options(serial: str):
        _require_slicer()
        if registry.get(serial) is None:
            raise HTTPException(404, "unknown printer")
        return slicer.options(serial)

    @app.post("/api/printers/{serial}/slice", status_code=202)
    def start_slice(serial: str, file: UploadFile = File(...),
                    preset: str = Form(...), material: str = Form(...),
                    supports: bool = Form(False)):
        # SYNC def for the same reason as the SD routes: reading a large model
        # off the wire must not stall the event loop and freeze every
        # WebSocket. The slice itself runs on the coordinator's own thread.
        _require_slicer()
        name = os.path.basename((file.filename or "").replace("\\", "/")).strip()
        if not name:
            raise HTTPException(400, "no filename")
        if not name.lower().endswith(slicejobs.MODEL_EXTS):
            raise HTTPException(
                400, f"{name}: not a model file. Upload one of "
                     f"{', '.join(slicejobs.MODEL_EXTS)}.")
        data = file.file.read()
        if not data:
            raise HTTPException(400, "empty file")
        try:
            job_id = slicer.submit(serial, name, data, preset, material,
                                   bool(supports))
        except KeyError:
            raise HTTPException(404, "unknown printer")
        except ValueError as e:
            raise HTTPException(400, str(e))  # bad choice, not a server fault
        return {"job_id": job_id}

    @app.get("/api/slice/jobs")
    def list_slice_jobs(serial: str | None = None):
        _require_slicer()
        return {"jobs": slicer.list(serial)}

    @app.delete("/api/slice/jobs/{job_id}", status_code=204)
    def cancel_slice_job(job_id: str):
        _require_slicer()
        if not slicer.cancel(job_id):
            raise HTTPException(404, "unknown job")
        return Response(status_code=204)
```

Add `from . import slicejobs` to the module imports.

- [ ] **Step 4: Run the tests**

Run: `python -m pytest server/tests/test_api.py -q`
Expected: PASS, including every pre-existing test (the new `slicer` parameter defaults to `None`).

- [ ] **Step 5: Commit**

```bash
git add server/main.py server/tests/test_api.py
git commit -m "feat(api): slice routes, inert when no slicer is installed"
```

---

## Task 9: Wiring

**Files:**
- Modify: `server/__main__.py`

- [ ] **Step 1: Wire the coordinator**

In `server/__main__.py`, add a `--no-slicer` flag beside the existing flags:

```python
    ap.add_argument("--no-slicer", action="store_true",
                    help="disable slicing even if Bambu Studio is installed")
```

Build the coordinator after the queue is built and before `create_app`:

```python
    slicer = None
    if not args.no_slicer:
        exe = slicer_mod.find_slicer()
        if exe is None:
            log.info("no Bambu Studio found; slicing disabled "
                     "(set BAMBU_STUDIO_EXE to point at it)")
        else:
            index = slicer_mod.ProfileIndex.load(slicer_mod.profiles_root(exe))
            if not index:
                log.warning("found %s but no vendor profiles beside it; "
                            "slicing disabled", exe)
            else:
                log.info("slicing enabled: %s (%d profiles)", exe, len(index))
                slicer = SliceCoordinator(
                    registry, queue, exe, index,
                    work_dir=pathlib.Path(args.runs_dir) / "_slice")
```

Import them at the top:

```python
from . import slicer as slicer_mod
from .slicejobs import SliceCoordinator
```

Pass it through: `create_app(..., queue=queue, slicer=slicer)`.

- [ ] **Step 2: Start and stop it with the app**

In `server/main.py`'s `lifespan`, alongside the existing detection start/stop:

```python
        if slicer is not None:
            slicer.start()
        try:
            yield
        finally:
            if slicer is not None:
                slicer.stop()
```

Keep the existing `detection` start/stop; both run in the same lifespan.

- [ ] **Step 3: Verify by hand**

Run: `python -m server --mock --port 8011`
Expected: the log line `slicing enabled: C:\Program Files\Bambu Studio\bambu-studio.exe (N profiles)` with N in the low thousands. Then:

```bash
curl http://127.0.0.1:8011/api/slice/jobs
```
Expected: `{"jobs":[]}`.

Stop the server with Ctrl-C and confirm it exits without hanging (the worker is a daemon thread and `stop()` joins it).

- [ ] **Step 4: Run the full suite and commit**

Run: `python -m pytest -q`
Expected: PASS.

```bash
git add server/__main__.py server/main.py
git commit -m "feat: wire the slice coordinator into the server"
```

---

## Task 10: Frontend

**Files:**
- Modify: `frontend/src/api/printer.js`
- Create: `frontend/src/components/slice/SliceForm.jsx`
- Create: `frontend/src/components/slice/SliceJobList.jsx`
- Create: `frontend/src/pages/Slice.jsx`
- Modify: `frontend/src/app/pageRegistry.jsx`

- [ ] **Step 1: Add the API wrappers**

Read the existing wrappers in `frontend/src/api/printer.js` first and match their
shape exactly, including the `detail(res)` helper for errors. Append:

```js
export async function fetchSliceOptions(serial) {
  const res = await fetch(`/api/printers/${serial}/slice/options`)
  if (!res.ok) throw new Error(await detail(res))
  return res.json()
}

export async function startSlice(serial, file, { preset, material, supports }) {
  const body = new FormData()
  body.append('file', file)
  body.append('preset', preset)
  body.append('material', material)
  body.append('supports', supports ? 'true' : 'false')
  const res = await fetch(`/api/printers/${serial}/slice`, { method: 'POST', body })
  if (!res.ok) throw new Error(await detail(res))
  return res.json()
}

export async function fetchSliceJobs(serial) {
  const res = await fetch(`/api/slice/jobs?serial=${encodeURIComponent(serial)}`)
  if (!res.ok) throw new Error(await detail(res))
  return (await res.json()).jobs
}

export async function cancelSliceJob(id) {
  const res = await fetch(`/api/slice/jobs/${id}`, { method: 'DELETE' })
  if (!res.ok) throw new Error(await detail(res))
}
```

- [ ] **Step 2: Build the form**

Create `frontend/src/components/slice/SliceForm.jsx`. Read an existing form —
`components/printers/EditPrinterForm.jsx` — and match its structure and class
names rather than inventing new ones.

Requirements, all of which come from the spec:
- a file input accepting `.stl,.3mf,.step,.stp,.obj`
- a preset radio group built from `options.presets`, showing `label`
- a filament `<select>` from `options.filaments`, **defaulted to
  `options.detected_filament` when present**, and always editable
- a "Tree supports" checkbox, **defaulting to unchecked**
- a submit button, disabled while no file is chosen or a submit is in flight
- when `options.presets` is empty, render the reason instead of the form:
  "No presets available for this printer. Check its model and nozzle on the
  Overview page." — an empty list means the model/nozzle is unset or unusual,
  and a bare empty form gives the user nothing to act on.

- [ ] **Step 3: Build the job list**

Create `frontend/src/components/slice/SliceJobList.jsx`: a table of
`{name, preset_label, material, supports, state}`, plus `seconds`/`grams` once
`done`, plus `error` in a `<pre>` when `failed`, and a cancel/clear button per
row. Poll `fetchSliceJobs` every 2 s with the same `requestId` ref guard
`QueuePanel` uses so an out-of-order response can never clobber a newer one.

- [ ] **Step 4: Build the page and register it**

Create `frontend/src/pages/Slice.jsx` taking the standard
`{printers, selected, onSelect}` props, rendering `SliceForm` and
`SliceJobList` keyed by `selected.serial` — `key={printer.serial}` so switching
printers **remounts** rather than resetting via an Effect, exactly as
`SdFiles.jsx` and `QueuePanel` do, and for the reason documented there.

Register it in `frontend/src/app/pageRegistry.jsx` (pages are added there and
nowhere else):

```jsx
  { key: 'slice', label: 'Slice', component: Slice },
```

Place it before `queue`, since slicing feeds the queue.

- [ ] **Step 5: Build and check by eye**

Run: `cd frontend && npm run build`
Expected: build succeeds with no errors.

Then `python -m server --mock` and open <http://127.0.0.1:8000>. Confirm: the
Slice tab appears, presets load for a printer with `model_id` set, the filament
dropdown prefills, and submitting a small STL creates a job row that moves
`queued → slicing → …`.

Note that under `--mock` the *upload* goes to `MockPrinter`, so the job will
complete without touching hardware — that exercises the whole state machine but
proves nothing about the printer.

- [ ] **Step 6: Commit**

```bash
git add frontend/src
git commit -m "feat(frontend): slice page - preset, filament, tree supports, job list"
```

---

## Task 11: Documentation

`master.md` is the umbrella doc and is authoritative. A new subsystem that
isn't in it is a subsystem the next person won't find.

**Files:**
- Modify: `master.md`
- Modify: `docs/superpowers/specs/2026-07-22-auto-slicing-design.md`

- [ ] **Step 1: Update `master.md`**

1. §1 data-flow narrative: add slicing as a step, noting the server shells out
   to Bambu Studio and that this is the only place it does.
2. §3.2 module table: add `slicer.py`, `slicepresets.py`, `slicejobs.py` with
   their key names.
3. New section after §5 covering: the engine choice and **why it is not
   OrcaSlicer**, profile flattening, the verified argv, the `A1`/`A1M` token
   split, and that the CLI omits `printer_model_id` so the job records
   provenance instead.
4. §6 frontend pages table: add the `slice` page.
5. §8 file layout: add `runs/_slice/` (per-job temp directories).
6. §9 testing table: add the three new test files.
7. §10 gotchas: add "**The engine is Bambu Studio, not OrcaSlicer**" and
   "**Preset names are not stable strings**".
8. §9's "Not covered" note: record that a CLI-sliced 3mf has **not** been
   started on real hardware, and that this makes FTPS STOR load-bearing.

Renumber carefully: `test_docs.py` checks that every `§N` reference in
`master.md` resolves to a real heading, including ones in `server/*.py`
comments.

- [ ] **Step 2: Flip the spec's status banner**

Change `STATUS: PROPOSED (2026-07-22)` to `STATUS: SHIPPED (<date>)` and note
anything the design got wrong in advance, following the format the
`2026-07-21-reconnect-roi-editor-model-check-design.md` banner uses.

- [ ] **Step 3: Run the doc tests**

Run: `python -m pytest server/tests/test_docs.py -q`
Expected: PASS. This checks every relative link resolves, every `§N` matches a
real heading, no hardcoded suite sizes, and every `docs/superpowers/` file
declares a STATUS.

- [ ] **Step 4: Run everything and commit**

Run: `python -m pytest -q && cd frontend && npm test && cd ..`
Expected: PASS both.

```bash
git add master.md docs/superpowers
git commit -m "docs: document the slicing subsystem in master.md"
```

---

## Task 12: The hardware gate

**This is the only step that can prove the feature works.** Everything above
proves the container has the right shape; none of it proves the printer accepts
it. Per master.md §1.1, it stays *unverified* until this is done.

**Files:**
- Modify: `master.md`, `docs/superpowers/specs/2026-07-22-auto-slicing-design.md`

- [ ] **Step 1: Slice something small**

With the real printer registered and **idle**, upload a small STL through the
Slice page for the actual printer. Confirm the job reaches `done` and the SD
Files page lists the new `.gcode.3mf` at the card root.

This is also the first time FTPS **STOR** has ever written to a real card
(master.md §9) — if it fails, that is the more likely culprit, not the slicer.

- [ ] **Step 2: Start it from the queue**

Press Start on the queued job and watch `gcode_state`. Expected: `IDLE →
PREPARE`, with the printer echoing the filename back as `subtask_name`.

**Stay at the printer.** This is the first print this repo has ever produced
end to end, and the first-layer behaviour of a CLI-sliced file is unproven.

- [ ] **Step 3: Record what actually happened**

Update master.md's §1.1 verification table and the spec's §8/§9 with what was
observed — on which machine, on which date, and what did *not* work. If the
printer rejected the file, record the exact symptom before changing any code:
that symptom is the only evidence of what the container is missing.

```bash
git add master.md docs/superpowers
git commit -m "docs: record the hardware verification of CLI-sliced prints"
```

---

## Self-review notes

Spec coverage checked section by section: §3 modules → Tasks 2–7; §4.1 nozzle →
Task 1; §4.2 filament → Task 6; §4.3 tiers → Task 5; §4.4 supports → Task 4
(`run_slice`) and Task 10 (checkbox); §5 flow → Task 7; §6 routes → Task 8;
§7 frontend → Task 10; §8 testing → distributed across every task; §9 risks →
the timeout in Task 4, the options filtering in Task 5, and Task 12.

Names are consistent across tasks: `flatten_profile`, `ProfileIndex.load`,
`find_slicer`, `profiles_root`, `build_argv`, `run_slice`, `resolve_preset`,
`available_presets`, `filament_profile_name`, `available_filaments`,
`detect_loaded_filament`, `SliceCoordinator.{options,submit,get,list,cancel,
run_once,start,stop}`, `output_name`, `MODEL_EXTS`, `PrinterConfig.nozzle`,
`registry.printer_nozzle`.
