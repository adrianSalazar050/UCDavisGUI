# Slicing improvements: STL preview + reorient, and presets for all Bambu models — design

> **STATUS: BOTH FEATURES SHIPPED (2026-07-25).** Written before
> implementation; both landed the same day.
>
> - **Feature A — presets for all models.** Shipped, and it went **further
>   than this document deferred to**: `MACHINE_TOKENS`/`PROCESS_TOKENS` in
>   `server/slicepresets.py` now cover **five** models, not four — A1 (`N2S`),
>   A1 mini (`N1`), P1P (`C11`), P1S (`C12`) **and X1 Carbon (`BL-P001`)**.
>   §2.2/§2.6's deferral of the X-series is therefore obsolete. The reason it
>   became possible: real Bambu-Studio-sliced files showed the P1/X1 family
>   **shares one process/filament token, `X1C`**, while keeping its own
>   per-model machine token — so X1 Carbon needed no new convention, only the
>   same two-map split. See `master.md` §6.3.
> - **Feature B — STL preview + reorient.** Shipped as designed
>   (`frontend/src/components/slice/{StlViewer.jsx,stlGeometry.js,stlBake.js,
>   OrientControls.jsx}`), and three.js is indeed now this project's first
>   heavyweight frontend dependency. Verified in a headless browser. Plan:
>   `plans/2026-07-25-stl-preview-reorient.md`. See `master.md` §6.9.
>
> Historical record, not maintained. **`master.md` is authoritative wherever
> this file disagrees.**

Date: 2026-07-25

---

## 1. Why

Two things are missing from the slicing flow on the **Slice page** (ad-hoc STL
upload → preset/filament → slice; `frontend/src/pages/Slice.jsx`,
`master.md` §6):

- **You can't see what you're about to print.** The STL is uploaded blind and
  sliced in whatever orientation the file happens to carry. A user wants to see
  the model on the build plate and reorient it (stand it up, lay it flat)
  before committing to a slice.
- **Presets only resolve for two printer models.** `slicepresets.py` maps only
  `N2S` (A1) and `N1` (A1 mini) to vendor-profile tokens. Register a P1S and the
  slice options come back empty — even though Bambu Studio ships full profiles
  for it.

The two are unrelated and ship independently.

---

## 2. Feature A — presets for all Bambu models

### 2.1 What's there now

`server/slicepresets.py` resolves a preset by building a vendor-profile name
from the printer's model id and looking it up in the installed profile index
(`master.md` §6.3). The model→token maps are the only model-specific data:

```python
MACHINE_TOKENS = {"N2S": "A1", "N1": "A1 mini"}   # in MACHINE profile names
PROCESS_TOKENS = {"N2S": "A1", "N1": "A1M"}        # in PROCESS/FILAMENT names
```

Every other model id in `store.MODEL_NAMES` — `C11` (P1P), `C12` (P1S),
`BL-P001` (X1 Carbon), `BL-P002` (X1) — has no entry, so
`machine_profile_name`/`filament_profile_name` return `""` and the options
route yields empty lists.

### 2.2 What "all models" actually resolves to (verified 2026-07-25)

Verifying the proposed tokens against the real 1,932-profile index (not just
eyeballing names) revealed that only **four** of the six models cleanly fit the
existing `Generic <material> @BBL <token>` filament convention. The other two —
the X-series — do not, for concrete reasons below. So Feature A ships the four
that work end to end (presets **and** filaments resolve, so a user can actually
slice), and the X-series is a scoped follow-up rather than a half-working
dropdown.

**Shipped — the four that resolve completely** (presets in all three tiers +
generic filaments, at the 0.4 nozzle):

| Model id | `MODEL_NAMES` | Machine token | Process/filament token | Notes |
|---|---|---|---|---|
| `N2S` | A1 | `A1` | `A1` | verified on hardware |
| `N1` | A1 mini | `A1 mini` | `A1M` | machine ≠ process token |
| `C11` | P1P | `P1P` | `P1P` | |
| `C12` | P1S | `P1S` | `P1P` | **machine `P1S`, but process/filament reuse `P1P`** — no `@BBL P1S` profile exists at all |

The **P1S** row is the trap this table exists to record — the same "one
printer, two tokens" split `master.md` §6.3 documents for the A1 mini, now with
a model reusing a *different* model's process token. A naive
`PROCESS_TOKENS["C12"] = "P1S"` yields zero presets.

**Deferred — the X-series, and why** (`BL-P001` X1 Carbon, `BL-P002` X1):

- **The X-series has no `Generic <material>` filament profiles at all.** The
  `Generic PLA @BBL <token>` names exist only for `A1`, `A1M`, `P1P`, `P2S`,
  `H2*` — never `X1`/`X1C`. The X-series names its baseline PLA
  `Bambu PLA Basic @BBL X1C`, and the naming is irregular even within itself
  (PETG is `Bambu PETG Basic`, ABS is `Bambu ABS` with no "Basic", TPU is
  different again). So `filament_profile_name`'s single `f"Generic {material}
  @BBL {token}"` formula cannot resolve any X-series filament. X1 Carbon
  *presets* resolve (machine token `X1 Carbon`, process token `X1C`), but with
  no filament it can't slice — shipping it would be a dropdown that dead-ends.
- **The plain X1 (`BL-P002`) is a degenerate case.** Its only process profiles
  are `0.30mm Standard @BBL X1 0.6 nozzle` and `0.40mm Standard @BBL X1 0.8
  nozzle` — no 0.4-nozzle tier, no Optimal/Extra Draft. The curated tier system
  resolves nothing for it at the default nozzle. It is a rare legacy machine.

Supporting the X-series well means a small **per-model filament base-name**
layer (X-series → `Bambu <material> Basic` with per-material exceptions), which
is its own change with its own verification — not the "extend two dicts" this
feature is. It is written up in §2.6 as the follow-up.

`machine_profile_name` already formats as `f"Bambu Lab {token} {nozzle}
nozzle"`, so the `A1 mini` / `P1S` tokens (with spaces) produce the right
machine names with no format change — only the right tokens matter.

### 2.3 What does NOT change

- **CoreXY handling is already correct.** `slicer._BED_SLINGER_MODELS`
  (`master.md` §6.8) gates the bed-forward-eject on exactly the two A1 models,
  so P1/X1 (CoreXY — bed moves only in Z) already get *no* Y-eject move. A P1S
  slices correctly without touching that code.
- **Bed type.** P1/X1 ship a textured PEI plate by default like the A1;
  `PrinterConfig.bed_type` is already configurable per printer, so no model-
  specific bed logic is needed.

### 2.6 Follow-up: X-series filament support

Not in this feature; recorded so the deferral is a decision, not an omission.
To support X1 Carbon (and any future X-series), `filament_profile_name` needs a
per-model **filament base name**, because the X-series has no `Generic
<material>` profiles. Verified names for X1 Carbon (`X1C`), 0.4 nozzle:
`Bambu PLA Basic @BBL X1C`, `Bambu PETG Basic @BBL X1C`, `Bambu ABS @BBL X1C`
(no "Basic"), and TPU needs its own lookup (none of the tried names matched).
The plain X1 (`BL-P002`) would remain unsupported at 0.4 nozzle regardless —
its process profiles only exist at 0.6/0.8. This is a small, self-contained
change with its own index verification; a candidate for its own spec.

### 2.4 Honesty about verification

Only the A1 mapping is verified on real hardware here. P1P and P1S match the
installed profile names and community knowledge, but no one on this project has
sliced-and-printed on a real P1P/P1S. The spec and code record that: the
tokens are *correct against the profiles*, not *proven on the machines*.

### 2.5 Testing

- A test asserts every model in `MACHINE_TOKENS` resolves **at least one**
  preset and **at least one** filament against a fixture of real profile names
  (a small hand-built index containing one profile per model). A wrong token
  fails the suite rather than surfacing as a silent empty dropdown.
- A test that pins the P1S trap specifically, so a future "cleanup" that
  naively sets the P1S process token to `P1S` breaks loudly: `resolve_preset`
  for `C12` must resolve against a `@BBL P1P` process name.

---

## 3. Feature B — in-browser STL preview + reorient

### 3.1 The shape

A new `StlViewer` on the Slice page renders the uploaded STL on a build-plate
grid and lets the user reorient it before slicing. Built on **three.js** and
three of its standard addons:

- `STLLoader` — parse the uploaded file into a mesh, entirely in the browser.
- `OrbitControls` — drag to orbit/zoom the camera (view only; does not move the
  model).
- `STLExporter` — write the reoriented mesh back to a binary STL at slice time.

Nothing reaches the server until the user clicks **Slice**.

### 3.2 The data flow — reorientation is baked in only at slice time

1. User picks an STL → `FileReader` → `ArrayBuffer` → `STLLoader` → a mesh,
   shown auto-centered on the plate and dropped so its lowest point sits on
   Z = 0.
2. User reorients with **90° X/Y/Z buttons** plus **fine-angle sliders** and a
   **reset**. After every rotation the model **auto-drops** (re-translated so
   min-Z = 0) and re-centers in X/Y, so it never floats or sinks through the
   plate.
3. User picks preset + filament + supports (the existing controls, unchanged).
4. On **Slice**: the accumulated rotation is applied to the geometry's
   vertices, a binary STL is exported, and *that* STL is uploaded to the
   existing `POST /api/printers/{serial}/slice`. **The slicing backend is
   unchanged** — it receives an already-oriented STL and slices it exactly as
   today.

**Why client-side baking, not a server-side transform.** Applying the rotation
in the browser (where the mesh is already loaded) and reusing the existing
slice-STL endpoint means zero backend change — no Python mesh library, no
slicer-CLI orientation argument, no new upload path. The cost is that the
uploaded STL is a re-export rather than the original bytes; that is acceptable
(STL round-trips losslessly for this purpose). **If the user does not rotate at
all, the original file bytes are uploaded unchanged** — the re-export path is
taken only when a transform was actually applied, so an un-rotated slice is
byte-identical to today.

### 3.3 Scope boundary: STL only

three.js's `STLLoader` parses STL. The slice endpoint also accepts `.3mf` and
`.step`; `.step` needs a CAD kernel and `.3mf` is a zip container — neither is
previewable in-browser here. For a non-STL upload the page **falls back to the
current no-preview behavior** and shows "3D preview is available for STL files
only." The reorient feature is STL-only by design; the user asked for STL.

### 3.4 The build plate is sized to the printer

The plate grid renders at the selected printer's real bed dimensions so the
user can see whether the model fits. Source: the machine profile's
`printable_area` (e.g. the A1's `256×256`). Delivery: add a `bed: {x, y}` field
to the existing `GET /api/printers/{serial}/slice/options` response
(`server/slicejobs.py`/`server/main.py`). When the model is unknown (no machine
profile resolves), the viewer draws a **default 256×256 plate** with a small
"bed size unknown" note rather than nothing.

### 3.5 UI layout

The Slice page becomes two columns:

- **Left (prominent):** the `StlViewer` canvas, with the rotation controls
  (X/Y/Z 90° buttons, fine sliders, reset) directly beneath it, and a soft
  status line (triangle count / fit warning).
- **Right:** the existing preset radio group, filament dropdown, tree-supports
  checkbox, and the **Slice** button — unchanged in behavior, just relocated
  into a panel beside the viewer.

The polled job list stays below, unchanged.

### 3.6 The dependency, recorded as a conscious trade

three.js (~150 KB gzipped) becomes the first heavyweight dependency in a
frontend that is deliberately minimal — no UI framework, a hand-rolled kit,
one global stylesheet (`FRONTEND-STACK-GUIDE.md`). There is no lightweight way
to render and rotate a 3D mesh, so the dependency is justified, but it is a
real departure from the stack's ethos and the plan records it the way the
guide records its other choices. Only the STL viewer imports three.js; it is
loaded on the Slice page, not app-wide, so it never weighs on the rest of the
dashboard. (Whether to code-split it so it loads lazily on first Slice-page
visit is an implementation detail for the plan.)

### 3.7 What is pure and testable vs verified by eye

The geometry that must be correct is extracted into a pure `stlGeometry.js`
module — the same discipline that makes `roiGeometry.js` the one unit-tested
frontend module today:

- composing a sequence of axis rotations into one matrix,
- the auto-drop translation from a mesh's bounding box (min-Z → 0, center X/Y),
- the plate-fit check (does the oriented bounding box exceed `bed.x`/`bed.y`).

These get vitest coverage. The three.js rendering, camera, and canvas wiring are
verified by `npm run build` + eye, which `master.md` §10 already accepts as the
frontend's real gap.

### 3.8 Error handling

- A file that `STLLoader` cannot parse → an inline error, no crash, the
  existing upload path still available.
- A very large triangle count → a soft "large model, preview may be slow"
  warning; three.js renders it, but the user is told.
- A model whose oriented bounding box exceeds the bed → a non-blocking "larger
  than the build plate" warning (slicing may still be attempted; the printer/
  slicer is the final arbiter, and unknown bed sizes must not hard-block).
- WebGL unavailable (rare, old/headless browser) → fall back to the no-preview
  upload with a note, never a blank page.

---

## 4. Components and files

**Feature A** (backend only):
- Modify `server/slicepresets.py` — extend `MACHINE_TOKENS`/`PROCESS_TOKENS`;
  the P1S process-token comment.
- Modify `server/tests/test_slicepresets.py` — the all-models resolution test
  and the P1S-token trap test.

**Feature B** (frontend, plus one tiny backend field):
- Create `frontend/src/components/slice/StlViewer.jsx` — the three.js canvas +
  camera + plate + mesh.
- Create `frontend/src/components/slice/stlGeometry.js` — pure rotation/drop/fit
  math.
- Create `frontend/src/components/slice/stlGeometry.test.js` — vitest.
- Create `frontend/src/components/slice/OrientControls.jsx` — the rotation
  buttons/sliders/reset.
- Modify `frontend/src/pages/Slice.jsx` — two-column layout, wire the viewer,
  bake-and-upload on slice.
- Modify `frontend/package.json` — add `three`.
- Modify `server/slicejobs.py` (`options`) and/or `server/main.py`
  (`slice_options` route) — add `bed: {x, y}` to the options response, derived
  from the machine profile's `printable_area`.
- Modify `server/tests/test_slicejobs.py` / `test_api.py` — assert the options
  response carries a `bed`.

---

## 5. Explicitly out of scope

- **Reorienting a stored part** (the Parts-page slice-from-recipe flow). This
  feature is on the ad-hoc Slice page only; a stored part's orientation would
  be a saved-transform feature, designed separately if wanted.
- **3MF/STEP preview.** STL only (§3.3).
- **Move/scale/multi-object placement.** Rotation + auto-drop only (approved
  scope).
- **"Lay flat on a picked face."** Considered and deferred — needs face-pick
  raycasting; the axis-rotation MVP covers the common needs.
- **Server-side mesh transform.** The client-side bake makes it unnecessary.
- **Hardware verification of the non-A1 model tokens.** Correct against the
  profiles; not proven on the machines (§2.4).

---

## 6. Open questions

1. **Code-splitting three.js.** Lazy-load it on first Slice-page visit vs bundle
   it always. Leaning lazy (keeps the initial dashboard bundle unaffected), but
   it's an implementation detail for the plan, not a design decision.
2. **Fine-rotation UX.** Sliders vs a numeric degree input per axis. Sliders in
   the design; the plan can pick whichever reads better in the kit.
