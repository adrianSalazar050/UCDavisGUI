import json
import pathlib
import subprocess

import pytest

from server.slicer import (ProfileIndex, SliceError, bed_forward_gcode,
                           build_argv, find_slicer, flatten_profile,
                           profiles_root, run_slice)


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


# --- include resolution ---
# Bambu splits a machine's big gcode blocks (start/end/layer-change/timelapse/
# change-filament) into separate "template" profiles pulled in via `include`,
# and the main profile does NOT define those fields itself. Resolving only
# `inherits` drops all of them and falls back to the generic base gcode.
# Measured 2026-07-23: that shipped M109 S205 instead of the filament's 220 C
# and a print that ran one layer then halted, because the generic start omits
# the A1's real bed-mesh/first-layer init.

def test_flatten_merges_an_included_profiles_keys():
    index = {
        "base": {"name": "base", "walls": "2"},
        "tmpl": {"name": "tmpl", "machine_start_gcode": "REAL"},
        "kid": {"name": "kid", "inherits": "base", "include": ["tmpl"]},
    }
    out = flatten_profile("kid", index)
    assert out["machine_start_gcode"] == "REAL"
    assert out["walls"] == "2"


def test_include_overrides_inherited_keys():
    # THE load-bearing property: the included template IS the machine's real
    # gcode and must win over the generic one the inherit chain provides.
    index = {
        "generic": {"name": "generic", "machine_start_gcode": "GENERIC S205"},
        "tmpl": {"name": "tmpl", "machine_start_gcode": "BAMBU S220"},
        "kid": {"name": "kid", "inherits": "generic", "include": ["tmpl"]},
    }
    assert flatten_profile("kid", index)["machine_start_gcode"] == "BAMBU S220"


def test_own_keys_override_an_included_profile():
    index = {
        "tmpl": {"name": "tmpl", "layer_gcode": "FROM_TEMPLATE"},
        "kid": {"name": "kid", "include": ["tmpl"], "layer_gcode": "OWN"},
    }
    assert flatten_profile("kid", index)["layer_gcode"] == "OWN"


def test_include_does_not_leak_the_templates_metadata():
    # A template's own name/instantiation must not shadow the including
    # profile's identity.
    index = {
        "tmpl": {"name": "tmpl", "instantiation": "false",
                 "machine_start_gcode": "REAL"},
        "kid": {"name": "kid", "include": ["tmpl"]},
    }
    out = flatten_profile("kid", index)
    assert out["name"] == "kid"
    assert out.get("instantiation") != "false" or "instantiation" not in out
    assert out["machine_start_gcode"] == "REAL"


def test_multiple_includes_merge_in_order():
    index = {
        "a": {"name": "a", "machine_start_gcode": "START"},
        "b": {"name": "b", "machine_end_gcode": "END"},
        "kid": {"name": "kid", "include": ["a", "b"]},
    }
    out = flatten_profile("kid", index)
    assert out["machine_start_gcode"] == "START"
    assert out["machine_end_gcode"] == "END"


def test_a_single_string_include_is_accepted():
    index = {
        "tmpl": {"name": "tmpl", "machine_start_gcode": "REAL"},
        "kid": {"name": "kid", "include": "tmpl"},
    }
    assert flatten_profile("kid", index)["machine_start_gcode"] == "REAL"


def test_flatten_raises_on_a_missing_include():
    # Loud, not silent: an unresolved template drops the entire machine start
    # gcode back to the generic fallback, which is exactly the dangerous
    # wrong-file case this whole mechanism exists to prevent.
    with pytest.raises(SliceError, match="gone"):
        flatten_profile("kid", {"kid": {"name": "kid", "include": ["gone"]}})


def test_flatten_raises_on_an_include_cycle():
    index = {"a": {"name": "a", "include": ["b"]},
             "b": {"name": "b", "include": ["a"]}}
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


# --- cross-platform slicer detection (for the Linux/mac desktop builds) ---
# On Linux Bambu Studio ships as an AppImage the user downloads, so there is no
# canonical install path -- detection is best-effort, and BAMBU_STUDIO_EXE /
# BAMBU_STUDIO_PROFILES let the user point at it explicitly when a guess misses.

def test_find_slicer_finds_a_linux_appimage_in_applications(tmp_path):
    apps = tmp_path / "Applications"
    apps.mkdir()
    appimage = apps / "Bambu_Studio_ubuntu.AppImage"
    appimage.write_text("", encoding="utf-8")
    got = find_slicer({}, system="linux", home=tmp_path)
    assert got == str(appimage)


def test_find_slicer_finds_a_linux_binary_on_a_standard_path(tmp_path):
    # Simulate /opt/bambu-studio/bambu-studio via an injected home-relative tree
    # is awkward; instead assert the candidate list includes the usual places.
    from server.slicer import _slicer_candidates
    cands = _slicer_candidates("linux", tmp_path)
    assert "/usr/bin/bambu-studio" in cands
    assert str(tmp_path / ".local" / "bin" / "bambu-studio") in cands


def test_find_slicer_env_override_still_wins_on_linux(tmp_path):
    exe = tmp_path / "my-bambu.AppImage"
    exe.write_text("", encoding="utf-8")
    assert find_slicer({"BAMBU_STUDIO_EXE": str(exe)},
                       system="linux", home=tmp_path) == str(exe)


def test_find_slicer_returns_none_on_linux_with_nothing_installed(tmp_path):
    assert find_slicer({}, system="linux", home=tmp_path) is None


def test_profiles_root_honors_the_env_override(tmp_path):
    prof = tmp_path / "custom_profiles"
    prof.mkdir()
    got = profiles_root("/anywhere/bambu-studio",
                        env={"BAMBU_STUDIO_PROFILES": str(prof)})
    assert got == prof


def test_profiles_root_falls_back_to_the_linux_user_config(tmp_path):
    # An AppImage has no readable resources/ beside it, but Bambu Studio writes
    # its system profiles to ~/.config/BambuStudio/system/BBL after first run.
    cfg = tmp_path / ".config" / "BambuStudio" / "system" / "BBL"
    cfg.mkdir(parents=True)
    got = profiles_root(str(tmp_path / "Bambu.AppImage"),
                        env={}, system="linux", home=tmp_path)
    assert got == cfg


def test_profiles_root_prefers_beside_the_exe_when_it_exists(tmp_path):
    beside = tmp_path / "resources" / "profiles" / "BBL"
    beside.mkdir(parents=True)
    exe = tmp_path / "bambu-studio"
    got = profiles_root(str(exe), env={}, system="linux", home=tmp_path)
    assert got == beside


def test_profiles_root_returns_the_beside_path_when_nothing_exists(tmp_path):
    # Graceful: ProfileIndex.load tolerates a missing dir (slicing disabled),
    # so returning the primary candidate is fine even when it doesn't exist.
    exe = tmp_path / "bambu-studio"
    got = profiles_root(str(exe), env={}, system="linux", home=tmp_path)
    assert got.as_posix().endswith("resources/profiles/BBL")


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


# --- end-of-print "plate fully forward" for the A1 family -------------------
# Measured 2026-07-23: the A1 parks at Y180 of a 256 bed (~70% forward), while
# the mini's Y180 IS its max. The stock end gcode's last lines are M400 /
# M18 X Y Z, so anything appended must re-enable the steppers first or it runs
# after the motors are off and does nothing.

A1 = {"name": "m", "printer_model": "Bambu Lab A1",
      "printable_area": ["0x0", "256x0", "256x256", "0x256"]}
A1_MINI = {"name": "m", "printer_model": "Bambu Lab A1 mini",
           "printable_area": ["0x0", "180x0", "180x180", "0x180"]}
P1S = {"name": "m", "printer_model": "Bambu Lab P1S",
       "printable_area": ["0x0", "256x0", "256x256", "0x256"]}


def test_bed_forward_moves_the_a1_to_its_max_y():
    got = bed_forward_gcode(A1)
    assert "G1 Y256" in got


def test_bed_forward_moves_the_mini_to_its_max_y():
    # 180 is already where the mini parks -- a harmless no-op, and keeping both
    # models on one derived code path beats special-casing the A1.
    assert "G1 Y180" in bed_forward_gcode(A1_MINI)


def test_bed_forward_re_enables_the_steppers_before_moving():
    # The whole point: the stock end gcode disabled them with M18.
    got = bed_forward_gcode(A1)
    assert got.index("M17") < got.index("G1 Y256")
    assert "G90" in got          # absolute, or Y256 would be a relative jog


def test_bed_forward_does_not_move_x():
    # Stock parks the toolhead off to the side; dragging it back over the plate
    # would defeat the purpose.
    for line in bed_forward_gcode(A1).splitlines():
        if line.strip().startswith("G1 "):
            assert " X" not in line


def test_bed_forward_skips_corexy_printers():
    # P1/X1 beds only move in Z -- a Y "eject" is meaningless there.
    assert bed_forward_gcode(P1S) is None


def test_bed_forward_skips_an_unknown_or_missing_model():
    assert bed_forward_gcode({"printable_area": ["0x0", "256x256"]}) is None
    assert bed_forward_gcode({"printer_model": "Something Else"}) is None


def test_bed_forward_skips_a_malformed_printable_area():
    # Degrade to "no change", never to a bad move.
    for bad in (None, [], "256x256", ["nonsense"], ["0x0", "256xNaN"],
                ["0x0", "0x0"]):
        assert bed_forward_gcode(
            {"printer_model": "Bambu Lab A1", "printable_area": bad}) is None


def test_run_slice_appends_the_bed_forward_block_for_an_a1(tmp_path):
    machine = dict(A1, machine_end_gcode="M400\nM18 X Y Z")
    run_slice("bs.exe", tmp_path / "m.stl", machine, PROCESS, FILAMENT,
              tmp_path, runner=_fake_runner(tmp_path, "sliced.gcode.3mf"))
    written = json.loads((tmp_path / "machine.json").read_text(encoding="utf-8"))
    assert "G1 Y256" in written["machine_end_gcode"]
    assert written["machine_end_gcode"].startswith("M400")   # stock kept first
    assert machine["machine_end_gcode"] == "M400\nM18 X Y Z"  # caller untouched


def test_run_slice_leaves_a_corexy_machine_end_gcode_alone(tmp_path):
    machine = dict(P1S, machine_end_gcode="M400\nM18 X Y Z")
    run_slice("bs.exe", tmp_path / "m.stl", machine, PROCESS, FILAMENT,
              tmp_path, runner=_fake_runner(tmp_path, "sliced.gcode.3mf"))
    written = json.loads((tmp_path / "machine.json").read_text(encoding="utf-8"))
    assert written["machine_end_gcode"] == "M400\nM18 X Y Z"
