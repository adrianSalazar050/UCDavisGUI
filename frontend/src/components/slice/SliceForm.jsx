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
export default function SliceForm({ serial, options, onSubmitted }) {
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
    return (
      <div className="empty">
        No presets available for this printer. Check its model and nozzle on
        the Overview page.
      </div>
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
      <div className="slice-layout__viewer">
        {showViewer ? (
          <>
            <StlViewer arrayBuffer={buffer} rotation={rotation}
                       bed={options.bed} onFit={setFits} />
            <OrientControls rotation={rotation} onRotate={rotate}
                            onReset={() => setRotation(IDENTITY)} disabled={busy} />
            {!options.bed && (
              <div className="ui-field__help">
                Bed size unknown — showing a default 256×256 plate.
              </div>
            )}
            {!fits && (
              <div className="add-form__error">
                Model is larger than the build plate.
              </div>
            )}
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
