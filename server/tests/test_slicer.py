import json
import pathlib
import subprocess

import pytest

from server.slicer import (ProfileIndex, SliceError, build_argv,
                           find_slicer, flatten_profile, profiles_root,
                           run_slice)


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


# ---------------- bed type ----------------
# MEASURED 2026-07-22: with curr_bed_type never set, Bambu Studio defaults to
# Cool Plate (35 C for PLA); this lab's A1 has a Textured PEI Plate (65 C)
# and a print with no bed adhesion stalled at 5% with an HMS warning. See
# store.py's BED_TYPES for the full writeup.

def test_run_slice_patches_curr_bed_type(tmp_path):
    run_slice("bs.exe", tmp_path / "m.stl", MACHINE, PROCESS, FILAMENT,
              tmp_path, bed_type="Textured PEI Plate",
              runner=_fake_runner(tmp_path, "sliced.gcode.3mf"))
    written = json.loads((tmp_path / "process.json").read_text(encoding="utf-8"))
    assert written["curr_bed_type"] == "Textured PEI Plate"


def test_run_slice_defaults_curr_bed_type_to_the_textured_pei_plate(tmp_path):
    # A caller that forgets to pass bed_type must still get the plate that's
    # actually installed in this lab, not Bambu Studio's own Cool Plate
    # default that caused today's failure.
    run_slice("bs.exe", tmp_path / "m.stl", MACHINE, PROCESS, FILAMENT,
              tmp_path, runner=_fake_runner(tmp_path, "sliced.gcode.3mf"))
    written = json.loads((tmp_path / "process.json").read_text(encoding="utf-8"))
    assert written["curr_bed_type"] == "Textured PEI Plate"


def test_run_slice_does_not_mutate_the_callers_process_dict_with_bed_type(tmp_path):
    run_slice("bs.exe", tmp_path / "m.stl", MACHINE, PROCESS, FILAMENT,
              tmp_path, bed_type="Cool Plate",
              runner=_fake_runner(tmp_path, "sliced.gcode.3mf"))
    assert "curr_bed_type" not in PROCESS


def test_run_slice_raises_with_stderr_when_the_slicer_fails(tmp_path):
    runner = _fake_runner(tmp_path, "sliced.gcode.3mf", returncode=2,
                          stderr="got error when validate: boom")
    with pytest.raises(SliceError, match="boom"):
        run_slice("bs.exe", tmp_path / "m.stl", MACHINE, PROCESS, FILAMENT,
                  tmp_path, runner=runner)


def test_run_slice_rejects_a_nonzero_exit_even_when_a_file_exists(tmp_path):
    # Bambu Studio exits 0 on success (verified 2026-07-22 -- see the
    # module's run_slice docstring). So a nonzero code with a file on disk
    # means a crash part-way through export left a TRUNCATED .gcode.3mf --
    # "there is a file" must never be enough on its own, since those bytes
    # get uploaded straight to a printer's microSD and queued.
    #
    # NOT _fake_runner: it only writes the file on returncode == 0, so it
    # cannot represent this scenario. This runner writes the (truncated)
    # file AND returns nonzero, which is the case under test.
    def runner(argv, **kw):
        (pathlib.Path(argv[argv.index("--outputdir") + 1])
         / "sliced.gcode.3mf").write_bytes(b"PK\x03\x04truncated")
        return subprocess.CompletedProcess(argv, 1, "", "crashed mid-export")
    with pytest.raises(SliceError, match="crashed mid-export"):
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


def test_run_slice_turns_an_oserror_into_a_slice_error(tmp_path):
    # Simulates the slicer binary vanishing between find_slicer() and the
    # call (uninstalled, or a stale cached path, mid-session).
    def runner(argv, **kw):
        raise FileNotFoundError("bs.exe not found")
    with pytest.raises(SliceError, match="could not run the slicer"):
        run_slice("bs.exe", tmp_path / "m.stl", MACHINE, PROCESS, FILAMENT,
                  tmp_path, runner=runner)
