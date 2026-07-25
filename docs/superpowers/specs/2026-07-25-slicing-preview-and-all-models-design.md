# Slicing improvements: STL preview + reorient, and presets for all Bambu models — design

> **STATUS: DESIGN APPROVED 2026-07-25 — NOT IMPLEMENTED.** Two independent
> slicing improvements, brainstormed and approved. Feature A (all-model preset
> coverage) is a small backend change; Feature B (in-browser STL preview +
> reorient) is a larger frontend feature that adds this project's first
> heavyweight dependency (three.js). They get separate implementation plans.
> **`master.md` is authoritative wherever this file disagrees.**

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

### 2.2 The change

Extend both maps to cover all six models, with every token **verified against
the actually-installed vendor profiles**, not guessed. Confirmed by inspecting
`resources/profiles/BBL/**` on this machine (2026-07-25):

Verified against `resources/profiles/BBL/**` on this machine (2026-07-25 —
machine profile names, and which `@BBL <token>` process/filament profiles
exist):

| Model id | `MODEL_NAMES` | Machine token | Process/filament token | Notes |
|---|---|---|---|---|
| `N2S` | A1 | `A1` | `A1` | verified on hardware |
| `N1` | A1 mini | `A1 mini` | `A1M` | machine ≠ process token |
| `C11` | P1P | `P1P` | `P1P` | |
| `C12` | P1S | `P1S` | `P1P` | **machine `P1S`, but process/filament reuse `P1P`** — there is no `@BBL P1S` profile at all |
| `BL-P001` | X1 Carbon | `X1 Carbon` | `X1C` | machine ≠ process token |
| `BL-P002` | X1 | `X1` | `X1` | |

**Two rows are traps, both the same "one printer, two tokens" split
`master.md` §6.3 documents for the A1 mini:**

- **P1S** — machine profile is `Bambu Lab P1S 0.4 nozzle`, but its process and
  filament profiles are `@BBL P1P` (confirmed: no `@BBL P1S` profile exists in
  the tree). So P1S reuses a *different model's* process token. A naive
  `PROCESS_TOKENS["C12"] = "P1S"` yields zero presets.
- **X1 Carbon** — machine profile is `Bambu Lab X1 Carbon 0.4 nozzle` (token
  `X1 Carbon`, with the space), but its process/filament profiles are `@BBL
  X1C`. A naive `MACHINE_TOKENS["BL-P001"] = "X1C"` yields no machine profile.

`machine_profile_name` already formats as `f"Bambu Lab {token} {nozzle}
nozzle"`, which produces the correct `Bambu Lab X1 Carbon 0.4 nozzle` from the
`X1 Carbon` token — no format change needed, only the right tokens. This is
exactly why the tokens are two separate maps and must be read off real profile
names, never derived from each other.

The final tokens are locked in by the plan against the installed index (and,
if the user drops a real P1S/X1-sliced `.gcode.3mf` on the desktop, cross-
checked against the `printer_model_id` and profile names it recorded).

### 2.3 What does NOT change

- **CoreXY handling is already correct.** `slicer._BED_SLINGER_MODELS`
  (`master.md` §6.8) gates the bed-forward-eject on exactly the two A1 models,
  so P1/X1 (CoreXY — bed moves only in Z) already get *no* Y-eject move. A P1S
  slices correctly without touching that code.
- **Bed type.** P1/X1 ship a textured PEI plate by default like the A1;
  `PrinterConfig.bed_type` is already configurable per printer, so no model-
  specific bed logic is needed.

### 2.4 Honesty about verification

Only the A1 mapping is verified on real hardware here. The other five match the
installed profile names and community knowledge, but no one on this project has
sliced-and-printed on a real P1P/P1S/X1. The spec and code record that: the
tokens are *correct against the profiles*, not *proven on the machines*.

### 2.5 Testing

- A test asserts every model in `MACHINE_TOKENS` resolves **at least one**
  preset and **at least one** filament against a fixture of real profile names
  (a small hand-built index containing one profile per model). A wrong token
  fails the suite rather than surfacing as a silent empty dropdown.
- Tests that pin the two traps specifically, so a future "cleanup" that
  naively derives one token from the other breaks loudly: the P1S process token
  is `P1P` (not `P1S`), and the X1 Carbon machine token is `X1 Carbon` (not
  `X1C`).

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
