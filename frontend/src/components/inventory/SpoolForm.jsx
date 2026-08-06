import { useState } from "react";
import { createSpool } from "../../api/printer.js";
import Button from "../ui/Button.jsx";
import Field from "../ui/Field.jsx";

const MATERIALS = ["PLA", "PETG", "ABS", "TPU"];
const BLANK = { spool_code: "", material: "PLA", colour: "", brand: "",
                initial_grams: "1000" };

// Add a physical spool. spool_code is the unique handle the operator scans or
// types; a duplicate comes back 409, surfaced in the error line.
export default function SpoolForm({ onCreated }) {
  const [form, setForm] = useState(BLANK);
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState(null);
  const set = (k) => (e) => setForm((f) => ({ ...f, [k]: e.target.value }));

  const submit = async (e) => {
    e.preventDefault();
    setBusy(true); setErr(null);
    try {
      const { spool } = await createSpool({
        spool_code: form.spool_code.trim(),
        material: form.material,
        colour: form.colour.trim() || null,
        brand: form.brand.trim() || null,
        initial_grams: form.initial_grams ? Number(form.initial_grams) : null,
      });
      setForm(BLANK);
      onCreated?.(spool.id);
    } catch (e2) { setErr(e2.message); } finally { setBusy(false); }
  };

  return (
    <form className="add-form" onSubmit={submit}>
      <div className="add-form__row">
        <Field label="Spool code" value={form.spool_code}
               onChange={set("spool_code")} placeholder="PLA-BLK-001"
               help={"Unique across the lab — scan it, or type what is on "
                     + "the reel's label"} />
        <Field label="Material">
          <select value={form.material} onChange={set("material")}>
            {MATERIALS.map((m) => <option key={m} value={m}>{m}</option>)}
          </select>
        </Field>
      </div>
      <div className="add-form__row">
        <Field label="Colour (optional)" value={form.colour}
               onChange={set("colour")} placeholder="Black" />
        <Field label="Brand (optional)" value={form.brand}
               onChange={set("brand")} placeholder="Bambu" />
      </div>
      {/* The number every "remaining" figure on the page is derived from, so
          the help says what happens when it is left out: the server reports
          remaining as unknown rather than inventing one. */}
      <Field label="Initial weight (g)" type="number" min="0"
             value={form.initial_grams} onChange={set("initial_grams")}
             help={"Net filament weight, not the weight with the reel — a 1 kg "
                   + "spool is 1000. Leave it blank if you don't know: "
                   + "remaining then reads as unknown, never as a number that "
                   + "isn't true."} />
      {err && <div className="add-form__error">{err}</div>}
      <div className="add-form__actions">
        <Button type="submit" variant="primary" busy={busy}
                disabled={!form.spool_code.trim()}>Add spool</Button>
      </div>
    </form>
  );
}
