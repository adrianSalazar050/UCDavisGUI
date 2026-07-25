# STL Preview + Reorient Implementation Plan (Feature B)

> **STATUS: NOT STARTED (2026-07-25).** Implements **Feature B** of
> `docs/superpowers/specs/2026-07-25-slicing-preview-and-all-models-design.md`
> (Feature A — P1P/P1S presets — already shipped). **`master.md` is
> authoritative wherever this file disagrees.**

> **For agentic workers:** REQUIRED SUB-SKILL: Use
> superpowers:subagent-driven-development or superpowers:executing-plans.
> Steps use checkbox (`- [ ]`) syntax.

**Goal:** On the Slice page, show the uploaded STL on a build-plate grid and
let the user reorient it (axis rotations + auto-drop) before slicing; bake the
chosen orientation into the mesh client-side and upload the rotated STL to the
unchanged slice endpoint.

**Architecture:** A new three.js `StlViewer` renders the STL in-browser; a pure
`stlGeometry.js` holds the reorientation math (rotation compose, auto-drop from
a bounding box, plate-fit), unit-tested with vitest; `OrientControls` drives it.
On slice, the accumulated rotation is applied to the geometry and exported to a
binary STL that goes to the existing `POST /api/printers/{serial}/slice`.
Unrotated → the original file bytes are uploaded unchanged. STL-only; 3MF/STEP
fall back to the current no-preview upload. The backend gains one field:
`bed:{x,y}` on the slice-options response, so the plate renders at the real bed
size.

**Tech Stack:** React 19 + Vite, **three.js** (new dependency) with its
`STLLoader`, `OrbitControls`, `STLExporter` addons; vitest; FastAPI (one field).

---

## Background the engineer needs

- **The Slice page today.** `frontend/src/pages/Slice.jsx` mounts a
  `SlicePanel` per printer that fetches `fetchSliceOptions(serial)` and renders
  `frontend/src/components/slice/SliceForm.jsx` (file input, preset radios,
  filament dropdown, tree-supports checkbox → `startSlice(serial, file, {...})`)
  plus a `SliceJobList`. `startSlice` (in `frontend/src/api/printer.js`) POSTs
  a multipart form with the `file` to `POST /api/printers/{serial}/slice`.
- **The one tested frontend module** is `frontend/src/components/detection/
  roiGeometry.js` (+ `.test.js`), pure math, vitest. `stlGeometry.js` follows
  that exact pattern. Everything else is verified by `npm run build` + eye.
- **Backend slice options** come from `SliceCoordinator.options(serial)` in
  `server/slicejobs.py` — presets, filaments, detected_filament. The machine
  profile's bed size is read from `printable_area` (e.g. the A1's
  `['0x0','256x0','256x256','0x256']` → 256×256), the same field
  `slicer._max_printable_y` already parses for the bed-forward eject.
- **Nothing about the slicing pipeline changes.** The viewer produces an STL
  the existing endpoint already accepts.

Conventions: em dashes / unicode are fine in `.jsx`/`.js`. The frontend is
deliberately minimal — three.js is the first heavyweight dependency and the
spec (§3.6) records it as a conscious trade; keep it imported only by the
viewer, on the Slice page, never app-wide.

---

## File structure

**Create:**

| Path | Responsibility |
|---|---|
| `frontend/src/components/slice/stlGeometry.js` | PURE reorientation math: rotation compose, drop translation from a bbox, plate-fit. No three.js import. |
| `frontend/src/components/slice/stlGeometry.test.js` | vitest for the above. |
| `frontend/src/components/slice/stlBake.js` | three.js glue: parse an STL ArrayBuffer, apply a rotation, export a binary STL Blob. Build/eye verified. |
| `frontend/src/components/slice/StlViewer.jsx` | three.js canvas: scene, camera, `OrbitControls`, build-plate grid sized to `bed`, the mesh, auto-dropped. |
| `frontend/src/components/slice/OrientControls.jsx` | X/Y/Z 90° buttons, fine sliders, reset. |

**Modify:**

| Path | Change |
|---|---|
| `frontend/package.json` | add `three`. |
| `frontend/src/components/slice/SliceForm.jsx` | two-column: viewer+controls on one side, existing form on the other; bake-and-upload on submit. |
| `server/slicejobs.py` | `options()` returns `bed:{x,y}` (or null). |
| `server/slicer.py` | add `bed_dimensions(machine)` reading `printable_area`. |
| `server/tests/test_slicer.py` | test `bed_dimensions`. |
| `server/tests/test_slicejobs.py` or `test_api.py` | assert options carries `bed`. |
| `master.md` | note the viewer under §6 (or a short line). |

---

## Task 1: Backend — `bed:{x,y}` in slice options

**Files:** Modify `server/slicer.py`, `server/slicejobs.py`; Test
`server/tests/test_slicer.py`, `server/tests/test_slicejobs.py`.

- [ ] **Step 1 — failing test for `bed_dimensions`.** Append to
  `server/tests/test_slicer.py`:

```python
from server.slicer import bed_dimensions


def test_bed_dimensions_parses_a_square_bed():
    m = {"printable_area": ["0x0", "256x0", "256x256", "0x256"]}
    assert bed_dimensions(m) == {"x": 256.0, "y": 256.0}


def test_bed_dimensions_parses_a_rectangular_bed():
    m = {"printable_area": ["0x0", "180x0", "180x180", "0x180"]}
    assert bed_dimensions(m) == {"x": 180.0, "y": 180.0}


def test_bed_dimensions_is_none_when_missing_or_unparseable():
    assert bed_dimensions({}) is None
    assert bed_dimensions({"printable_area": "nope"}) is None
    assert bed_dimensions({"printable_area": ["0x0", "bad"]}) is None
```

- [ ] **Step 2 — run, watch fail** (`ImportError: cannot import name
  'bed_dimensions'`). `python -m pytest server/tests/test_slicer.py -k bed -q`

- [ ] **Step 3 — implement `bed_dimensions`.** In `server/slicer.py`, next to
  `_max_printable_y`, add:

```python
def bed_dimensions(machine: dict):
    """Bed size {"x", "y"} in mm from the machine profile's printable_area,
    or None if missing/unparseable. Same tolerant parse as _max_printable_y --
    a corner list like ['0x0','256x0','256x256','0x256'] -> {"x":256,"y":256}.
    None means 'unknown'; the viewer falls back to a default plate rather than
    inventing a size."""
    area = machine.get("printable_area")
    if not isinstance(area, (list, tuple)) or not area:
        return None
    xs, ys = [], []
    for point in area:
        if not isinstance(point, str) or "x" not in point:
            return None
        a, b = point.split("x", 1)
        try:
            xs.append(float(a))
            ys.append(float(b))
        except ValueError:
            return None
    if not xs or not ys:
        return None
    return {"x": max(xs), "y": max(ys)}
```

- [ ] **Step 4 — run, pass.** `python -m pytest server/tests/test_slicer.py -k bed -q`

- [ ] **Step 5 — failing test for options carrying bed.** Find the existing
  `options()` test in `server/tests/test_slicejobs.py` (search `def options`
  or `.options(`); it uses a fake registry + a small index. Add a test that
  the returned dict has a `bed` key. If the fixture's index has no machine
  profile with a printable_area, `bed` is `None` — assert the KEY exists:

```python
def test_options_includes_a_bed_key(coordinator_and_fakes):
    # reuse whatever harness the other options test uses; the point is only
    # that the response always carries a 'bed' key (value may be None when the
    # machine profile or its printable_area is absent from the fixture index).
    coord = coordinator_and_fakes            # adapt to the real fixture name
    opts = coord.options("S1")
    assert "bed" in opts
```

Adapt the fixture/serial to match the file's existing options test. If the
existing test builds a machine profile with a `printable_area`, assert the
concrete `{"x":..,"y":..}` instead.

- [ ] **Step 6 — run, watch fail** (`KeyError`/`assert 'bed' in`).

- [ ] **Step 7 — implement.** In `server/slicejobs.py` `options()`, before the
  `return`, resolve the machine profile and its bed:

```python
        machine_name = slicepresets.machine_profile_name(model_id, nozzle)
        bed = None
        if machine_name and machine_name in self._index:
            try:
                flat = flatten_profile(machine_name, self._index)
                bed = slicer.bed_dimensions(flat)
            except SliceError:
                bed = None
```

and add `"bed": bed,` to the returned dict. Confirm the imports at the top of
`slicejobs.py` include `flatten_profile` and `SliceError` from `.slicer` (they
are already used there — check; if `flatten_profile` isn't imported, add it)
and `from . import slicer` / the `slicepresets` import already present.

- [ ] **Step 8 — run** the slicejobs + slicer tests, then `python -m pytest -q`.

- [ ] **Step 9 — commit**
  `feat(slice): expose the printer bed size in slice options`.

---

## Task 2: three.js dependency + the pure `stlGeometry.js`

**Files:** Modify `frontend/package.json`; Create
`frontend/src/components/slice/stlGeometry.js`, `.test.js`.

- [ ] **Step 1 — add three.js.**

```bash
cd frontend && npm install three@^0.169.0
```

Confirm `three` lands in `frontend/package.json` `dependencies` and
`package-lock.json` updates. (Version pinned to a recent stable; the addons
used — `STLLoader`, `OrbitControls`, `STLExporter` — live under
`three/examples/jsm/…` in this line.)

- [ ] **Step 2 — failing test.** Create
  `frontend/src/components/slice/stlGeometry.test.js`:

```js
import { describe, expect, it } from "vitest";
import { addRotation, dropTranslation, exceedsPlate, IDENTITY, isIdentity }
  from "./stlGeometry.js";

describe("addRotation", () => {
  it("adds degrees on an axis and wraps at 360", () => {
    expect(addRotation(IDENTITY, "x", 90)).toEqual({ x: 90, y: 0, z: 0 });
    expect(addRotation({ x: 300, y: 0, z: 0 }, "x", 90)).toEqual({ x: 30, y: 0, z: 0 });
  });
  it("handles negative wrap", () => {
    expect(addRotation(IDENTITY, "y", -90)).toEqual({ x: 0, y: 270, z: 0 });
  });
});

describe("isIdentity", () => {
  it("is true only for all-zero rotation", () => {
    expect(isIdentity(IDENTITY)).toBe(true);
    expect(isIdentity({ x: 0, y: 0, z: 0 })).toBe(true);
    expect(isIdentity({ x: 0, y: 90, z: 0 })).toBe(false);
  });
});

describe("dropTranslation", () => {
  it("centers X/Y and puts min-Z on the plate", () => {
    const bbox = { min: { x: 10, y: 20, z: 5 }, max: { x: 30, y: 60, z: 25 } };
    // center of X is 20, of Y is 40; min-Z is 5 -> translate by the negatives
    expect(dropTranslation(bbox)).toEqual({ x: -20, y: -40, z: -5 });
  });
});

describe("exceedsPlate", () => {
  const bed = { x: 256, y: 256 };
  it("false when the footprint fits", () => {
    const bbox = { min: { x: -50, y: -50, z: 0 }, max: { x: 50, y: 50, z: 40 } };
    expect(exceedsPlate(bbox, bed)).toBe(false);
  });
  it("true when the footprint is larger than the bed", () => {
    const bbox = { min: { x: -200, y: -10, z: 0 }, max: { x: 200, y: 10, z: 5 } };
    expect(exceedsPlate(bbox, bed)).toBe(true);
  });
  it("false when the bed is unknown (null) -- never hard-block", () => {
    const bbox = { min: { x: -999, y: -999, z: 0 }, max: { x: 999, y: 999, z: 1 } };
    expect(exceedsPlate(bbox, null)).toBe(false);
  });
});
```

- [ ] **Step 3 — run, watch fail** (`cd frontend && npm test`) — cannot
  resolve `./stlGeometry.js`.

- [ ] **Step 4 — implement.** Create
  `frontend/src/components/slice/stlGeometry.js`:

```js
// Pure reorientation math for the STL viewer. No three.js import -- it takes
// plain numbers so it unit-tests with vitest, the same discipline that makes
// detection/roiGeometry.js the one tested frontend module. three.js does the
// rendering and the actual mesh transform (stlBake.js / StlViewer.jsx); the
// decisions -- which rotation, where to drop, does it fit -- live here.

export const IDENTITY = { x: 0, y: 0, z: 0 };   // degrees per axis

// Add `degrees` about `axis` to a rotation, normalized to [0, 360).
export function addRotation(rot, axis, degrees) {
  const next = { ...rot };
  next[axis] = ((rot[axis] + degrees) % 360 + 360) % 360;
  return next;
}

export function isIdentity(rot) {
  return rot.x === 0 && rot.y === 0 && rot.z === 0;
}

// Translation that centers a bounding box in X/Y and rests its lowest point on
// the plate (z = 0). bbox = {min:{x,y,z}, max:{x,y,z}} in world space AFTER the
// rotation is applied (three.js recomputes it; this just reads it).
export function dropTranslation(bbox) {
  const cx = (bbox.min.x + bbox.max.x) / 2;
  const cy = (bbox.min.y + bbox.max.y) / 2;
  return { x: -cx, y: -cy, z: -bbox.min.z };
}

// Does the oriented footprint exceed the bed? bed may be null (unknown) -- then
// never block, because an unknown bed must not stop a slice (spec 3.8).
export function exceedsPlate(bbox, bed) {
  if (!bed) return false;
  const w = bbox.max.x - bbox.min.x;
  const d = bbox.max.y - bbox.min.y;
  return w > bed.x || d > bed.y;
}

export function degToRad(d) { return (d * Math.PI) / 180; }
```

- [ ] **Step 5 — run, pass** (`cd frontend && npm test` — the new tests plus
  the existing roiGeometry ones).

- [ ] **Step 6 — commit**
  `feat(slice): three.js dep + pure reorientation geometry`.

---

## Task 3: `stlBake.js` — parse, rotate, export a binary STL

**Files:** Create `frontend/src/components/slice/stlBake.js`.

This module wraps three.js's loader/exporter. It has no pure-unit test (it
needs three.js + a real geometry); it is exercised by the viewer and the
gstack check. Keep it tiny and single-purpose.

- [ ] **Step 1 — implement.** Create
  `frontend/src/components/slice/stlBake.js`:

```js
import { BufferGeometry, Mesh, MeshStandardMaterial, Euler } from "three";
import { STLLoader } from "three/examples/jsm/loaders/STLLoader.js";
import { STLExporter } from "three/examples/jsm/exporters/STLExporter.js";
import { degToRad } from "./stlGeometry.js";

// Parse an STL ArrayBuffer into a three.js BufferGeometry.
export function parseStl(arrayBuffer) {
  return new STLLoader().parse(arrayBuffer);   // throws on a bad STL
}

// Apply a rotation (degrees per axis) to a geometry and export a BINARY STL as
// a Blob, ready to upload. Applies the rotation to the vertices (bake), so the
// slicer receives an already-oriented mesh. The rotation order is X, then Y,
// then Z (Euler 'XYZ'), matching how the viewer composes the OrientControls.
export function bakeRotatedStl(arrayBuffer, rot) {
  const geom = parseStl(arrayBuffer);
  const euler = new Euler(degToRad(rot.x), degToRad(rot.y), degToRad(rot.z), "XYZ");
  geom.applyEuler ? geom.applyEuler(euler)
                  : geom.applyMatrix4(new Mesh(geom).setRotationFromEuler(euler).matrix);
  geom.computeVertexNormals();
  const mesh = new Mesh(geom, new MeshStandardMaterial());
  // STLExporter takes an Object3D; binary:true yields a compact ArrayBuffer.
  const data = new STLExporter().parse(mesh, { binary: true });
  return new Blob([data], { type: "model/stl" });
}
```

> Note: `BufferGeometry.applyEuler` exists in this three.js line; the ternary
> fallback is defensive. If `applyEuler` is missing at build time, replace the
> body with `geom.applyMatrix4(new Matrix4().makeRotationFromEuler(euler))`
> (import `Matrix4`). Verify which one compiles in Step 2.

- [ ] **Step 2 — build check.** `cd frontend && npm run build` — must compile.
  If the `applyEuler` line errors, switch to the `Matrix4` form noted above and
  rebuild.

- [ ] **Step 3 — commit** `feat(slice): bake a rotation into an STL client-side`.

---

## Task 4: `StlViewer.jsx` — the three.js canvas

**Files:** Create `frontend/src/components/slice/StlViewer.jsx`.

Renders the mesh on a plate grid, auto-dropped, with an orbit camera. Verified
by build + eye (gstack in Task 6).

- [ ] **Step 1 — implement.** Create
  `frontend/src/components/slice/StlViewer.jsx`:

```jsx
import { useEffect, useRef } from "react";
import {
  Scene, PerspectiveCamera, WebGLRenderer, GridHelper, Mesh,
  MeshStandardMaterial, DirectionalLight, AmbientLight, Box3, Euler, Color,
} from "three";
import { OrbitControls } from "three/examples/jsm/controls/OrbitControls.js";
import { parseStl } from "./stlBake.js";
import { dropTranslation, exceedsPlate, degToRad } from "./stlGeometry.js";

const DEFAULT_BED = { x: 256, y: 256 };

// A self-contained three.js viewport. Props:
//   arrayBuffer: the STL bytes (or null -> empty plate)
//   rotation: {x,y,z} degrees
//   bed: {x,y} mm or null (-> DEFAULT_BED, with the caller showing a note)
//   onFit(fits:boolean): reports whether the oriented model fits the plate
// Rebuilds the mesh on arrayBuffer/rotation change; the renderer/camera persist.
export default function StlViewer({ arrayBuffer, rotation, bed, onFit }) {
  const mountRef = useRef(null);
  const stateRef = useRef(null);   // {renderer,scene,camera,controls,mesh,raf}

  // one-time scene setup
  useEffect(() => {
    const mount = mountRef.current;
    const w = mount.clientWidth || 480, h = mount.clientHeight || 360;
    const scene = new Scene();
    scene.background = new Color(0x0e1116);
    const camera = new PerspectiveCamera(45, w / h, 1, 5000);
    camera.position.set(200, 200, 260);
    const renderer = new WebGLRenderer({ antialias: true });
    renderer.setSize(w, h);
    renderer.setPixelRatio(window.devicePixelRatio || 1);
    mount.appendChild(renderer.domElement);
    const controls = new OrbitControls(camera, renderer.domElement);
    controls.enableDamping = true;
    scene.add(new AmbientLight(0xffffff, 0.6));
    const dir = new DirectionalLight(0xffffff, 0.8);
    dir.position.set(1, 1, 1);
    scene.add(dir);
    const st = { renderer, scene, camera, controls, mesh: null, grid: null, raf: 0 };
    stateRef.current = st;
    const loop = () => { controls.update(); renderer.render(scene, camera); st.raf = requestAnimationFrame(loop); };
    loop();
    const onResize = () => {
      const ww = mount.clientWidth || 480, hh = mount.clientHeight || 360;
      camera.aspect = ww / hh; camera.updateProjectionMatrix(); renderer.setSize(ww, hh);
    };
    window.addEventListener("resize", onResize);
    return () => {
      window.removeEventListener("resize", onResize);
      cancelAnimationFrame(st.raf);
      controls.dispose();
      renderer.dispose();
      if (renderer.domElement.parentNode) renderer.domElement.parentNode.removeChild(renderer.domElement);
    };
  }, []);

  // plate grid sized to the bed
  useEffect(() => {
    const st = stateRef.current; if (!st) return;
    if (st.grid) { st.scene.remove(st.grid); st.grid.geometry.dispose(); }
    const b = bed || DEFAULT_BED;
    const size = Math.max(b.x, b.y);
    const grid = new GridHelper(size, Math.max(4, Math.round(size / 20)), 0x3a4150, 0x22262e);
    grid.rotation.x = Math.PI / 2;        // GridHelper is XZ by default; we use Z-up
    st.grid = grid; st.scene.add(grid);
  }, [bed]);

  // (re)build the mesh on new bytes or rotation
  useEffect(() => {
    const st = stateRef.current; if (!st) return;
    if (st.mesh) { st.scene.remove(st.mesh); st.mesh.geometry.dispose(); st.mesh.material.dispose(); st.mesh = null; }
    if (!arrayBuffer) { onFit?.(true); return; }
    let geom;
    try { geom = parseStl(arrayBuffer); } catch { onFit?.(true); return; }
    geom.applyEuler(new Euler(degToRad(rotation.x), degToRad(rotation.y), degToRad(rotation.z), "XYZ"));
    geom.computeVertexNormals();
    geom.computeBoundingBox();
    const bb = geom.boundingBox;
    const drop = dropTranslation({ min: bb.min, max: bb.max });
    geom.translate(drop.x, drop.y, drop.z);
    geom.computeBoundingBox();
    const mesh = new Mesh(geom, new MeshStandardMaterial({ color: 0x4c8bf5, metalness: 0.1, roughness: 0.7 }));
    st.mesh = mesh; st.scene.add(mesh);
    onFit?.(!exceedsPlate({ min: geom.boundingBox.min, max: geom.boundingBox.max }, bed));
  }, [arrayBuffer, rotation, bed, onFit]);

  return <div ref={mountRef} className="stl-viewer" style={{ width: "100%", height: 360 }} />;
}
```

> Coordinate note: three.js is Y-up by default; the code above treats Z as up
> (matching a printer bed) by rotating the grid and dropping on Z. If the
> model appears lying on its side in the gstack check, that's the axis
> convention — adjust the grid rotation / camera up vector then, not the math
> module. This is exactly the "verified by eye" part.

- [ ] **Step 2 — build.** `cd frontend && npm run build` — must compile.

- [ ] **Step 3 — commit** `feat(slice): three.js STL viewer with a sized plate`.

---

## Task 5: `OrientControls.jsx` + wire the two-column Slice form

**Files:** Create `frontend/src/components/slice/OrientControls.jsx`; Modify
`frontend/src/components/slice/SliceForm.jsx`.

- [ ] **Step 1 — OrientControls.** Create
  `frontend/src/components/slice/OrientControls.jsx`:

```jsx
import Button from "../ui/Button.jsx";
import { IDENTITY } from "./stlGeometry.js";

const AXES = ["x", "y", "z"];

// Rotation controls. Emits the NEXT rotation via onChange -- the math lives in
// stlGeometry.addRotation (applied by the parent) so this stays presentational.
export default function OrientControls({ rotation, onRotate, onReset, disabled }) {
  return (
    <div className="orient-controls">
      {AXES.map((axis) => (
        <div key={axis} className="orient-controls__axis">
          <span className="orient-controls__label">{axis.toUpperCase()}</span>
          <Button size="sm" disabled={disabled} onClick={() => onRotate(axis, -90)}>-90°</Button>
          <Button size="sm" disabled={disabled} onClick={() => onRotate(axis, 90)}>+90°</Button>
          <input type="range" min="0" max="359" value={rotation[axis]}
                 disabled={disabled} aria-label={`${axis} fine`}
                 onChange={(e) => onRotate(axis, Number(e.target.value) - rotation[axis])} />
          <span className="orient-controls__deg">{Math.round(rotation[axis])}°</span>
        </div>
      ))}
      <Button size="sm" disabled={disabled} onClick={onReset}>Reset</Button>
    </div>
  );
}
```

- [ ] **Step 2 — rewrite SliceForm to two columns + preview.** Replace
  `frontend/src/components/slice/SliceForm.jsx` with (keeps every existing
  behavior — preset/filament/supports/submit — and adds the viewer + bake):

```jsx
import { useRef, useState } from "react";
import { startSlice, startSliceBlob } from "../../api/printer.js";
import Button from "../ui/Button.jsx";
import Field from "../ui/Field.jsx";
import StlViewer from "./StlViewer.jsx";
import OrientControls from "./OrientControls.jsx";
import { IDENTITY, addRotation, isIdentity } from "./stlGeometry.js";
import { bakeRotatedStl } from "./stlBake.js";

const MODEL_ACCEPT = ".stl,.3mf,.step,.stp,.obj";
const isStl = (name) => /\.stl$/i.test(name || "");

function initialMaterial(options) {
  const { detected_filament, filaments } = options;
  if (detected_filament && filaments.some((f) => f.material === detected_filament)) {
    return detected_filament;
  }
  return filaments[0]?.material ?? "";
}

export default function SliceForm({ serial, options, onSubmitted }) {
  const [file, setFile] = useState(null);
  const [buffer, setBuffer] = useState(null);   // STL ArrayBuffer for preview
  const [rotation, setRotation] = useState(IDENTITY);
  const [fits, setFits] = useState(true);
  const [presetId, setPresetId] = useState(() => options.presets[0]?.id ?? "");
  const [material, setMaterial] = useState(() => initialMaterial(options));
  const [supports, setSupports] = useState(false);
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState(null);
  const fileInput = useRef(null);

  if (options.presets.length === 0) {
    return (
      <div className="empty">
        No presets available for this printer. Check its model and nozzle on
        the Overview page.
      </div>
    );
  }

  const onPick = (f) => {
    setFile(f); setRotation(IDENTITY); setBuffer(null);
    if (f && isStl(f.name)) {
      const reader = new FileReader();
      reader.onload = () => setBuffer(reader.result);
      reader.readAsArrayBuffer(f);
    }
  };

  const rotate = (axis, deg) => setRotation((r) => addRotation(r, axis, deg));

  const submit = async (e) => {
    e.preventDefault();
    if (!file || busy) return;
    setBusy(true); setErr(null);
    try {
      if (buffer && !isIdentity(rotation)) {
        // Bake the orientation into an STL and upload THAT.
        const blob = bakeRotatedStl(buffer, rotation);
        const rotatedName = file.name.replace(/\.stl$/i, "") + "-oriented.stl";
        await startSliceBlob(serial, blob, rotatedName, { preset: presetId, material, supports });
      } else {
        // No rotation (or a non-STL we can't preview) -> original bytes.
        await startSlice(serial, file, { preset: presetId, material, supports });
      }
      setFile(null); setBuffer(null); setRotation(IDENTITY);
      if (fileInput.current) fileInput.current.value = "";
      onSubmitted();
    } catch (e2) {
      setErr(e2.message);
    } finally {
      setBusy(false);
    }
  };

  const showViewer = file && isStl(file.name);

  return (
    <form className="add-form slice-layout" onSubmit={submit}>
      <div className="slice-layout__viewer">
        {showViewer ? (
          <>
            <StlViewer arrayBuffer={buffer} rotation={rotation}
                       bed={options.bed} onFit={setFits} />
            <OrientControls rotation={rotation} onRotate={rotate}
                            onReset={() => setRotation(IDENTITY)} disabled={busy} />
            {!options.bed && <div className="ui-field__help">Bed size unknown — showing a default 256×256 plate.</div>}
            {!fits && <div className="add-form__error">Model is larger than the build plate.</div>}
          </>
        ) : file ? (
          <div className="empty">3D preview is available for STL files only.</div>
        ) : (
          <div className="empty">Pick an STL to preview and reorient it.</div>
        )}
      </div>

      <div className="slice-layout__form">
        <Field label="Model file" help={`Accepted: ${MODEL_ACCEPT}`}>
          <input ref={fileInput} type="file" accept={MODEL_ACCEPT}
                 onChange={(e) => onPick(e.target.files?.[0] ?? null)} />
        </Field>

        <div className="ui-field">
          <span className="ui-field__label">Preset</span>
          <div className="slice-presets">
            {options.presets.map((p) => (
              <label key={p.id} className="slice-presets__option">
                <input type="radio" name="slice-preset" value={p.id}
                       checked={presetId === p.id}
                       onChange={() => setPresetId(p.id)} />
                {p.label}
              </label>
            ))}
          </div>
        </div>

        <Field label="Filament"
               help={options.detected_filament
                 ? `Detected: ${options.detected_filament}`
                 : "Not detected — pick what's loaded"}>
          <select value={material} onChange={(e) => setMaterial(e.target.value)}>
            {options.filaments.map((f) => (
              <option key={f.material} value={f.material}>{f.material}</option>
            ))}
          </select>
        </Field>

        <label className="add-form__check">
          <input type="checkbox" checked={supports}
                 onChange={(e) => setSupports(e.target.checked)} />
          Tree supports
        </label>

        {err && <div className="add-form__error">{err}</div>}

        <div className="add-form__actions">
          <Button type="submit" variant="primary" busy={busy} disabled={!file}>
            Slice &amp; queue
          </Button>
        </div>
      </div>
    </form>
  );
}
```

- [ ] **Step 3 — add `startSliceBlob` to the API wrapper.** In
  `frontend/src/api/printer.js`, find `startSlice` and add a sibling that
  uploads a Blob under a chosen filename (the endpoint is identical; only the
  form part differs):

```js
// Like startSlice, but for an in-memory Blob (a client-baked, reoriented STL)
// with an explicit filename. Same multipart endpoint.
export async function startSliceBlob(serial, blob, filename, { preset, material, supports }) {
  const form = new FormData();
  form.append("file", blob, filename);
  form.append("preset", preset);
  form.append("material", material);
  form.append("supports", String(supports));
  const res = await fetch(`/api/printers/${encodeURIComponent(serial)}/slice`,
                          { method: "POST", body: form });
  if (!res.ok) throw new Error(await detail(res));
  return res.json();
}
```

Check `startSlice`'s existing body shape and mirror the field names exactly
(`preset`/`material`/`supports` — confirm against the current `startSlice`).

- [ ] **Step 4 — a little CSS.** In `frontend/src/styles.css`, add (near the
  other slice rules):

```css
.slice-layout { display: grid; grid-template-columns: minmax(0, 1.4fr) minmax(0, 1fr); gap: var(--sp-5); align-items: start; }
.slice-layout__viewer { min-width: 0; }
.stl-viewer { border-radius: 8px; overflow: hidden; background: #0e1116; }
.orient-controls { display: flex; flex-wrap: wrap; gap: var(--sp-2); align-items: center; margin-top: var(--sp-3); }
.orient-controls__axis { display: flex; gap: var(--sp-1); align-items: center; }
.orient-controls__label { width: 1.4em; font-weight: 600; }
.orient-controls__deg { width: 3em; text-align: right; font-variant-numeric: tabular-nums; }
@media (max-width: 800px) { .slice-layout { grid-template-columns: 1fr; } }
```

- [ ] **Step 5 — build + vitest.** `cd frontend && npm run build && npm test`
  — both green.

- [ ] **Step 6 — commit** `feat(slice): two-column Slice page with STL preview and reorient`.

---

## Task 6: Visual check (gstack) + docs + full verification

- [ ] **Step 1 — run the app with mock data.** In one shell:
  `python -m server --mock --port 8151`. The mock A1 has a real model so
  presets resolve; `bed` will be the A1's 256×256.

- [ ] **Step 2 — gstack visual check.** Use the `gstack` skill to drive a
  headless browser: open `http://127.0.0.1:8151`, select the running mock
  printer, go to the **Slice** page, upload a small test STL (create one:
  write an ASCII cube STL to a temp file, or reuse
  `/tmp/cube.stl` if present), and screenshot. Confirm by eye:
  - the model renders on a plate grid,
  - the +90°/−90° buttons and sliders rotate it,
  - the model stays sitting on the plate after rotation (auto-drop),
  - a too-large model shows the "larger than the build plate" note.
  Capture a screenshot as evidence. If the model renders on its side, fix the
  axis convention in `StlViewer` (the grid rotation / camera up) — see the
  Task 4 coordinate note — and re-shoot.

- [ ] **Step 3 — real end-to-end (optional, no hardware needed for the bake).**
  Still on mock, pick an STL, rotate 90°, click **Slice & queue**. Confirm a
  job appears in the job list (mock upload succeeds). This exercises the
  bake→upload path without a real printer.

- [ ] **Step 4 — docs.** Add a short paragraph to `master.md` §6 (or a new
  sub-point) noting the Slice page now previews and reorients STLs in-browser
  via three.js, baking the orientation into the uploaded STL, backend
  unchanged, STL-only. Run `python -m pytest server/tests/test_docs.py -q`.

- [ ] **Step 5 — full verification.**
  `python -m pytest -q && cd frontend && npm test && npm run build` — all green.

- [ ] **Step 6 — commit** `docs: STL preview + reorient on the Slice page`.

---

## Self-review notes

- **Spec coverage:** viewer + axis-rotation + auto-drop (Tasks 3–5), client-side
  bake reusing the slice endpoint (Task 3, Task 5 submit), unrotated →
  original bytes (Task 5 `isIdentity` branch), STL-only with 3MF/STEP fallback
  (Task 5 `isStl`), bed sizing from `printable_area` (Task 1), pure tested math
  (Task 2), three.js recorded as a conscious dep (this plan + spec §3.6),
  error handling — bad STL caught in `StlViewer`/`parseStl`, oversize → non-
  blocking note, unknown bed → default plate, WebGL-less → the viewer simply
  renders nothing and the form still works. Fit/oversize is a warning, never a
  submit block (spec §3.8).
- **Not covered (deliberately, per spec §5):** reorienting a stored part,
  3MF/STEP preview, move/scale, lay-flat-on-face, server-side transform.
- **Consistent names:** `stlGeometry.js` exports `IDENTITY`, `addRotation`,
  `isIdentity`, `dropTranslation`, `exceedsPlate`, `degToRad`; `stlBake.js`
  exports `parseStl`, `bakeRotatedStl`; `StlViewer` props `arrayBuffer`,
  `rotation`, `bed`, `onFit`; `OrientControls` props `rotation`, `onRotate`,
  `onReset`, `disabled`; API `startSliceBlob(serial, blob, filename, opts)`;
  backend `bed_dimensions(machine)` and options key `bed`.
- **Open items from the spec (§6):** three.js is imported directly (not lazy-
  split) for simplicity; if the Slice-page bundle weight matters, a later
  `React.lazy` split of `StlViewer` is a drop-in follow-up. Fine rotation uses
  sliders (chosen).
