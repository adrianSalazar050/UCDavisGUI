import { useRef, useState } from "react";
import { startSlice } from "../../api/printer.js";
import Button from "../ui/Button.jsx";
import Field from "../ui/Field.jsx";

const MODEL_ACCEPT = ".stl,.3mf,.step,.stp,.obj";

// Prefers options.detected_filament, but only when the slicer actually ships
// a profile for it -- a detected material this printer has no profile for
// (e.g. a tag read as TPU on a printer with only PLA/PETG profiles) must
// still land on something the dropdown actually offers.
function initialMaterial(options) {
  const { detected_filament, filaments } = options;
  if (detected_filament && filaments.some((f) => f.material === detected_filament)) {
    return detected_filament;
  }
  return filaments[0]?.material ?? "";
}

// One printer's slice form: file, preset, filament, tree supports. Submitting
// slices, uploads over FTPS, and queues it server-side -- this component only
// ever sees `queued` come back (see SliceJobList for the rest of the state
// machine). Presets/filaments/detection all come from `options`, fetched once
// by the parent panel (Slice.jsx) for the selected printer.
export default function SliceForm({ serial, options, onSubmitted }) {
  const [file, setFile] = useState(null);
  const [presetId, setPresetId] = useState(() => options.presets[0]?.id ?? "");
  const [material, setMaterial] = useState(() => initialMaterial(options));
  const [supports, setSupports] = useState(false); // profile default: off
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState(null);
  const fileInput = useRef(null);

  // An empty options.presets means the model/nozzle is unset or unusual (see
  // slicepresets.resolve_preset) -- a bare empty form would give the user
  // nothing to act on, so explain it instead.
  if (options.presets.length === 0) {
    return (
      <div className="empty">
        No presets available for this printer. Check its model and nozzle on
        the Overview page.
      </div>
    );
  }

  const submit = async (e) => {
    e.preventDefault();
    if (!file || busy) return;
    setBusy(true);
    setErr(null);
    try {
      await startSlice(serial, file, { preset: presetId, material, supports });
      setFile(null);
      if (fileInput.current) fileInput.current.value = "";
      onSubmitted();
    } catch (e2) {
      setErr(e2.message);
    } finally {
      setBusy(false);
    }
  };

  return (
    <form className="add-form" onSubmit={submit}>
      <Field label="Model file" help={`Accepted: ${MODEL_ACCEPT}`}>
        <input ref={fileInput} type="file" accept={MODEL_ACCEPT}
               onChange={(e) => setFile(e.target.files?.[0] ?? null)} />
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
    </form>
  );
}
