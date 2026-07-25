import { useState } from "react";
import { updatePrinter } from "../../api/printer.js";
import Button from "../ui/Button.jsx";
import Field from "../ui/Field.jsx";
import { BED_TYPES, NOZZLES, PRINTER_MODELS, guessModelId } from "./printerModels.js";

// Prefilled from the printer summary, which never carries the access code
// (see EditPrinter in server/main.py) -- that field always starts blank.
export default function EditPrinterForm({ printer, onDone }) {
  const [form, setForm] = useState({
    host: printer.printer,
    access_code: "",
    name: printer.name ?? "",
    capture: !!printer.capture,
    // Fall back to the serial-prefix guess only when nothing is stored, so
    // an existing printer gets a sensible preselection without ever having
    // its deliberate choice overwritten.
    model_id: printer.model_id || guessModelId(printer.serial),
    // The printer summary always carries bed_type (see _with_bed_type in
    // server/main.py), so the "Textured PEI Plate" fallback below is only
    // for safety, not the normal path -- unlike model_id there's no
    // "Unknown" choice, every printer has a plate actually installed.
    bed_type: printer.bed_type || "Textured PEI Plate",
    // The printer summary always carries nozzle (see _with_nozzle in
    // server/main.py), so the "0.4" fallback below is only for safety, not
    // the normal path -- unlike model_id there's no "Unknown" choice, every
    // printer has a nozzle actually installed.
    nozzle: printer.nozzle || "0.4",
  });
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState(null);

  const set = (k) => (e) =>
    setForm((f) => ({
      ...f,
      [k]: e.target.type === "checkbox" ? e.target.checked : e.target.value,
    }));

  const submit = async (e) => {
    e.preventDefault();
    setBusy(true);
    setErr(null);
    try {
      // name/capture are sent every time, prefilled -- the backend applies
      // them unconditionally, so omitting them would reset them.
      await updatePrinter(printer.serial, {
        host: form.host.trim(),
        access_code: form.access_code.trim(),
        name: form.name.trim(),
        capture: form.capture,
        model_id: form.model_id,
        bed_type: form.bed_type,
        nozzle: form.nozzle,
      });
      onDone(); // /ws pushes the refreshed card in on its own
    } catch (e2) {
      setErr(e2.message);
    } finally {
      setBusy(false);
    }
  };

  const ready = form.host.trim();

  return (
    <form className="add-form" onSubmit={submit}>
      <div className="add-form__row">
        <Field label="IP address" value={form.host} onChange={set("host")}
               placeholder="192.168.137.2"
               help="Printer screen: Settings → WLAN" />
        <Field label="Serial" value={printer.serial} disabled
               help="Serial can't be changed" />
      </div>
      <div className="add-form__row">
        <Field label="LAN access code" value={form.access_code}
               onChange={set("access_code")} placeholder="00000000"
               help="Leave blank to keep the current code" />
        <Field label="Name (optional)" value={form.name} onChange={set("name")}
               placeholder="A1-bench" help="Defaults to the IP address" />
      </div>
      <div className="add-form__row">
        <Field label="Printer model"
               help="Refuses starting a file sliced for another model. “Unknown” disables the check.">
          <select value={form.model_id} onChange={set("model_id")}>
            {PRINTER_MODELS.map((m) => (
              <option key={m.id} value={m.id}>{m.name}</option>
            ))}
          </select>
        </Field>
        <Field label="Build plate"
               help="Sets the bed temperature a slice heats to -- must match the plate actually installed.">
          <select value={form.bed_type} onChange={set("bed_type")}>
            {BED_TYPES.map((b) => (
              <option key={b} value={b}>{b}</option>
            ))}
          </select>
        </Field>
      </div>
      <div className="add-form__row">
        <Field label="Nozzle"
               help="Selects the machine profile a slice uses -- must match the nozzle actually installed.">
          <select value={form.nozzle} onChange={set("nozzle")}>
            {NOZZLES.map((n) => (
              <option key={n} value={n}>{n} mm</option>
            ))}
          </select>
        </Field>
      </div>
      <label className="add-form__check">
        <input type="checkbox" checked={form.capture} onChange={set("capture")} />
        This printer is the one the webcam points at
      </label>
      {err && <div className="add-form__error">{err}</div>}
      <div className="add-form__actions">
        <Button type="submit" variant="primary" busy={busy} disabled={!ready}>
          Save
        </Button>
        <Button type="button" variant="secondary" onClick={onDone} disabled={busy}>
          Cancel
        </Button>
      </div>
    </form>
  );
}
