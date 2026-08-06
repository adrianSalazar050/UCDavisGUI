import { Suspense, lazy, useRef, useState } from "react";
import { startSlice, startSliceBlob } from "../../api/printer.js";
import Button from "../ui/Button.jsx";
import EmptyState from "../ui/EmptyState.jsx";
import Field from "../ui/Field.jsx";
import OrientControls from "./OrientControls.jsx";
import { IDENTITY, addRotation, isIdentity } from "./stlGeometry.js";

// three.js is heavy (~150 KB gz) and only ever needed once someone opens the
// Slice page AND picks an STL. Lazy-load the viewer so three.js lands in its
// own chunk, out of the main dashboard bundle. stlGeometry (pure math) and
// OrientControls (no three) stay static. The bake path (also three.js) is
// dynamically imported at submit time below, for the same reason.
const StlViewer = lazy(() => import("./StlViewer.jsx"));

const MODEL_ACCEPT = ".stl,.3mf,.step,.stp,.obj";
const isStl = (name) => /\.stl$/i.test(name || "");

// The server builds a preset label as "<tier> <layer height> mm", reading the
// height off whichever profile resolved (server/slicepresets.py) -- two facts
// answering two questions: which quality tier, and how thick its layers are on
// THIS nozzle. Splitting them lets the tier read first with the height beside
// it in secondary text, so a stack of presets is scannable without inventing
// any wording of our own. A label that doesn't match the shape is shown whole.
const PRESET_LABEL = /^(.+?)\s+([\d.]+\s*mm)$/;

function splitPresetLabel(label) {
  const m = PRESET_LABEL.exec(label ?? "");
  return m ? { tier: m[1], layer: m[2] } : { tier: label, layer: null };
}

// Prefers options.detected_filament, but only when the slicer actually ships
// a profile for it -- a detected material this printer has no profile for must
// still land on something the dropdown actually offers.
function initialMaterial(options) {
  const { detected_filament, filaments } = options;
  if (detected_filament && filaments.some((f) => f.material === detected_filament)) {
    return detected_filament;
  }
  return filaments[0]?.material ?? "";
}

// One printer's slice form, now two-column: an in-browser STL preview you can
// reorient (left) and the preset/filament/supports form (right). On submit,
// an STL that was rotated is baked into a new oriented STL and uploaded; an
// unrotated STL (or a non-STL we can't preview) uploads its original bytes,
// so the no-rotation path is byte-identical to before. The slicing backend is
// unchanged. Presets/filaments/detection/bed all come from `options`.
//
// `onNavigate` is OPTIONAL: only the no-presets empty state uses it, so the
// form still renders (minus that one button) for any caller that has no router
// to hand.
export default function SliceForm({ serial, options, onSubmitted, onNavigate }) {
  const [file, setFile] = useState(null);
  const [buffer, setBuffer] = useState(null); // STL ArrayBuffer for the preview
  const [rotation, setRotation] = useState(IDENTITY);
  const [fits, setFits] = useState(true);
  const [presetId, setPresetId] = useState(() => options.presets[0]?.id ?? "");
  const [material, setMaterial] = useState(() => initialMaterial(options));
  const [supports, setSupports] = useState(false); // profile default: off
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState(null);
  const fileInput = useRef(null);

  if (options.presets.length === 0) {
    // Model and nozzle are edited on the Printers page (Setup) -- and naming a
    // page is not the same as offering it, so this carries the button too.
    const model = options.model_id || "an unrecognised model";
    const nozzle = options.nozzle
      ? `a ${options.nozzle} mm nozzle` : "an unknown nozzle";
    return (
      <EmptyState
        title="No slicing presets for this printer"
        action={onNavigate && (
          <Button variant="primary" onClick={() => onNavigate("printers")}>
            Check model &amp; nozzle
          </Button>
        )}
      >
        No installed Bambu Studio process profile resolves for {model} with
        {" "}{nozzle}, so there is nothing to slice with. Correct either one on
        the Printers page, under Setup.
      </EmptyState>
    );
  }

  const onPick = (f) => {
    setFile(f);
    setRotation(IDENTITY);
    setBuffer(null);
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
    setBusy(true);
    setErr(null);
    try {
      if (buffer && !isIdentity(rotation)) {
        // Dynamic import so three.js (via stlBake) stays out of the main
        // bundle -- only pulled in when a rotated STL is actually baked.
        const { bakeRotatedStl } = await import("./stlBake.js");
        const blob = bakeRotatedStl(buffer, rotation);
        const rotatedName = file.name.replace(/\.stl$/i, "") + "-oriented.stl";
        await startSliceBlob(serial, blob, rotatedName,
                             { preset: presetId, material, supports });
      } else {
        await startSlice(serial, file, { preset: presetId, material, supports });
      }
      setFile(null);
      setBuffer(null);
      setRotation(IDENTITY);
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
      {/* `stack` is composed onto the viewer column for its vertical rhythm:
          the plate, the rotation controls and the two notes under them had no
          spacing between them, and no new class names may be invented. */}
      <div className="slice-layout__viewer stack">
        {showViewer ? (
          <>
            <Suspense fallback={<EmptyState title="Loading 3D viewer…" />}>
              <StlViewer arrayBuffer={buffer} rotation={rotation}
                         bed={options.bed} onFit={setFits} />
            </Suspense>
            <OrientControls rotation={rotation} onRotate={rotate}
                            onReset={() => setRotation(IDENTITY)} disabled={busy} />
            {!options.bed && (
              <div className="ui-field__help">
                Bed size unknown — this is a default 256 × 256 mm plate, so
                treat the fit check as a guess.
              </div>
            )}
            {!fits && (
              <div className="add-form__error">
                Model is larger than the build plate — try rotating it.
              </div>
            )}
          </>
        ) : file ? (
          <EmptyState title="No preview for this file">
            The plate view and the rotation controls are STL-only.
            {" "}{file.name} still slices, it just can't be reoriented here.
          </EmptyState>
        ) : (
          <EmptyState title="Nothing to preview yet">
            Pick an STL and it lands on its plate here, where you can turn it
            before slicing.
          </EmptyState>
        )}
      </div>

      <div className="slice-layout__form">
        <Field label="Model file"
               help="STL, 3MF, STEP or OBJ. Only STL can be previewed and turned.">
          <input ref={fileInput} type="file" accept={MODEL_ACCEPT}
                 onChange={(e) => onPick(e.target.files?.[0] ?? null)} />
        </Field>

        <div className="ui-field">
          <span className="ui-field__label">Quality preset</span>
          {/* role=group rather than a <fieldset>: nothing in the stylesheet
              tames a fieldset's default border, and the visible heading above
              is the group's name. */}
          <div className="slice-presets" role="group" aria-label="Quality preset">
            {options.presets.map((p) => {
              const { tier, layer } = splitPresetLabel(p.label);
              return (
                <label key={p.id} className="slice-presets__option">
                  <input type="radio" name="slice-preset" value={p.id}
                         checked={presetId === p.id}
                         onChange={() => setPresetId(p.id)} />
                  <span>{tier}</span>
                  {layer && <span className="muted">{layer}</span>}
                </label>
              );
            })}
          </div>
          <div className="ui-field__help">
            Thinner layers, finer detail, longer print.
          </div>
        </div>

        <Field label="Filament"
               help={options.detected_filament
                 ? `Detected in the printer: ${options.detected_filament}`
                 : "Nothing detected — pick what is loaded"}>
          <select value={material} onChange={(e) => setMaterial(e.target.value)}>
            {options.filaments.map((f) => (
              <option key={f.material} value={f.material}>{f.material}</option>
            ))}
          </select>
        </Field>

        <div className="ui-field">
          <span className="ui-field__label">Supports</span>
          <label className="add-form__check">
            <input type="checkbox" checked={supports}
                   onChange={(e) => setSupports(e.target.checked)} />
            Tree supports
          </label>
        </div>

        {err && <div className="add-form__error">{err}</div>}

        <div className="add-form__actions">
          <Button type="submit" variant="primary" busy={busy} disabled={!file}>
            Slice &amp; queue
          </Button>
          <span className="ui-field__help">
            {file
              ? "Slices, uploads to the microSD card, then queues it."
              : "Pick a model file to start."}
          </span>
        </div>
      </div>
    </form>
  );
}
