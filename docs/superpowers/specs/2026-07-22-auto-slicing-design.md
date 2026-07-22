# Automatic slicing: printer + filament + preset → startable gcode — design

> **STATUS: PROPOSED (2026-07-22).** Agreed in brainstorming; **not implemented**.
> Written in the future tense. Nothing in `server/` implements any of this yet.
>
> The feasibility findings in §2 **are** verified — they were measured on this
> machine on 2026-07-22 against Bambu Studio and OrcaSlicer as installed. Treat
> §2 as fact and the rest as intent.
>
> Historical record, not maintained. **`master.md` is authoritative wherever
> this file disagrees with it.**

Date: 2026-07-22

---

## 1. The goal

Today the queue can only reference `.gcode.3mf` files that **already exist** on a
printer's microSD — something a human sliced in Bambu Studio and copied across.
There is no slicing anywhere in the repo.

This feature closes that gap: the operator drops an STL into the dashboard,
picks a printer, and gets a queued, startable job. Concretely —

1. user uploads an STL and selects one of the registered printers,
2. the server derives the slicer machine profile from that printer's configured
   `model_id` + nozzle,
3. the filament is **detected** from the live MQTT state and prefilled, editable,
4. a curated named preset supplies the process settings, plus a tree-support
   toggle,
5. the server slices, uploads the result over FTPS, and adds it to the queue.

Step 5 is entirely existing machinery (`sdcard.upload_file`, `PrintQueue.add`),
which is most of why this is affordable. The new code stops at producing a file.

Where it ends: the existing start route (§5.4 of `master.md`) takes over
unchanged. This feature never commands a printer.

---

## 2. Feasibility, measured 2026-07-22

Everything in this section was run on this machine and is reproducible. It is
recorded because the naive version of this feature does not work, and the
reasons are non-obvious.

### 2.1 The engine is Bambu Studio, not OrcaSlicer

Both are installed (`C:\Program Files\Bambu Studio\bambu-studio.exe`,
`C:\Program Files\OrcaSlicer\orca-slicer.exe`); both are PrusaSlicer forks with
near-identical CLIs. They are **not** interchangeable here.

| | OrcaSlicer 01.10.01.50 | Bambu Studio |
|---|---|---|
| Slices an STL headless | yes | yes |
| Needs `inherits` flattening (§2.2) | yes | yes |
| Needs `use_relative_e_distances` patched or it refuses to slice | **yes** | no |
| Emits a `.gcode.3mf` via `--export-3mf` | **no — never produced a file, across 5 argument orderings** | **yes** |

That last row decides it. A raw `.gcode` **cannot be started over MQTT** — per
`master.md` §5.4 the `project_file` command points at `Metadata/plate_N.gcode`
*inside the zip*, and the `sdcard.py` table records that raw `.gcode` is
printer-screen-only. Orca gave us a file the printer can't be told to run.

**Do not "simplify" this to OrcaSlicer later.** It looks like the more open
choice and it is the one that fails.

### 2.2 System profiles are `inherits` partials

Vendor profiles are **not** self-contained. `Bambu Lab A1 0.4 nozzle.json` has 39
keys and an `inherits: "fdm_bbl_3dp_001_common"`; the process profile has 11 keys
and inherits `fdm_process_bbl_0.20`. Passing them straight to `--load-settings`
fails validation.

Resolving the chain recursively yields self-contained configs — 109 / 188 / 131
keys for machine / process / filament, from an index of **1,932 presets** under
`resources/profiles/BBL/**/*.json`. Profiles are indexed by their `name` field,
which is *not* always the filename.

Note also that the OTA-updated copy under `%APPDATA%` and the shipped copy under
`resources/` can differ. **Index the installed `resources/profiles` tree**, which
is the one Bambu Studio itself validated against.

### 2.3 The verified invocation

```
bambu-studio.exe <model.stl>
  --load-settings  "<flat_machine.json>;<flat_process.json>"
  --load-filaments "<flat_filament.json>"
  --slice 0
  --export-3mf     "<name>.gcode.3mf"
  --outputdir      "<per-job temp dir>"
```

`--outputdir` is **mandatory**. Without it the output landed nowhere findable in
testing. Every job gets its own directory so two slices cannot collide — the
gcode is always written as `plate_1.gcode`, a fixed name.

### 2.4 The output is startable, and we already parse it

A 20 mm cube produced a 43,677-byte `sliced.gcode.3mf` containing 19 entries,
including `Metadata/plate_1.gcode` (227,083 B), `Metadata/plate_1.gcode.md5`,
and `Metadata/slice_info.config`.

The repo's **existing** `server/threemf.py::parse_slice_info` read it unmodified:

```
{'seconds': 738, 'grams': 3.75,
 'filaments': [{'type': 'PLA', 'color': '#00AE42', 'used_g': 3.75}],
 'printer_model_id': None}
```

### 2.5 One sharp edge: `printer_model_id` is absent

CLI-produced 3mfs omit `printer_model_id`. Per `master.md` §5.3's
**UNKNOWN NEVER BLOCKS** rule this is safe — `model_mismatch()` returns `None`
the moment either side is empty, so nothing is refused — but it means a
CLI-sliced file **silently skips the model guard entirely**.

The design must not paper over this by pretending the file knows. It doesn't.
Instead: we know which printer we sliced *for*, so the job records that
`model_id` server-side at creation. The guard then works off provenance rather
than off a key the file never carries.

### 2.6 Tree supports are a one-key change

The flattened A1 process profile already carries `support_type = 'tree(auto)'`
and `enable_support = '0'`. The toggle is therefore literally `enable_support`.

Measured on an overhanging T-shaped test model:

| `enable_support` | plate_1.gcode | seconds | grams |
|---|---|---|---|
| `'0'` | 270,756 B | 485 | 1.70 |
| `'1'` | 891,633 B | **968** | **2.84** |

The estimates come from `parse_slice_info`, so the queue's existing time and
filament totals stay correct with no changes.

---

## 3. Architecture

Three new modules. `server/` gains no dependency on a slicer being installed.

| Module | Owns | Key names |
|---|---|---|
| `server/slicer.py` | Locating the slicer, resolving profiles, running the subprocess | `find_slicer`, `ProfileIndex`, `flatten_profile`, `build_argv`, `run_slice`, `SliceError`, `SlicerNotFound` |
| `server/slicepresets.py` | The curated quality tiers + filament mapping | `TIERS`, `PROCESS_TOKENS`, `resolve_preset`, `filament_profile_for`, `detect_loaded_filament` |
| `server/slicejobs.py` | Job records, states, the worker thread | `SliceJob`, `SliceJobStore`, `SliceCoordinator` |

### 3.1 "None means inert"

`find_slicer()` looks at `BAMBU_STUDIO_EXE`, then the default install path, and
returns `None` when there is no slicer. Passing `slicer=None` to `create_app`
makes every slice route **404**, exactly as `queue=None` and `detection=None`
already do (`master.md` §5.2). A machine with no slicer installed still boots,
still monitors, still prints from existing SD files.

### 3.2 The seams

Following the repo's existing testability rule — everything that touches
hardware or a subprocess is injectable:

- `flatten_profile(name, index)` is **pure**: dict-of-dicts in, flat dict out.
  Tested against a fake index, including a deliberate inheritance cycle.
- `build_argv(...)` is pure and tested like `DetectorSupervisor.build_argv`.
- `detect_loaded_filament(state)` is **pure**: fed an MQTT state dict.
- `SliceCoordinator` takes `run_slice` as a parameter, so the whole job state
  machine tests with no slicer, no camera, no printer.

---

## 4. Selection: printer, filament, preset, supports

### 4.1 Printer → machine profile

The user picks a **registered printer**; the machine profile is derived, never
chosen by hand. `model_id` (`N2S` = A1) already exists from §5.3.

This needs **one new `PrinterConfig` field: `nozzle`**, defaulting to `"0.4"`,
because machine profiles are per-nozzle (`Bambu Lab A1 0.4 nozzle`). It joins
`model_id` in the Edit form. A wrong nozzle is a genuine crash risk, so it is
configured and visible, not guessed — the same reasoning §5.3 applied to
`model_id`.

`nozzle` is validated in `PrinterConfig.from_dict` against the four known values
(`0.2`, `0.4`, `0.6`, `0.8`), degrading to `"0.4"` on anything else, consistent
with `normalize_roi()` degrading rather than raising.

### 4.2 Filament: detect, prefill, override

`detect_loaded_filament(state)` reads `ams.tray[].tray_type`, falling back to
`vt_tray`, and returns `None` when nothing is identifiable — which is the normal
case for a non-RFID third-party spool.

The UI dropdown is **prefilled with the detection and always editable**. A spool
the printer can't identify must never block slicing; that would make the feature
useless with generic filament, which is most filament.

### 4.3 Presets: curated tiers, resolved against the index

Chosen over enumerating the installed slicer's presets so the set is
reproducible, reviewable in git, and survives a slicer reinstall.

The obvious implementation — a table of literal profile names — **does not
work**, and the reasons were measured on 2026-07-22:

| Trap | Reality |
|---|---|
| Model token differs by profile kind | machine says `A1 mini`, process says **`A1M`**. The same printer has two different tokens |
| Nozzle suffix is conditional | `0.20mm Standard @BBL A1` (0.4 nozzle, **no suffix**) vs `0.30mm Standard @BBL A1 0.6 nozzle` |
| Layer height is **not** constant across nozzles | "Standard" is `0.20mm` on a 0.4 nozzle and `0.30mm` on a 0.6 |

So a preset cannot be a fixed profile name, and cannot hardcode a layer height.
A preset is a **quality tier**:

```python
TIERS = {"standard": "Standard", "fine": "Optimal", "draft": "Extra Draft"}
```

resolved against the profile index by searching for a process profile whose name
ends `{tier} @BBL {process_token}{nozzle_suffix}`, where:

- `process_token` = `A1` for `N2S`, `A1M` for the mini (a small explicit map, not
  string-munging of the machine name),
- `nozzle_suffix` = `""` for `0.4`, else `" {nozzle} nozzle"`.

The **label shown to the user is read off the resolved profile name**, so a 0.6
nozzle correctly reads "Standard 0.30 mm" rather than a hardcoded 0.20. Getting
this backwards would display a layer height the printer is not using.

`GET .../slice/options` returns only tiers that **actually resolve** for that
printer, so an unavailable combination is a missing option rather than a slice
that fails late. Verified present for the A1 at 0.4: `0.20mm Standard`,
`0.16mm Optimal`, `0.28mm Extra Draft`.

### 4.4 Supports

A single boolean per job, patched into the flattened process JSON as
`enable_support`. `support_type` stays at the profile's `tree(auto)`.

Exposed as one checkbox, "Tree supports". Default **off**, matching the profile
default — a support toggle that silently defaults on doubles print time (§2.6).

---

## 5. Data flow

```
POST /api/printers/{serial}/slice   (multipart: STL + preset + filament + supports)
   → 202 {job_id}                                        state: queued
   → worker thread (ONE at a time, global)
       1. flatten machine + process + filament → per-job temp JSONs
          (patch enable_support into the process JSON)
       2. bambu-studio.exe … --slice 0 --export-3mf … --outputdir <tmp>   state: slicing
       3. threemf.parse_slice_info → seconds, grams
       4. sdcard.upload_file  (existing FTPS STOR)                       state: uploading
       5. queue.add(sd_path, model_id=<the printer's>)                   state: done
```

**One slice at a time, globally.** Slicing pegs a core and this server also
supervises a YOLO detector process (`master.md` §2). A per-printer worker would
let a three-printer fleet start three slices at once and starve detection, which
is the one thing on this box that must stay responsive.

**Failure latches and touches nothing.** Any step failing sets `failed`, captures
the slicer's stderr, and leaves the queue unmodified — the same principle as
§5.4's "dequeue only on confirmation": a step that didn't happen must never
leave a half-finished job behind. Temp directories are removed on both paths.

The three defaults recommended in brainstorming and adopted here: `nozzle` is a
real config field (§4.1), the worker is global (above), and success chains
straight through to upload + queue with no confirmation step — the job is
visible and removable in the existing Queue page, and nothing starts printing
until someone presses Start.

---

## 6. Routes

| Method + path | Notes |
|---|---|
| `GET /api/printers/{serial}/slice/options` | Presets valid for this printer's model+nozzle, filament choices, and the detected filament. 404 when no slicer |
| `POST /api/printers/{serial}/slice` | Multipart STL + preset + filament + supports → 202 `{job_id}`. 400 on a non-model extension or empty body, 404 when no slicer |
| `GET /api/slice/jobs?serial=` | Job list, for polling |
| `DELETE /api/slice/jobs/{id}` | Cancel a queued job, or clear a finished one |

The upload-receiving route is a sync `def` so FastAPI runs it on the threadpool,
matching the FTPS routes (§3.2 of `master.md`) — reading a large STL off the
wire must not stall the event loop and freeze every WebSocket. The slice itself
runs on the coordinator's own thread and never on the event loop.

Accepted input extensions: `.stl`, `.3mf`, `.step`/`.stp`. Anything else is a
400 at the boundary.

Slice jobs are **runtime-only and not persisted** — a restart clears them. They
are transient work, and a half-finished slice pointing at a deleted temp
directory must not survive a reboot. This mirrors the "arm is runtime-only"
decision in §4.5. The *result* is durable, because it lands on the microSD and
in `queues.json`.

---

## 7. Frontend

One new page registered in `pageRegistry.jsx` (`slice`) — per §6 of `master.md`,
pages are added there and nowhere else, and get the standard
`{printers, selected, onSelect}` props.

Contents: a file drop zone, the selected printer shown read-only, a preset radio
group, a filament dropdown prefilled from detection, a "Tree supports" checkbox,
and a job list polled every 2 s showing state, and on completion the time/grams,
and on failure the slicer's stderr in a `<pre>`. Follows the `Queue.jsx` polling
pattern, with the same `requestId` guard against out-of-order responses.

New API wrappers in `src/api/printer.js`: `fetchSliceOptions`, `startSlice`,
`fetchSliceJobs`, `cancelSliceJob`.

---

## 8. Testing

Everything below runs with **no slicer, no printer, no camera** installed, in
keeping with §9 of `master.md`:

| Target | Test |
|---|---|
| `flatten_profile` | Chain resolution, child-overrides-parent, missing parent, inheritance cycle |
| `ProfileIndex` | Indexes by `name` not filename; later duplicates don't clobber |
| `build_argv` | `--outputdir` always present; support key patched; per-job temp paths |
| `resolve_preset` | `A1M` process token for the mini; nozzle suffix omitted at 0.4 and present at 0.6; label read off the resolved name (0.6 → "0.30mm"); unresolvable tier filtered out |
| `detect_loaded_filament` | AMS tray, `vt_tray` fallback, `None` for unidentifiable |
| `SliceCoordinator` | Full state machine against an injected fake `run_slice`: success chains to upload+queue, each failure step latches and leaves the queue untouched |
| Routes | Via `TestClient` against a fake registry, including 404-when-inert |
| `PrinterConfig.nozzle` | Validation and degradation to `"0.4"` |

**Not covered, and it must stay marked so:** that a CLI-sliced `.gcode.3mf`
actually starts and prints on the real A1. §2 proves the container has the right
shape and that our own parser reads it; it does **not** prove the printer accepts
it. Per the discipline in `master.md` §1.1, that stays *unverified* until someone
runs it on hardware, and this document should be updated when they do.

This also interacts with an existing gap: FTPS **STOR** has never written to a
real card (§9 of `master.md`). This feature makes STOR load-bearing, so the
hardware gate covers both — upload, then start.

---

## 9. Risks

- **The printer may reject a CLI-sliced 3mf.** The likeliest cause would be
  metadata Bambu Studio's GUI writes that the CLI doesn't. `plate_1.gcode.md5`
  is present, which is the checksum most likely to be verified. Untested (§8).
- **Profile names are version-dependent.** `0.16mm Optimal @BBL A1` exists in
  this install; a future Bambu Studio may rename it. Mitigated by filtering
  `slice/options` to presets that actually resolve (§4.3), so the failure is a
  missing option rather than a broken slice.
- **A wrong `nozzle` slices for the wrong hardware.** Configured, not guessed,
  and shown in the Edit form — the same trade-off §5.3 made for `model_id`.
- **Slicing is unbounded work.** A pathological STL can slice for a very long
  time. `run_slice` takes a timeout and kills the subprocess, surfacing the
  failure as a normal `failed` job.
